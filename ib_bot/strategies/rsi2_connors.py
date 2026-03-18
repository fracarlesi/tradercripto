"""
RSI(2) Connors Mean Reversion Strategy
========================================

Daily timeframe, long-only mean reversion strategy for MES futures.

Rules:
  - LONG ONLY (S&P upward bias)
  - Entry: RSI(2) closes below 10 AND price > SMA(200) on daily bars
  - Exit:  RSI(2) closes above 70
  - Stop:  20 points fixed catastrophe stop ($100 on MES)
  - Max hold: 7 trading days (force exit if RSI hasn't recovered)
  - Only 1 position at a time

Evaluation: once per day at 16:00 ET using daily bar data.
Signals queue a market order for next day's open.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from .base import BaseStrategy, StrategyResult
from ..config.loader import RSI2ConnorsConfig, StopsConfig
from ..core.contracts import CONTRACTS
from ..core.enums import Direction, SetupType
from ..core.models import ORBRange, ORBSetup

logger = logging.getLogger(__name__)


@dataclass
class DailyBar:
    """Single daily OHLC bar."""

    date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal("0")


class RSI2ConnorsStrategy(BaseStrategy):
    """RSI(2) Connors mean reversion strategy.

    Evaluates daily bars to detect oversold conditions (RSI(2) < 10)
    with a trend filter (price > SMA(200)). Long only.

    The strategy is called once per day at market close. It maintains
    state about the current position (entry date, entry price) to
    manage max hold days and the catastrophe stop.
    """

    def __init__(
        self,
        rsi2_config: RSI2ConnorsConfig,
        stops_config: StopsConfig,
        symbol: str = "MES",
    ) -> None:
        super().__init__(config={})
        self._cfg = rsi2_config
        self._stops = stops_config
        self._symbol = symbol

        # Position tracking
        self._in_position: bool = False
        self._entry_price: Optional[Decimal] = None
        self._entry_date: Optional[date] = None
        self._hold_days: int = 0

    @property
    def name(self) -> str:
        return "rsi2_connors"

    def reset_daily(self) -> None:
        """Reset daily state. Position state persists across days."""
        pass

    def reset_position(self) -> None:
        """Clear position tracking (called after exit fill confirmed)."""
        self._in_position = False
        self._entry_price = None
        self._entry_date = None
        self._hold_days = 0

    # =========================================================================
    # Core Evaluation
    # =========================================================================

    def evaluate(
        self,
        state: "object",
        or_range: "object",
    ) -> StrategyResult:
        """Not used for RSI2 -- use evaluate_daily() instead.

        This exists only to satisfy the BaseStrategy ABC. The RSI2 strategy
        operates on daily bars, not intraday market data ticks.
        """
        return self.reject("RSI2 uses evaluate_daily(), not evaluate()")

    def evaluate_daily(
        self,
        bars: list[DailyBar],
        current_date: date,
    ) -> StrategyResult:
        """Evaluate daily bars for RSI(2) entry/exit signals.

        Called once per day at 16:00 ET with the latest daily bars.
        Requires at least sma_period + rsi_period bars for indicator warmup.

        Args:
            bars: Daily OHLC bars sorted oldest-first. Must have at least
                  sma_period (200) bars for SMA calculation.
            current_date: Today's date (for hold-day counting).

        Returns:
            StrategyResult with setup if entry or exit signal detected.
        """
        min_bars = self._cfg.sma_period + self._cfg.rsi_period + 1
        if len(bars) < min_bars:
            return self.reject(
                f"Need {min_bars} bars, have {len(bars)} (warming up)"
            )

        # Calculate indicators on the full bar series
        closes = [b.close for b in bars]
        rsi_value = self._compute_rsi(closes, self._cfg.rsi_period)
        sma_value = self._compute_sma(closes, self._cfg.sma_period)

        if rsi_value is None or sma_value is None:
            return self.reject("Indicator calculation failed")

        last_bar = bars[-1]
        last_close = last_bar.close

        logger.info(
            "RSI2 eval [%s]: close=%.2f RSI(2)=%.2f SMA(%d)=%.2f "
            "in_position=%s hold_days=%d",
            self._symbol, float(last_close), float(rsi_value),
            self._cfg.sma_period, float(sma_value),
            self._in_position, self._hold_days,
        )

        # --- EXIT LOGIC (check first) ---
        if self._in_position:
            return self._evaluate_exit(
                rsi_value=rsi_value,
                last_close=last_close,
                current_date=current_date,
            )

        # --- ENTRY LOGIC ---
        return self._evaluate_entry(
            rsi_value=rsi_value,
            sma_value=sma_value,
            last_close=last_close,
            current_date=current_date,
        )

    # =========================================================================
    # Entry / Exit Logic
    # =========================================================================

    def _evaluate_entry(
        self,
        rsi_value: Decimal,
        sma_value: Decimal,
        last_close: Decimal,
        current_date: date,
    ) -> StrategyResult:
        """Check entry conditions: RSI(2) < threshold AND price > SMA(200)."""
        # Trend filter: price must be above SMA(200)
        if last_close <= sma_value:
            return self.reject(
                f"Price {float(last_close):.2f} below SMA({self._cfg.sma_period}) "
                f"{float(sma_value):.2f} - no uptrend"
            )

        # RSI(2) must be below entry threshold
        if float(rsi_value) >= self._cfg.rsi_entry_threshold:
            return self.reject(
                f"RSI(2) {float(rsi_value):.2f} >= {self._cfg.rsi_entry_threshold} "
                f"- not oversold"
            )

        # All conditions met -- generate BUY signal
        spec = CONTRACTS.get(self._symbol)
        tick_size = spec.tick_size if spec else Decimal("0.25")

        entry_price = last_close  # will execute at next open (approx)
        stop_price = entry_price - Decimal(str(self._cfg.stop_points))
        # Target: dummy high value since we exit on RSI, not fixed target
        target_price = entry_price + Decimal(str(self._cfg.stop_points * 3))

        risk_ticks = int(Decimal(str(self._cfg.stop_points)) / tick_size)
        reward_ticks = risk_ticks * 3

        # Create a dummy ORBRange for compatibility with ORBSetup
        dummy_or = self._make_dummy_or_range(last_close)

        setup = ORBSetup(
            symbol=self._symbol,
            direction=Direction.LONG,
            setup_type=SetupType.RSI2_LONG,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            risk_ticks=risk_ticks,
            reward_ticks=reward_ticks,
            or_range=dummy_or,
            confidence=Decimal("0.7"),
        )

        # Mark position as open (will be confirmed on fill)
        self._in_position = True
        self._entry_price = entry_price
        self._entry_date = current_date
        self._hold_days = 0

        logger.info(
            "RSI2 ENTRY SIGNAL: %s BUY @ %.2f | RSI(2)=%.2f | "
            "stop=%.2f (%d pts) | hold limit=%d days",
            self._symbol, float(entry_price), float(rsi_value),
            float(stop_price), self._cfg.stop_points, self._cfg.max_hold_days,
        )

        return StrategyResult(has_setup=True, setup=setup, reason="RSI2 entry")

    def _evaluate_exit(
        self,
        rsi_value: Decimal,
        last_close: Decimal,
        current_date: date,
    ) -> StrategyResult:
        """Check exit conditions: RSI(2) > 70 OR max hold exceeded."""
        self._hold_days += 1
        exit_reason: Optional[str] = None

        # Catastrophe stop check
        if self._entry_price is not None:
            unrealized_loss = self._entry_price - last_close
            if unrealized_loss >= Decimal(str(self._cfg.stop_points)):
                exit_reason = (
                    f"Catastrophe stop: loss {float(unrealized_loss):.2f} pts "
                    f">= {self._cfg.stop_points} pts"
                )

        # RSI exit
        if exit_reason is None and float(rsi_value) > self._cfg.rsi_exit_threshold:
            exit_reason = (
                f"RSI(2) {float(rsi_value):.2f} > {self._cfg.rsi_exit_threshold} "
                f"- mean reverted"
            )

        # Max hold days
        if exit_reason is None and self._hold_days >= self._cfg.max_hold_days:
            exit_reason = (
                f"Max hold {self._cfg.max_hold_days} days reached "
                f"(held {self._hold_days} days)"
            )

        if exit_reason is None:
            return self.reject(
                f"Holding: RSI(2)={float(rsi_value):.2f}, "
                f"day {self._hold_days}/{self._cfg.max_hold_days}"
            )

        # Generate EXIT signal
        spec = CONTRACTS.get(self._symbol)
        tick_size = spec.tick_size if spec else Decimal("0.25")

        entry_price = self._entry_price or last_close
        stop_price = entry_price  # already exiting
        target_price = last_close  # exiting at current price

        risk_ticks = int(abs(last_close - entry_price) / tick_size)
        reward_ticks = risk_ticks

        dummy_or = self._make_dummy_or_range(last_close)

        setup = ORBSetup(
            symbol=self._symbol,
            direction=Direction.LONG,
            setup_type=SetupType.RSI2_EXIT,
            entry_price=last_close,
            stop_price=stop_price,
            target_price=target_price,
            risk_ticks=max(risk_ticks, 1),
            reward_ticks=max(reward_ticks, 1),
            or_range=dummy_or,
            confidence=Decimal("0.5"),
        )

        logger.info(
            "RSI2 EXIT SIGNAL: %s SELL @ %.2f | %s | "
            "entry was %.2f | held %d days",
            self._symbol, float(last_close), exit_reason,
            float(entry_price), self._hold_days,
        )

        self.reset_position()

        return StrategyResult(
            has_setup=True, setup=setup, reason=f"RSI2 exit: {exit_reason}"
        )

    # =========================================================================
    # Indicator Calculations
    # =========================================================================

    @staticmethod
    def _compute_rsi(closes: list[Decimal], period: int) -> Optional[Decimal]:
        """Compute Wilder RSI over the last `period` changes.

        Uses the standard Wilder smoothing method:
        1. Seed with SMA of first `period` gains/losses
        2. Smooth subsequent values: avg = (prev_avg * (period-1) + current) / period

        Args:
            closes: Full list of closing prices, oldest first.
            period: RSI lookback period (e.g., 2 for Connors RSI).

        Returns:
            RSI value as Decimal, or None if insufficient data.
        """
        if len(closes) < period + 1:
            return None

        # Calculate all price changes
        changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

        # Seed: SMA of first `period` gains and losses
        seed_changes = changes[:period]
        avg_gain = sum(max(c, Decimal("0")) for c in seed_changes) / Decimal(str(period))
        avg_loss = sum(max(-c, Decimal("0")) for c in seed_changes) / Decimal(str(period))

        # Smooth through remaining changes
        p = Decimal(str(period))
        for change in changes[period:]:
            gain = max(change, Decimal("0"))
            loss = max(-change, Decimal("0"))
            avg_gain = (avg_gain * (p - Decimal("1")) + gain) / p
            avg_loss = (avg_loss * (p - Decimal("1")) + loss) / p

        if avg_loss == Decimal("0"):
            return Decimal("100")

        rs = avg_gain / avg_loss
        rsi = Decimal("100") - (Decimal("100") / (Decimal("1") + rs))
        return rsi

    @staticmethod
    def _compute_sma(closes: list[Decimal], period: int) -> Optional[Decimal]:
        """Compute simple moving average of the last `period` closes.

        Args:
            closes: Full list of closing prices, oldest first.
            period: SMA lookback period (e.g., 200).

        Returns:
            SMA value as Decimal, or None if insufficient data.
        """
        if len(closes) < period:
            return None
        window = closes[-period:]
        return sum(window) / Decimal(str(period))

    # =========================================================================
    # Helpers
    # =========================================================================

    def _make_dummy_or_range(self, price: Decimal) -> ORBRange:
        """Create a placeholder ORBRange for ORBSetup compatibility.

        The RSI2 strategy doesn't use opening ranges, but ORBSetup
        requires one. This creates a minimal valid instance.
        """
        from datetime import timezone

        return ORBRange(
            symbol=self._symbol,
            or_high=price,
            or_low=price,
            midpoint=price,
            range_ticks=0,
            volume=Decimal("0"),
            vwap=price,
            timestamp=datetime.now(timezone.utc),
            valid=False,  # mark as non-OR to distinguish
        )

    @property
    def in_position(self) -> bool:
        """Whether the strategy currently has an open position."""
        return self._in_position

    @property
    def hold_days(self) -> int:
        """Number of trading days the current position has been held."""
        return self._hold_days
