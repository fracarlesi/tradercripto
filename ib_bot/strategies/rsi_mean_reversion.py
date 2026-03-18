"""
RSI Mean Reversion Strategy (Intraday)
========================================

5-minute bar RSI(14) mean reversion for MES futures.

Entry Rules:
  LONG:  RSI(14) < 25 on 5-min bars
  SHORT: RSI(14) > 75 on 5-min bars

Exit Rules:
  LONG exit:  RSI(14) crosses above 50
  SHORT exit: RSI(14) crosses below 50

Stop: 6 points fixed ($30 per MES contract)

Filters:
  - Trading hours: 10:00-15:30 ET only (skip open volatility + close)
  - Max 1 position at a time
  - Max 4 trades per day
  - Does NOT enter if RSI(2) Connors or ORB has an open position

Backtest: PF 1.27, ~1.9 trades/day.
"""

import logging
from datetime import time
from decimal import Decimal
from typing import Optional

from .base import BaseStrategy, StrategyResult
from ..config.loader import RSIMeanReversionConfig
from ..core.contracts import CONTRACTS
from ..core.enums import Direction, SetupType
from ..core.models import FuturesMarketState, ORBRange, ORBSetup

logger = logging.getLogger(__name__)


class _RSIState:
    """Running RSI(14) state for incremental 5-min bar updates.

    Uses Wilder's smoothing method (same as standard RSI).
    """

    def __init__(self, period: int) -> None:
        self._period = period
        self._prev_close: Optional[Decimal] = None
        self._avg_gain: Optional[Decimal] = None
        self._avg_loss: Optional[Decimal] = None
        self._warmup_gains: list[Decimal] = []
        self._warmup_losses: list[Decimal] = []
        self.rsi: Optional[Decimal] = None
        self.prev_rsi: Optional[Decimal] = None
        self.bar_count: int = 0

    def update(self, close: Decimal) -> None:
        """Update RSI with a new bar close price."""
        self.bar_count += 1
        self.prev_rsi = self.rsi

        if self._prev_close is not None:
            delta = close - self._prev_close
            gain = max(delta, Decimal("0"))
            loss = max(-delta, Decimal("0"))

            if self._avg_gain is None:
                # Warmup phase: collecting initial values
                self._warmup_gains.append(gain)
                self._warmup_losses.append(loss)

                if len(self._warmup_gains) == self._period:
                    period_d = Decimal(str(self._period))
                    self._avg_gain = sum(self._warmup_gains) / period_d
                    self._avg_loss = sum(self._warmup_losses) / period_d
                    self._compute_rsi()
            else:
                # Wilder's smoothing
                period_d = Decimal(str(self._period))
                self._avg_gain = (self._avg_gain * (period_d - Decimal("1")) + gain) / period_d
                self._avg_loss = (self._avg_loss * (period_d - Decimal("1")) + loss) / period_d
                self._compute_rsi()

        self._prev_close = close

    def _compute_rsi(self) -> None:
        if self._avg_loss is not None and self._avg_loss > Decimal("0"):
            rs = self._avg_gain / self._avg_loss  # type: ignore[operator]
            self.rsi = Decimal("100") - (Decimal("100") / (Decimal("1") + rs))
        else:
            self.rsi = Decimal("100")

    def is_ready(self) -> bool:
        """RSI is ready when we have at least period+1 bars."""
        return self.rsi is not None and self.prev_rsi is not None

    def crosses_above(self, level: Decimal) -> bool:
        """Check if RSI crossed above a level on this bar."""
        if not self.is_ready():
            return False
        return self.prev_rsi <= level < self.rsi  # type: ignore[operator]

    def crosses_below(self, level: Decimal) -> bool:
        """Check if RSI crossed below a level on this bar."""
        if not self.is_ready():
            return False
        return self.prev_rsi >= level > self.rsi  # type: ignore[operator]


class RSIMeanReversionStrategy(BaseStrategy):
    """Intraday RSI(14) mean reversion on 5-minute bars.

    Designed to run alongside the primary ORB/Connors strategy.
    Self-manages position state (max 1 at a time) and daily trade count.

    The strategy produces entry signals (RSI_MR_LONG/SHORT) and exit
    signals (RSI_MR_EXIT_LONG/SHORT). The caller (main.py) is
    responsible for checking that no other strategy has an open position
    before forwarding entry signals to execution.
    """

    def __init__(
        self,
        rsi_mr_config: RSIMeanReversionConfig,
    ) -> None:
        super().__init__(config={})
        self._cfg = rsi_mr_config
        # Per-symbol RSI state
        self._indicators: dict[str, _RSIState] = {}
        # Daily trade counter
        self._daily_trade_count: int = 0
        # Current open position direction (None = flat)
        self._position_direction: Optional[Direction] = None
        # Entry price for stop calculation
        self._entry_price: Optional[Decimal] = None

    @property
    def name(self) -> str:
        return "rsi_mean_reversion"

    @property
    def has_position(self) -> bool:
        """Whether this strategy currently has an open position."""
        return self._position_direction is not None

    def reset_daily(self) -> None:
        """Reset indicator state and trade counts for a new trading day."""
        self._indicators.clear()
        self._daily_trade_count = 0
        self._position_direction = None
        self._entry_price = None

    def record_entry(self, direction: Direction, entry_price: Decimal) -> None:
        """Record that a position was opened by this strategy."""
        self._position_direction = direction
        self._entry_price = entry_price

    def record_exit(self) -> None:
        """Record that the position was closed."""
        self._position_direction = None
        self._entry_price = None

    def _get_indicators(self, symbol: str) -> _RSIState:
        """Get or create RSI state for a symbol."""
        if symbol not in self._indicators:
            self._indicators[symbol] = _RSIState(period=self._cfg.rsi_period)
        return self._indicators[symbol]

    def _in_time_window(self, state: FuturesMarketState) -> bool:
        """Check if current time is within the allowed trading window."""
        try:
            start = time.fromisoformat(self._cfg.start_time)
            end = time.fromisoformat(self._cfg.end_time)
            if hasattr(state.timestamp, "time"):
                current = state.timestamp.time()
                return start <= current <= end
        except Exception:
            pass
        return False

    def evaluate(
        self,
        state: FuturesMarketState,
        or_range: ORBRange,
    ) -> StrategyResult:
        """Evaluate RSI mean reversion signal on 5-min bar update.

        Updates internal RSI state, then checks for:
        1. Exit signals if we have an open position
        2. Entry signals if we're flat

        Args:
            state: Current market state with latest 5-min bar close.
            or_range: Opening Range (not used by this strategy, kept for interface).

        Returns:
            StrategyResult with setup if entry or exit signal detected.
        """
        spec = CONTRACTS.get(state.symbol)
        if not spec:
            return self.reject(f"Unknown contract: {state.symbol}")

        # Update RSI with current price
        indicators = self._get_indicators(state.symbol)
        indicators.update(state.last_price)

        if not indicators.is_ready():
            return self.reject("RSI warming up")

        rsi_val = float(indicators.rsi)  # type: ignore[arg-type]
        exit_level = Decimal(str(self._cfg.rsi_exit))

        # --- EXIT LOGIC (check first, always) ---
        if self._position_direction is not None:
            if self._position_direction == Direction.LONG:
                # Exit long when RSI crosses above 50
                if indicators.crosses_above(exit_level):
                    return self._build_exit_setup(
                        state=state,
                        or_range=or_range,
                        direction=Direction.LONG,
                        spec=spec,
                        rsi=rsi_val,
                    )
                # Check fixed stop
                if self._entry_price is not None:
                    stop_price = self._entry_price - Decimal(str(self._cfg.stop_points))
                    if state.last_price <= stop_price:
                        return self._build_exit_setup(
                            state=state,
                            or_range=or_range,
                            direction=Direction.LONG,
                            spec=spec,
                            rsi=rsi_val,
                            reason="stop_hit",
                        )
            elif self._position_direction == Direction.SHORT:
                # Exit short when RSI crosses below 50
                if indicators.crosses_below(exit_level):
                    return self._build_exit_setup(
                        state=state,
                        or_range=or_range,
                        direction=Direction.SHORT,
                        spec=spec,
                        rsi=rsi_val,
                    )
                # Check fixed stop
                if self._entry_price is not None:
                    stop_price = self._entry_price + Decimal(str(self._cfg.stop_points))
                    if state.last_price >= stop_price:
                        return self._build_exit_setup(
                            state=state,
                            or_range=or_range,
                            direction=Direction.SHORT,
                            spec=spec,
                            rsi=rsi_val,
                            reason="stop_hit",
                        )

            # Position open but no exit signal
            return self.reject(f"Position open, RSI={rsi_val:.1f}, no exit signal")

        # --- ENTRY LOGIC (only when flat) ---

        # Time window check
        if not self._in_time_window(state):
            return self.reject(f"Outside trading hours ({self._cfg.start_time}-{self._cfg.end_time})")

        # Daily trade limit
        if self._daily_trade_count >= self._cfg.max_daily_trades:
            return self.reject(
                f"Daily trade limit reached: {self._daily_trade_count}/{self._cfg.max_daily_trades}"
            )

        # Check RSI entry thresholds
        direction: Optional[Direction] = None
        if rsi_val < self._cfg.rsi_entry_long:
            direction = Direction.LONG
        elif rsi_val > self._cfg.rsi_entry_short:
            direction = Direction.SHORT

        if direction is None:
            return self.reject(f"No RSI signal: RSI={rsi_val:.1f}")

        return self._build_entry_setup(
            state=state,
            or_range=or_range,
            direction=direction,
            spec=spec,
            rsi=rsi_val,
        )

    def _build_entry_setup(
        self,
        state: FuturesMarketState,
        or_range: ORBRange,
        direction: Direction,
        spec: object,
        rsi: float,
    ) -> StrategyResult:
        """Build entry setup with fixed-point stop."""
        tick_size = getattr(spec, "tick_size", Decimal("0.25"))
        entry_price = state.last_price
        stop_distance = Decimal(str(self._cfg.stop_points))

        if direction == Direction.LONG:
            stop_price = entry_price - stop_distance
            # Target: RSI mean reversion to 50 (approximate 1:1 R:R for conservative target)
            target_price = entry_price + stop_distance
            setup_type = SetupType.RSI_MR_LONG
        else:
            stop_price = entry_price + stop_distance
            target_price = entry_price - stop_distance
            setup_type = SetupType.RSI_MR_SHORT

        risk_ticks = int(stop_distance / tick_size) if tick_size > 0 else 0
        reward_ticks = risk_ticks  # 1:1 as baseline; actual exit is RSI-based

        if risk_ticks <= 0:
            return self.reject("Invalid risk: 0 ticks")

        # Confidence based on RSI extremity
        if direction == Direction.LONG:
            # Lower RSI = higher confidence
            confidence = min(Decimal("1.0"), Decimal(str((self._cfg.rsi_entry_long - rsi) / self._cfg.rsi_entry_long + 0.5)))
        else:
            # Higher RSI = higher confidence
            confidence = min(Decimal("1.0"), Decimal(str((rsi - self._cfg.rsi_entry_short) / (100 - self._cfg.rsi_entry_short) + 0.5)))
        confidence = max(Decimal("0.1"), confidence)

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
            "RSI_MR %s entry: %s price=%.2f stop=%.2f target=%.2f "
            "RSI=%.1f trades=%d/%d",
            direction.value, state.symbol,
            float(entry_price), float(stop_price), float(target_price),
            rsi, self._daily_trade_count, self._cfg.max_daily_trades,
        )

        return StrategyResult(has_setup=True, setup=setup)

    def _build_exit_setup(
        self,
        state: FuturesMarketState,
        or_range: ORBRange,
        direction: Direction,
        spec: object,
        rsi: float,
        reason: str = "rsi_exit",
    ) -> StrategyResult:
        """Build exit signal setup."""
        tick_size = getattr(spec, "tick_size", Decimal("0.25"))
        exit_price = state.last_price

        if direction == Direction.LONG:
            setup_type = SetupType.RSI_MR_EXIT_LONG
        else:
            setup_type = SetupType.RSI_MR_EXIT_SHORT

        # For exit signals, stop/target are at exit price (immediate close)
        setup = ORBSetup(
            symbol=state.symbol,
            direction=direction,
            setup_type=setup_type,
            entry_price=exit_price,
            stop_price=exit_price,
            target_price=exit_price,
            risk_ticks=0,
            reward_ticks=0,
            or_range=or_range,
            confidence=Decimal("1.0"),
        )

        pnl_str = ""
        if self._entry_price is not None:
            if direction == Direction.LONG:
                pnl_pts = float(exit_price - self._entry_price)
            else:
                pnl_pts = float(self._entry_price - exit_price)
            pnl_str = f" P&L={pnl_pts:+.2f}pts"

        logger.info(
            "RSI_MR %s exit (%s): %s price=%.2f RSI=%.1f%s",
            direction.value, reason, state.symbol,
            float(exit_price), rsi, pnl_str,
        )

        return StrategyResult(has_setup=True, setup=setup)
