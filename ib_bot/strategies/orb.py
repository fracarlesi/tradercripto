"""
Opening Range Breakout Strategy
================================

Detects breakouts above/below the Opening Range with:
- VWAP confirmation
- ATR volatility filter
- Buffer ticks for noise filtering
- No re-entry after stop out (handled by execution engine)

Entry Rules:
  LONG:  close > OR_high + buffer_ticks AND price > VWAP AND ATR > min
  SHORT: close < OR_low  - buffer_ticks AND price < VWAP AND ATR > min

Stop: OR midpoint (+/- buffer)
Target: 1.5x risk (R:R 1:1.5)
"""

import logging
from datetime import time
from decimal import Decimal
from typing import Optional

from .base import BaseStrategy, StrategyResult
from ..config.loader import StrategyConfig, StopsConfig
from ..core.contracts import CONTRACTS
from ..core.enums import Direction, SessionPhase, SetupType
from ..core.models import FuturesMarketState, ORBRange, ORBSetup

logger = logging.getLogger(__name__)


class ORBStrategy(BaseStrategy):
    """Opening Range Breakout strategy for futures."""

    def __init__(
        self,
        strategy_config: StrategyConfig,
        stops_config: StopsConfig,
    ) -> None:
        super().__init__(config={})
        self._strategy = strategy_config
        self._stops = stops_config

    @property
    def name(self) -> str:
        return "orb"

    def evaluate(
        self,
        state: FuturesMarketState,
        or_range: ORBRange,
    ) -> StrategyResult:
        """Evaluate for ORB breakout.

        Args:
            state: Current market state with VWAP and ATR
            or_range: Calculated Opening Range

        Returns:
            StrategyResult with setup if breakout detected
        """
        # Must be in active trading phase
        if state.session_phase != SessionPhase.ACTIVE_TRADING:
            return self.reject(f"Wrong phase: {state.session_phase.value}")

        # OR must be valid
        if not or_range.valid:
            return self.reject("OR range invalid (too flat or too wide)")

        # Check max entry time
        try:
            max_entry = time.fromisoformat(self._strategy.max_entry_time)
            if hasattr(state.timestamp, "time") and state.timestamp.time() > max_entry:
                return self.reject(f"Past max entry time {self._strategy.max_entry_time}")
        except Exception:
            pass

        # Get contract spec for tick calculations
        spec = CONTRACTS.get(state.symbol)
        if not spec:
            return self.reject(f"Unknown contract: {state.symbol}")

        # ATR filter
        atr_ticks = int(state.atr_14 / spec.tick_size) if spec.tick_size > 0 else 0
        if atr_ticks < self._strategy.min_atr_ticks:
            return self.reject(
                f"ATR too low: {atr_ticks} ticks < {self._strategy.min_atr_ticks}"
            )

        buffer = Decimal(str(self._strategy.breakout_buffer_ticks)) * spec.tick_size
        price = state.last_price

        # Check for LONG breakout
        long_entry = or_range.or_high + buffer
        if price > long_entry:
            if self._strategy.vwap_confirmation and price <= state.vwap:
                return self.reject("LONG rejected: price below VWAP")

            return self._build_setup(
                state=state,
                or_range=or_range,
                direction=Direction.LONG,
                entry_price=price,
                spec=spec,
            )

        # Check for SHORT breakout
        if self._strategy.allow_short:
            short_entry = or_range.or_low - buffer
            if price < short_entry:
                if self._strategy.vwap_confirmation and price >= state.vwap:
                    return self.reject("SHORT rejected: price above VWAP")

                return self._build_setup(
                    state=state,
                    or_range=or_range,
                    direction=Direction.SHORT,
                    entry_price=price,
                    spec=spec,
                )

        return self.reject("No breakout detected")

    def _build_setup(
        self,
        state: FuturesMarketState,
        or_range: ORBRange,
        direction: Direction,
        entry_price: Decimal,
        spec: "FuturesSpec",  # type: ignore[name-defined]
    ) -> StrategyResult:
        """Build a complete ORB setup with stop and target.

        Stop: OR midpoint +/- buffer
        Target: entry + (risk * reward_risk_ratio)
        """
        stop_buffer = Decimal(str(self._stops.stop_buffer_ticks)) * spec.tick_size

        if direction == Direction.LONG:
            stop_price = or_range.midpoint - stop_buffer
            risk = entry_price - stop_price
            target_price = entry_price + (risk * self._stops.reward_risk_ratio)
            setup_type = SetupType.ORB_LONG
        else:
            stop_price = or_range.midpoint + stop_buffer
            risk = stop_price - entry_price
            target_price = entry_price - (risk * self._stops.reward_risk_ratio)
            setup_type = SetupType.ORB_SHORT

        risk_ticks = int(risk / spec.tick_size) if spec.tick_size > 0 else 0
        reward_ticks = int(abs(target_price - entry_price) / spec.tick_size) if spec.tick_size > 0 else 0

        if risk_ticks <= 0:
            return self.reject("Invalid risk: 0 ticks")

        # Confidence based on VWAP alignment strength
        vwap_distance = abs(entry_price - state.vwap) / entry_price
        confidence = min(Decimal("1.0"), Decimal("0.5") + vwap_distance * 10)

        setup = ORBSetup(
            symbol=state.symbol,
            direction=direction,
            setup_type=setup_type,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            risk_ticks=risk_ticks,
            reward_ticks=reward_ticks,
            or_range=or_range,
            confidence=confidence,
        )

        logger.info(
            "ORB %s setup: %s entry=%.2f stop=%.2f target=%.2f "
            "risk=%d ticks reward=%d ticks R:R=1:%.1f",
            direction.value, state.symbol,
            float(entry_price), float(stop_price), float(target_price),
            risk_ticks, reward_ticks,
            float(self._stops.reward_risk_ratio),
        )

        return StrategyResult(has_setup=True, setup=setup)
