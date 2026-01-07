"""
Mean Reversion Strategy
========================

Secondary strategy for HLQuantBot (DISABLED by default).

Entry conditions:
- Regime is RANGE
- Price at Bollinger Band extremes
- RSI oversold/overbought confirmation

Exit:
- Target: Bollinger mid-band
- Stop: ATR-based

This strategy is riskier and should only be enabled after validation.
"""

import logging
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional

from .base import BaseStrategy, StrategyResult
from ..core.models import MarketState, Setup, Regime, Direction, SetupType


logger = logging.getLogger(__name__)


class MeanReversionStrategy(BaseStrategy):
    """
    Mean reversion strategy for range-bound markets.

    Works only in RANGE regime. Enters when price is at extremes
    of Bollinger Bands with RSI confirmation.

    WARNING: This strategy is disabled by default. Enable only after
    extensive backtesting and paper trading validation.
    """

    def __init__(self, config: dict = None):
        """
        Initialize mean reversion strategy.

        Config options:
            enabled: Whether strategy is enabled (default: False)
            bb_period: Bollinger Bands period (default: 20)
            bb_std: Bollinger Bands standard deviations (default: 2.0)
            rsi_oversold: RSI oversold threshold (default: 30)
            rsi_overbought: RSI overbought threshold (default: 70)
            stop_atr_mult: ATR multiplier for stop (default: 2.0)
            max_adx: Maximum ADX for entry (default: 20)
        """
        super().__init__(config)

        # DISABLED BY DEFAULT
        self.enabled = self.config.get("enabled", False)

        # Configuration
        self.bb_period = self.config.get("bb_period", 20)
        self.bb_std = self.config.get("bb_std", 2.0)
        self.rsi_oversold = self.config.get("rsi_oversold", 30)
        self.rsi_overbought = self.config.get("rsi_overbought", 70)
        self.stop_atr_mult = self.config.get("stop_atr_mult", 2.0)
        self.max_adx = self.config.get("max_adx", 20.0)

        self._logger.info(
            "MeanReversionStrategy initialized: enabled=%s, RSI=%d/%d",
            self.enabled,
            self.rsi_oversold,
            self.rsi_overbought,
        )

    @property
    def name(self) -> str:
        return "mean_reversion"

    @property
    def required_regime(self) -> Regime:
        return Regime.RANGE

    def evaluate(self, state: MarketState) -> StrategyResult:
        """
        Evaluate market state for mean reversion setup.

        Checks:
        1. Strategy is enabled
        2. Regime is RANGE
        3. ADX < max_adx
        4. Price at Bollinger extremes
        5. RSI confirmation
        """
        # Check if enabled
        if not self.enabled:
            return StrategyResult(
                has_setup=False,
                reason="Strategy disabled"
            )

        # Check regime
        if not self.can_trade(state):
            return StrategyResult(
                has_setup=False,
                reason=f"Wrong regime: {state.regime.value}, need RANGE"
            )

        # Check ADX
        if float(state.adx) > self.max_adx:
            return StrategyResult(
                has_setup=False,
                reason=f"ADX too high: {state.adx:.1f} > {self.max_adx}"
            )

        # Check Bollinger Bands
        if state.bb_lower is None or state.bb_upper is None:
            return StrategyResult(
                has_setup=False,
                reason="Bollinger Bands not available"
            )

        # Determine direction based on price position and RSI
        direction = self._determine_direction(state)
        if direction == Direction.FLAT:
            return StrategyResult(
                has_setup=False,
                reason="No mean reversion setup"
            )

        # Calculate entry and stop prices
        entry_price = state.close
        stop_price = self.calculate_stop_price(
            entry_price=entry_price,
            atr=state.atr,
            direction=direction,
            atr_mult=self.stop_atr_mult,
        )
        stop_distance_pct = self.calculate_stop_distance_pct(entry_price, stop_price)

        # Calculate quality
        quality = self._calculate_quality(state, direction)

        # Create setup
        setup = Setup(
            id=self.generate_setup_id(),
            symbol=state.symbol,
            timestamp=datetime.now(timezone.utc),
            setup_type=SetupType.MEAN_REVERSION,
            direction=direction,
            regime=state.regime,
            entry_price=entry_price,
            stop_price=stop_price,
            stop_distance_pct=stop_distance_pct,
            atr=state.atr,
            adx=state.adx,
            rsi=state.rsi,
            setup_quality=quality,
            confidence=Decimal(str(min(0.8, 0.3 + quality / 2))),
        )

        self._logger.info(
            "SETUP: %s %s @ %.2f (mean reversion), stop=%.2f",
            direction.value.upper(),
            state.symbol,
            float(entry_price),
            float(stop_price),
        )

        return StrategyResult(
            has_setup=True,
            setup=setup,
            reason="Mean reversion conditions met"
        )

    def _determine_direction(self, state: MarketState) -> Direction:
        """
        Determine trade direction based on Bollinger Bands and RSI.

        Long: Price near lower BB + RSI oversold
        Short: Price near upper BB + RSI overbought
        """
        rsi = float(state.rsi)
        price = float(state.close)
        bb_lower = float(state.bb_lower)
        bb_upper = float(state.bb_upper)
        bb_mid = float(state.bb_mid)

        # Calculate position within bands
        bb_range = bb_upper - bb_lower
        if bb_range <= 0:
            return Direction.FLAT

        position_pct = (price - bb_lower) / bb_range * 100

        # Long setup: price in lower 20% and RSI oversold
        if position_pct < 20 and rsi < self.rsi_oversold:
            return Direction.LONG

        # Short setup: price in upper 20% and RSI overbought
        if position_pct > 80 and rsi > self.rsi_overbought:
            return Direction.SHORT

        return Direction.FLAT

    def _calculate_quality(self, state: MarketState, direction: Direction) -> Decimal:
        """
        Calculate setup quality score 0-1.

        Factors:
        - RSI extremity
        - Distance from Bollinger extreme
        - Choppiness confirmation
        """
        score = Decimal("0.4")  # Base score

        rsi = float(state.rsi)
        price = float(state.close)
        bb_lower = float(state.bb_lower)
        bb_upper = float(state.bb_upper)

        # RSI extremity bonus
        if direction == Direction.LONG:
            if rsi < 20:
                score += Decimal("0.3")
            elif rsi < 25:
                score += Decimal("0.2")
            elif rsi < 30:
                score += Decimal("0.1")
        else:  # SHORT
            if rsi > 80:
                score += Decimal("0.3")
            elif rsi > 75:
                score += Decimal("0.2")
            elif rsi > 70:
                score += Decimal("0.1")

        # Bollinger position bonus
        bb_range = bb_upper - bb_lower
        if bb_range > 0:
            if direction == Direction.LONG:
                distance_pct = (price - bb_lower) / bb_range
                if distance_pct < 0.05:  # Very close to lower band
                    score += Decimal("0.2")
                elif distance_pct < 0.1:
                    score += Decimal("0.1")
            else:  # SHORT
                distance_pct = (bb_upper - price) / bb_range
                if distance_pct < 0.05:
                    score += Decimal("0.2")
                elif distance_pct < 0.1:
                    score += Decimal("0.1")

        # Choppiness confirmation
        if state.choppiness and float(state.choppiness) > 60:
            score += Decimal("0.1")

        return min(Decimal("1.0"), max(Decimal("0.0"), score))
