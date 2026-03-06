"""
EMA Momentum Backtest Simulator
=================================
Intraday EMA crossover strategy on 1-min futures bars.

Strategy logic (adapted from crypto EMA momentum):
- EMA-9 / EMA-21 crossover on 1-min close prices
- RSI-14 filter for overbought/oversold avoidance
- ATR-based stop loss, reward:risk ratio for take profit
- RTH only (9:30-15:45 ET), EOD flatten

Independent from ORBSimulator - fresh implementation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional

from ..core.contracts import CONTRACTS, FuturesSpec
from ..core.enums import Direction

logger = logging.getLogger(__name__)


# =========================================================================
# EMA Strategy Configuration
# =========================================================================

@dataclass
class EMAStrategyConfig:
    """Configuration for the EMA momentum strategy."""

    # EMA periods
    ema_fast: int = 9
    ema_slow: int = 21

    # RSI filter
    rsi_period: int = 14
    rsi_long_min: float = 30.0
    rsi_long_max: float = 65.0
    rsi_short_min: float = 35.0
    rsi_short_max: float = 70.0

    # Stops & targets
    atr_period: int = 14
    atr_stop_multiplier: float = 2.0
    reward_risk_ratio: float = 1.5

    # Session limits
    max_trades_per_day: int = 4
    max_entry_time: str = "15:00"
    eod_flatten_time: str = "15:45"

    # Direction
    allow_short: bool = True

    # Risk / sizing
    max_risk_per_trade_usd: float = 500.0
    max_contracts_per_trade: int = 2
    commission_per_contract: float = 1.24  # round-trip
    slippage_ticks: int = 1
    account_size: float = 10_000.0

    # Symbols
    symbols: list = field(default_factory=lambda: ["MES", "MNQ"])

    # Backtest
    lookback_days: int = 90
    cache_dir: str = "ib_bot/backtesting/cache"


# =========================================================================
# Running indicator state (per-symbol, per-day)
# =========================================================================

class IndicatorState:
    """Maintains running EMA, RSI, and ATR calculations on 1-min bars."""

    def __init__(self, cfg: EMAStrategyConfig) -> None:
        self.cfg = cfg
        self.bar_count: int = 0

        # EMA state
        self.ema_fast: Optional[Decimal] = None
        self.ema_slow: Optional[Decimal] = None
        self._ema_fast_k = Decimal("2") / (Decimal(str(cfg.ema_fast)) + Decimal("1"))
        self._ema_slow_k = Decimal("2") / (Decimal(str(cfg.ema_slow)) + Decimal("1"))

        # Warmup buffers (collect closes until we have enough for initial SMA)
        self._closes: List[Decimal] = []

        # RSI state
        self._prev_close: Optional[Decimal] = None
        self._avg_gain: Optional[Decimal] = None
        self._avg_loss: Optional[Decimal] = None
        self._rsi_warmup_gains: List[Decimal] = []
        self._rsi_warmup_losses: List[Decimal] = []
        self.rsi: Optional[Decimal] = None

        # ATR state
        self._prev_bar_close: Optional[Decimal] = None
        self._atr_warmup: List[Decimal] = []
        self.atr: Optional[Decimal] = None

        # Previous EMA values (for crossover detection)
        self.prev_ema_fast: Optional[Decimal] = None
        self.prev_ema_slow: Optional[Decimal] = None

    def update(self, h: Decimal, l: Decimal, c: Decimal) -> None:
        """Update all indicators with a new 1-min bar."""
        self.bar_count += 1
        self._closes.append(c)

        # --- EMA update ---
        self.prev_ema_fast = self.ema_fast
        self.prev_ema_slow = self.ema_slow

        if self.bar_count <= self.cfg.ema_slow:
            # Still warming up - compute SMA when we have enough bars
            if self.bar_count == self.cfg.ema_fast:
                # Initialize fast EMA with SMA
                sma = sum(self._closes[-self.cfg.ema_fast:]) / Decimal(str(self.cfg.ema_fast))
                self.ema_fast = sma
            elif self.bar_count > self.cfg.ema_fast and self.ema_fast is not None:
                self.ema_fast = c * self._ema_fast_k + self.ema_fast * (Decimal("1") - self._ema_fast_k)

            if self.bar_count == self.cfg.ema_slow:
                sma = sum(self._closes[-self.cfg.ema_slow:]) / Decimal(str(self.cfg.ema_slow))
                self.ema_slow = sma
        else:
            # Normal EMA update
            if self.ema_fast is not None:
                self.ema_fast = c * self._ema_fast_k + self.ema_fast * (Decimal("1") - self._ema_fast_k)
            if self.ema_slow is not None:
                self.ema_slow = c * self._ema_slow_k + self.ema_slow * (Decimal("1") - self._ema_slow_k)

        # --- RSI update ---
        if self._prev_close is not None:
            delta = c - self._prev_close
            gain = max(delta, Decimal("0"))
            loss = max(-delta, Decimal("0"))

            if self._avg_gain is None:
                # Warmup phase
                self._rsi_warmup_gains.append(gain)
                self._rsi_warmup_losses.append(loss)

                if len(self._rsi_warmup_gains) == self.cfg.rsi_period:
                    self._avg_gain = sum(self._rsi_warmup_gains) / Decimal(str(self.cfg.rsi_period))
                    self._avg_loss = sum(self._rsi_warmup_losses) / Decimal(str(self.cfg.rsi_period))
                    self._compute_rsi()
            else:
                # Smoothed update
                period = Decimal(str(self.cfg.rsi_period))
                self._avg_gain = (self._avg_gain * (period - Decimal("1")) + gain) / period
                self._avg_loss = (self._avg_loss * (period - Decimal("1")) + loss) / period
                self._compute_rsi()

        self._prev_close = c

        # --- ATR update ---
        if self._prev_bar_close is not None:
            tr = max(
                h - l,
                abs(h - self._prev_bar_close),
                abs(l - self._prev_bar_close),
            )

            if self.atr is None:
                self._atr_warmup.append(tr)
                if len(self._atr_warmup) == self.cfg.atr_period:
                    self.atr = sum(self._atr_warmup) / Decimal(str(self.cfg.atr_period))
            else:
                alpha = Decimal("1") / Decimal(str(self.cfg.atr_period))
                self.atr = alpha * tr + (Decimal("1") - alpha) * self.atr

        self._prev_bar_close = c

    def _compute_rsi(self) -> None:
        """Compute RSI from current avg_gain / avg_loss."""
        if self._avg_loss is not None and self._avg_loss > Decimal("0"):
            rs = self._avg_gain / self._avg_loss
            self.rsi = Decimal("100") - (Decimal("100") / (Decimal("1") + rs))
        else:
            self.rsi = Decimal("100")  # No losses = RSI 100

    def is_ready(self) -> bool:
        """Check if all indicators have warmed up."""
        return (
            self.ema_fast is not None
            and self.ema_slow is not None
            and self.prev_ema_fast is not None
            and self.prev_ema_slow is not None
            and self.rsi is not None
            and self.atr is not None
        )

    def has_bullish_cross(self) -> bool:
        """EMA fast crossed above EMA slow this bar."""
        if not self.is_ready():
            return False
        return (
            self.prev_ema_fast <= self.prev_ema_slow  # type: ignore
            and self.ema_fast > self.ema_slow  # type: ignore
        )

    def has_bearish_cross(self) -> bool:
        """EMA fast crossed below EMA slow this bar."""
        if not self.is_ready():
            return False
        return (
            self.prev_ema_fast >= self.prev_ema_slow  # type: ignore
            and self.ema_fast < self.ema_slow  # type: ignore
        )


# =========================================================================
# Daily risk tracker
# =========================================================================

class EMADailyRisk:
    """Track daily risk for EMA strategy."""

    def __init__(self, cfg: EMAStrategyConfig) -> None:
        self.cfg = cfg
        self.trade_count: int = 0
        self.positions: Dict[str, Dict[str, Any]] = {}

    def can_trade(self, symbol: str) -> bool:
        if symbol in self.positions:
            return False
        if self.trade_count >= self.cfg.max_trades_per_day:
            return False
        return True

    def has_position(self, symbol: str) -> bool:
        return symbol in self.positions

    def open_position(
        self,
        symbol: str,
        direction: Direction,
        entry_price: Decimal,
        stop_price: Decimal,
        target_price: Decimal,
        contracts: int,
        bar_time: Any,
    ) -> None:
        self.positions[symbol] = {
            "direction": direction,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "target_price": target_price,
            "contracts": contracts,
            "entry_time": bar_time,
        }

    def close_position(
        self,
        symbol: str,
        exit_price: Decimal,
        reason: str,
        exit_time: Any,
        spec: FuturesSpec,
        cfg: EMAStrategyConfig,
    ) -> Dict[str, Any]:
        pos = self.positions.pop(symbol)
        trade = _calculate_pnl(pos, exit_price, reason, exit_time, spec, cfg)
        self.trade_count += 1
        return trade


# =========================================================================
# P&L calculation
# =========================================================================

def _calculate_pnl(
    pos: Dict[str, Any],
    exit_price: Decimal,
    reason: str,
    exit_time: Any,
    spec: FuturesSpec,
    cfg: EMAStrategyConfig,
) -> Dict[str, Any]:
    """Calculate P&L for a futures trade."""
    direction = pos["direction"]
    entry = pos["entry_price"]
    contracts: int = pos["contracts"]

    if direction == Direction.LONG:
        price_diff = exit_price - entry
    else:
        price_diff = entry - exit_price

    ticks = price_diff / spec.tick_size
    gross_pnl = float(ticks * spec.tick_value * contracts)
    commission = cfg.commission_per_contract * contracts  # already round-trip
    net_pnl = gross_pnl - commission

    return {
        "symbol": spec.symbol,
        "direction": "LONG" if direction == Direction.LONG else "SHORT",
        "entry": float(entry),
        "exit": float(exit_price),
        "contracts": contracts,
        "ticks": float(ticks),
        "gross_pnl": gross_pnl,
        "commission": commission,
        "net_pnl": net_pnl,
        "reason": reason,
        "entry_time": pos["entry_time"],
        "exit_time": exit_time,
    }


# =========================================================================
# Position sizing
# =========================================================================

def _size_trade(
    stop_distance: Decimal,
    spec: FuturesSpec,
    cfg: EMAStrategyConfig,
) -> int:
    """Position sizing: floor(max_risk / (stop_ticks * tick_value))."""
    stop_ticks = stop_distance / spec.tick_size
    if stop_ticks <= 0:
        return 0
    risk_per_contract = stop_ticks * spec.tick_value
    contracts = int(
        (Decimal(str(cfg.max_risk_per_trade_usd)) / risk_per_contract)
        .to_integral_value(rounding=ROUND_DOWN)
    )
    return min(max(contracts, 0), cfg.max_contracts_per_trade)


# =========================================================================
# Core EMA Simulator
# =========================================================================

class EMASimulator:
    """Intraday EMA crossover backtest simulator.

    Walks 1-min bars day-by-day, computing running EMA-9/21, RSI-14, ATR-14.
    Generates entries on EMA crossovers with RSI filter.
    Exits via bracket orders (ATR-based SL, R:R-based TP) or EOD flatten.
    """

    def __init__(self, cfg: EMAStrategyConfig) -> None:
        self.cfg = cfg
        self.trades: List[Dict[str, Any]] = []
        self.equity_curve: List[float] = []
        self.daily_results: List[Dict[str, Any]] = []

    def run(
        self,
        bars_by_day: Dict[str, Dict[str, List[Dict[str, Any]]]],
    ) -> None:
        """Run simulation over all days.

        Args:
            bars_by_day: {date_str: {symbol: [bar_dicts]}}
                         bar_dict keys: dt, o, h, l, c, v (Decimal prices)
        """
        equity = self.cfg.account_size
        self.equity_curve = [equity]

        for date_str in sorted(bars_by_day.keys()):
            day_bars = bars_by_day[date_str]
            day_trades = self._simulate_day(day_bars)

            day_pnl = sum(t["net_pnl"] for t in day_trades)
            equity += day_pnl
            self.equity_curve.append(equity)

            self.trades.extend(day_trades)
            self.daily_results.append({
                "date": date_str,
                "trades": len(day_trades),
                "pnl": day_pnl,
                "equity": equity,
            })

            if day_trades:
                logger.info(
                    "Day %s: %d trades, P&L=$%.2f, equity=$%.2f",
                    date_str, len(day_trades), day_pnl, equity,
                )

    def _simulate_day(
        self,
        day_bars: Dict[str, List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        """Simulate one trading day across all symbols."""
        risk = EMADailyRisk(self.cfg)
        trades: List[Dict[str, Any]] = []

        for symbol in self.cfg.symbols:
            bars = day_bars.get(symbol, [])
            if not bars:
                continue

            spec = CONTRACTS.get(symbol)
            if not spec:
                continue

            symbol_trades = self._simulate_symbol_day(
                symbol, bars, spec, risk,
            )
            trades.extend(symbol_trades)

        return trades

    def _simulate_symbol_day(
        self,
        symbol: str,
        bars: List[Dict[str, Any]],
        spec: FuturesSpec,
        risk: EMADailyRisk,
    ) -> List[Dict[str, Any]]:
        """Simulate one symbol for one day with EMA crossover strategy."""
        trades: List[Dict[str, Any]] = []
        indicators = IndicatorState(self.cfg)

        rth_start = time(9, 30)
        max_entry = time.fromisoformat(self.cfg.max_entry_time)
        eod_flatten = time.fromisoformat(self.cfg.eod_flatten_time)

        for bar in bars:
            # Coerce prices to Decimal
            h = Decimal(str(bar["h"])) if not isinstance(bar["h"], Decimal) else bar["h"]
            l = Decimal(str(bar["l"])) if not isinstance(bar["l"], Decimal) else bar["l"]
            c = Decimal(str(bar["c"])) if not isinstance(bar["c"], Decimal) else bar["c"]

            bar_time = bar["dt"]
            t = bar_time.time()

            # Only process bars during RTH
            if t < rth_start:
                continue

            # Update indicators
            indicators.update(h, l, c)

            # --- Check exits FIRST ---
            if risk.has_position(symbol):
                exit_result = self._check_exit(risk.positions[symbol], bar, spec)
                if exit_result:
                    trade = risk.close_position(
                        symbol, exit_result["price"], exit_result["reason"],
                        bar_time, spec, self.cfg,
                    )
                    trades.append(trade)
                elif t >= eod_flatten:
                    trade = risk.close_position(
                        symbol, c, "EOD", bar_time, spec, self.cfg,
                    )
                    trades.append(trade)

            # --- Check entries ---
            if t >= max_entry or t < rth_start:
                continue
            if risk.has_position(symbol):
                continue
            if not risk.can_trade(symbol):
                continue
            if not indicators.is_ready():
                continue

            # Check for EMA crossover signals
            entry_signal = self._check_entry(indicators, spec, c)
            if entry_signal is not None:
                direction = entry_signal["direction"]

                # RSI filter
                rsi_val = float(indicators.rsi)  # type: ignore
                if direction == Direction.LONG:
                    if not (self.cfg.rsi_long_min <= rsi_val <= self.cfg.rsi_long_max):
                        continue
                else:
                    if not self.cfg.allow_short:
                        continue
                    if not (self.cfg.rsi_short_min <= rsi_val <= self.cfg.rsi_short_max):
                        continue

                # Calculate stop and target
                atr = indicators.atr  # type: ignore
                stop_distance = atr * Decimal(str(self.cfg.atr_stop_multiplier))

                # Position sizing
                contracts = _size_trade(stop_distance, spec, self.cfg)
                if contracts <= 0:
                    continue

                # Entry slippage
                slip = Decimal(str(self.cfg.slippage_ticks)) * spec.tick_size
                if direction == Direction.LONG:
                    entry_price = c + slip
                    stop_price = entry_price - stop_distance
                    target_price = entry_price + stop_distance * Decimal(str(self.cfg.reward_risk_ratio))
                else:
                    entry_price = c - slip
                    stop_price = entry_price + stop_distance
                    target_price = entry_price - stop_distance * Decimal(str(self.cfg.reward_risk_ratio))

                risk.open_position(
                    symbol=symbol,
                    direction=direction,
                    entry_price=entry_price,
                    stop_price=stop_price,
                    target_price=target_price,
                    contracts=contracts,
                    bar_time=bar_time,
                )

                logger.debug(
                    "EMA ENTRY: %s %s @ %.2f (SL=%.2f TP=%.2f ATR=%.2f RSI=%.1f) x%d",
                    direction.value, symbol,
                    float(entry_price), float(stop_price),
                    float(target_price), float(atr), rsi_val, contracts,
                )

        return trades

    def _check_entry(
        self,
        indicators: IndicatorState,
        spec: FuturesSpec,
        close: Decimal,
    ) -> Optional[Dict[str, Any]]:
        """Check for EMA crossover entry signal."""
        if indicators.has_bullish_cross():
            return {"direction": Direction.LONG}
        if indicators.has_bearish_cross():
            return {"direction": Direction.SHORT}
        return None

    def _check_exit(
        self,
        pos: Dict[str, Any],
        bar: Dict[str, Any],
        spec: FuturesSpec,
    ) -> Optional[Dict[str, Any]]:
        """Check if bar triggers TP or SL.

        Conservative: if both could trigger in the same bar, SL fills first.
        """
        h = Decimal(str(bar["h"])) if not isinstance(bar["h"], Decimal) else bar["h"]
        l = Decimal(str(bar["l"])) if not isinstance(bar["l"], Decimal) else bar["l"]
        slip = Decimal(str(self.cfg.slippage_ticks)) * spec.tick_size

        stop = pos["stop_price"]
        target = pos["target_price"]

        if pos["direction"] == Direction.LONG:
            if l <= stop:
                return {"price": stop - slip, "reason": "SL"}
            if h >= target:
                return {"price": target - slip, "reason": "TP"}
        else:  # SHORT
            if h >= stop:
                return {"price": stop + slip, "reason": "SL"}
            if l <= target:
                return {"price": target + slip, "reason": "TP"}

        return None
