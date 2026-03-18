"""
EMA Momentum Strategy (Live)
==============================

EMA-9/EMA-21 crossover strategy for live futures trading.

Entry Rules:
  LONG:  EMA9 crosses above EMA21 AND RSI in [30, 65]
  SHORT: EMA9 crosses below EMA21 AND RSI in [35, 70]

Stop: ATR(14) * multiplier from entry
Target: stop_distance * reward_risk_ratio from entry

Ported from backtesting/simulator_ema.py into BaseStrategy interface.
"""

import logging
from datetime import time
from decimal import Decimal
from typing import Dict, Optional

from .base import BaseStrategy, StrategyResult
from ..config.loader import EMAStrategyConfig, StopsConfig
from ..core.contracts import CONTRACTS
from ..core.enums import Direction, SessionPhase, SetupType
from ..core.models import FuturesMarketState, ORBRange, ORBSetup

logger = logging.getLogger(__name__)


class _IndicatorState:
    """Running EMA, RSI, and ATR state for one symbol.

    Maintains incremental calculations across bar-by-bar updates.
    The strategy calls ``update()`` once per bar (via ``evaluate()``),
    passing close, high, and low prices.
    """

    def __init__(self, ema_fast: int, ema_slow: int, rsi_period: int) -> None:
        self.bar_count: int = 0

        # EMA state
        self.ema_fast: Optional[Decimal] = None
        self.ema_slow: Optional[Decimal] = None
        self._ema_fast_k = Decimal("2") / (Decimal(str(ema_fast)) + Decimal("1"))
        self._ema_slow_k = Decimal("2") / (Decimal(str(ema_slow)) + Decimal("1"))
        self._ema_fast_period = ema_fast
        self._ema_slow_period = ema_slow

        # Warmup buffer
        self._closes: list[Decimal] = []

        # RSI state
        self._rsi_period = rsi_period
        self._prev_close: Optional[Decimal] = None
        self._avg_gain: Optional[Decimal] = None
        self._avg_loss: Optional[Decimal] = None
        self._rsi_warmup_gains: list[Decimal] = []
        self._rsi_warmup_losses: list[Decimal] = []
        self.rsi: Optional[Decimal] = None

        # ATR state (uses rsi_period as atr_period, same 14 default)
        self._atr_period = rsi_period
        self._prev_bar_close: Optional[Decimal] = None
        self._atr_warmup: list[Decimal] = []
        self.atr: Optional[Decimal] = None

        # Previous EMA values for crossover detection
        self.prev_ema_fast: Optional[Decimal] = None
        self.prev_ema_slow: Optional[Decimal] = None

    def update(self, high: Decimal, low: Decimal, close: Decimal) -> None:
        """Update all indicators with a new bar."""
        self.bar_count += 1
        self._closes.append(close)

        # --- EMA ---
        self.prev_ema_fast = self.ema_fast
        self.prev_ema_slow = self.ema_slow

        if self.bar_count <= self._ema_slow_period:
            if self.bar_count == self._ema_fast_period:
                sma = sum(self._closes[-self._ema_fast_period:]) / Decimal(str(self._ema_fast_period))
                self.ema_fast = sma
            elif self.bar_count > self._ema_fast_period and self.ema_fast is not None:
                self.ema_fast = close * self._ema_fast_k + self.ema_fast * (Decimal("1") - self._ema_fast_k)

            if self.bar_count == self._ema_slow_period:
                sma = sum(self._closes[-self._ema_slow_period:]) / Decimal(str(self._ema_slow_period))
                self.ema_slow = sma
        else:
            if self.ema_fast is not None:
                self.ema_fast = close * self._ema_fast_k + self.ema_fast * (Decimal("1") - self._ema_fast_k)
            if self.ema_slow is not None:
                self.ema_slow = close * self._ema_slow_k + self.ema_slow * (Decimal("1") - self._ema_slow_k)

        # --- RSI ---
        if self._prev_close is not None:
            delta = close - self._prev_close
            gain = max(delta, Decimal("0"))
            loss = max(-delta, Decimal("0"))

            if self._avg_gain is None:
                self._rsi_warmup_gains.append(gain)
                self._rsi_warmup_losses.append(loss)

                if len(self._rsi_warmup_gains) == self._rsi_period:
                    self._avg_gain = sum(self._rsi_warmup_gains) / Decimal(str(self._rsi_period))
                    self._avg_loss = sum(self._rsi_warmup_losses) / Decimal(str(self._rsi_period))
                    self._compute_rsi()
            else:
                period = Decimal(str(self._rsi_period))
                self._avg_gain = (self._avg_gain * (period - Decimal("1")) + gain) / period
                self._avg_loss = (self._avg_loss * (period - Decimal("1")) + loss) / period
                self._compute_rsi()

        self._prev_close = close

        # --- ATR ---
        if self._prev_bar_close is not None:
            tr = max(
                high - low,
                abs(high - self._prev_bar_close),
                abs(low - self._prev_bar_close),
            )

            if self.atr is None:
                self._atr_warmup.append(tr)
                if len(self._atr_warmup) == self._atr_period:
                    self.atr = sum(self._atr_warmup) / Decimal(str(self._atr_period))
            else:
                alpha = Decimal("1") / Decimal(str(self._atr_period))
                self.atr = alpha * tr + (Decimal("1") - alpha) * self.atr

        self._prev_bar_close = close

    def _compute_rsi(self) -> None:
        if self._avg_loss is not None and self._avg_loss > Decimal("0"):
            rs = self._avg_gain / self._avg_loss  # type: ignore[operator]
            self.rsi = Decimal("100") - (Decimal("100") / (Decimal("1") + rs))
        else:
            self.rsi = Decimal("100")

    def is_ready(self) -> bool:
        return (
            self.ema_fast is not None
            and self.ema_slow is not None
            and self.prev_ema_fast is not None
            and self.prev_ema_slow is not None
            and self.rsi is not None
            and self.atr is not None
        )

    def has_bullish_cross(self) -> bool:
        if not self.is_ready():
            return False
        return (
            self.prev_ema_fast <= self.prev_ema_slow  # type: ignore[operator]
            and self.ema_fast > self.ema_slow  # type: ignore[operator]
        )

    def has_bearish_cross(self) -> bool:
        if not self.is_ready():
            return False
        return (
            self.prev_ema_fast >= self.prev_ema_slow  # type: ignore[operator]
            and self.ema_fast < self.ema_slow  # type: ignore[operator]
        )


class EMAMomentumStrategy(BaseStrategy):
    """EMA-9/21 crossover momentum strategy for live futures trading.

    Maintains per-symbol indicator state across bar updates.
    Each call to ``evaluate()`` updates indicators and checks
    for crossover signals with RSI filtering.
    """

    def __init__(
        self,
        ema_config: EMAStrategyConfig,
        stops_config: StopsConfig,
    ) -> None:
        super().__init__(config={})
        self._ema_cfg = ema_config
        self._stops = stops_config
        # Per-symbol indicator state (persists across bars)
        self._indicators: Dict[str, _IndicatorState] = {}
        # Per-symbol daily trade count
        self._daily_trade_count: int = 0

    @property
    def name(self) -> str:
        return "ema_momentum"

    def reset_daily(self) -> None:
        """Reset indicator state and trade counts for a new trading day."""
        self._indicators.clear()
        self._daily_trade_count = 0

    def _get_indicators(self, symbol: str) -> _IndicatorState:
        """Get or create indicator state for a symbol."""
        if symbol not in self._indicators:
            self._indicators[symbol] = _IndicatorState(
                ema_fast=self._ema_cfg.ema_fast,
                ema_slow=self._ema_cfg.ema_slow,
                rsi_period=self._ema_cfg.rsi_period,
            )
        return self._indicators[symbol]

    def evaluate(
        self,
        state: FuturesMarketState,
        or_range: ORBRange,
    ) -> StrategyResult:
        """Evaluate for EMA crossover signal.

        Updates internal indicator state with the current bar's price,
        then checks for bullish/bearish EMA crossover with RSI filter.

        Args:
            state: Current market state (provides close price, ATR, timestamp).
            or_range: Opening Range (used for context but not for entry logic).

        Returns:
            StrategyResult with setup if crossover signal detected.
        """
        # Must be in active trading phase
        if state.session_phase != SessionPhase.ACTIVE_TRADING:
            return self.reject(f"Wrong phase: {state.session_phase.value}")

        # Check max entry time
        try:
            max_entry = time.fromisoformat(self._ema_cfg.max_entry_time)
            if hasattr(state.timestamp, "time") and state.timestamp.time() > max_entry:
                return self.reject(f"Past max entry time {self._ema_cfg.max_entry_time}")
        except Exception:
            pass

        # Check daily trade limit
        if self._daily_trade_count >= self._ema_cfg.max_trades_per_day:
            return self.reject(
                f"Daily trade limit reached: {self._daily_trade_count}/{self._ema_cfg.max_trades_per_day}"
            )

        spec = CONTRACTS.get(state.symbol)
        if not spec:
            return self.reject(f"Unknown contract: {state.symbol}")

        # Update indicators with current bar
        # Use last_price as close; use last_price for high/low too
        # (in live trading we get tick-level updates, not full bars,
        #  so high=low=close for each update)
        indicators = self._get_indicators(state.symbol)
        price = state.last_price
        indicators.update(high=price, low=price, close=price)

        if not indicators.is_ready():
            return self.reject("Indicators warming up")

        # Check for crossover signals
        direction: Optional[Direction] = None
        if indicators.has_bullish_cross():
            direction = Direction.LONG
        elif indicators.has_bearish_cross():
            direction = Direction.SHORT

        if direction is None:
            return self.reject("No EMA crossover")

        # RSI filter
        rsi_val = float(indicators.rsi)  # type: ignore[arg-type]
        if direction == Direction.LONG:
            if not (self._ema_cfg.rsi_long_min <= rsi_val <= self._ema_cfg.rsi_long_max):
                return self.reject(
                    f"LONG RSI filter: {rsi_val:.1f} not in "
                    f"[{self._ema_cfg.rsi_long_min}, {self._ema_cfg.rsi_long_max}]"
                )
        else:
            if not self._ema_cfg.allow_short:
                return self.reject("Short entries disabled")
            if not (self._ema_cfg.rsi_short_min <= rsi_val <= self._ema_cfg.rsi_short_max):
                return self.reject(
                    f"SHORT RSI filter: {rsi_val:.1f} not in "
                    f"[{self._ema_cfg.rsi_short_min}, {self._ema_cfg.rsi_short_max}]"
                )

        # Build setup with ATR-based stops
        return self._build_setup(
            state=state,
            or_range=or_range,
            direction=direction,
            entry_price=price,
            atr=indicators.atr,  # type: ignore[arg-type]
            rsi=rsi_val,
            spec=spec,
        )

    def _build_setup(
        self,
        state: FuturesMarketState,
        or_range: ORBRange,
        direction: Direction,
        entry_price: Decimal,
        atr: Decimal,
        rsi: float,
        spec: "object",
    ) -> StrategyResult:
        """Build EMA trade setup with ATR-based stop and R:R target."""
        tick_size = getattr(spec, "tick_size", Decimal("0.25"))

        stop_distance = atr * Decimal(str(self._ema_cfg.atr_stop_multiplier))
        rr_ratio = Decimal(self._ema_cfg.reward_risk_ratio)

        if direction == Direction.LONG:
            stop_price = entry_price - stop_distance
            target_price = entry_price + stop_distance * rr_ratio
            setup_type = SetupType.EMA_LONG
        else:
            stop_price = entry_price + stop_distance
            target_price = entry_price - stop_distance * rr_ratio
            setup_type = SetupType.EMA_SHORT

        risk_ticks = int(stop_distance / tick_size) if tick_size > 0 else 0
        reward_ticks = int(abs(target_price - entry_price) / tick_size) if tick_size > 0 else 0

        if risk_ticks <= 0:
            return self.reject("Invalid risk: 0 ticks")

        # Confidence based on EMA separation strength
        indicators = self._get_indicators(state.symbol)
        ema_spread = abs(indicators.ema_fast - indicators.ema_slow) / entry_price  # type: ignore[operator]
        confidence = min(Decimal("1.0"), Decimal("0.5") + ema_spread * Decimal("100"))

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

        self._daily_trade_count += 1

        logger.info(
            "EMA %s setup: %s entry=%.2f stop=%.2f target=%.2f "
            "risk=%d ticks reward=%d ticks R:R=1:%.1f ATR=%.2f RSI=%.1f",
            direction.value, state.symbol,
            float(entry_price), float(stop_price), float(target_price),
            risk_ticks, reward_ticks,
            float(rr_ratio), float(atr), rsi,
        )

        return StrategyResult(has_setup=True, setup=setup)
