"""
ORB Backtest Simulator
======================
Session-by-session simulation of Opening Range Breakout strategy.
Reuses live bot's ORBStrategy, VWAPCalculator, ATRCalculator.
"""

from __future__ import annotations

import logging
from datetime import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional

from ..core.contracts import CONTRACTS, FuturesSpec
from ..core.enums import Direction, SessionPhase
from ..core.models import FuturesMarketState
from ..strategies.orb import ORBStrategy
from ..services.market_data import VWAPCalculator, ATRCalculator
from ..config.loader import StrategyConfig, StopsConfig
from .config import IBBacktestConfig

logger = logging.getLogger(__name__)


# =========================================================================
# Daily risk tracker (mirrors RiskManager + ExecutionEngine)
# =========================================================================

class DailyRiskState:
    """Track daily risk state (mirrors RiskManager + ExecutionEngine)."""

    def __init__(self, cfg: IBBacktestConfig) -> None:
        self.cfg = cfg
        self.daily_loss = Decimal("0")
        self.trade_count: int = 0
        self.consecutive_stops: int = 0
        self.positions: Dict[str, Dict[str, Any]] = {}  # symbol -> position
        self.stopped_directions: Dict[str, set] = {}  # symbol -> {Direction}

    def can_trade(self, symbol: str, direction: Direction) -> bool:
        """Check if a new trade is allowed given current risk state."""
        if symbol in self.positions:
            return False
        if self.trade_count >= self.cfg.max_trades_per_day:
            return False
        if self.consecutive_stops >= self.cfg.consecutive_stops_halt:
            return False
        if self.daily_loss >= Decimal(str(self.cfg.max_daily_loss_usd)):
            return False
        if self.cfg.no_reentry_after_stop:
            if direction in self.stopped_directions.get(symbol, set()):
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
        risk_ticks: int,
    ) -> None:
        self.positions[symbol] = {
            "direction": direction,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "target_price": target_price,
            "contracts": contracts,
            "entry_time": bar_time,
            "risk_ticks": risk_ticks,
        }

    def close_position(
        self,
        symbol: str,
        exit_price: Decimal,
        reason: str,
        exit_time: Any,
        spec: FuturesSpec,
        cfg: IBBacktestConfig,
    ) -> Dict[str, Any]:
        pos = self.positions.pop(symbol)
        trade = calculate_pnl(pos, exit_price, reason, exit_time, spec, cfg)

        self.trade_count += 1
        if trade["net_pnl"] < 0:
            self.daily_loss += abs(Decimal(str(trade["net_pnl"])))

        is_stop = reason == "SL"
        if is_stop:
            self.consecutive_stops += 1
            if symbol not in self.stopped_directions:
                self.stopped_directions[symbol] = set()
            self.stopped_directions[symbol].add(pos["direction"])
        else:
            self.consecutive_stops = 0

        return trade


# =========================================================================
# P&L calculation
# =========================================================================

def calculate_pnl(
    pos: Dict[str, Any],
    exit_price: Decimal,
    reason: str,
    exit_time: Any,
    spec: FuturesSpec,
    cfg: IBBacktestConfig,
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
    commission = cfg.commission_per_contract * contracts * 2  # round-trip
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

def size_trade(
    setup: Any,
    spec: FuturesSpec,
    cfg: IBBacktestConfig,
) -> int:
    """Position sizing: floor(max_risk / (risk_ticks * tick_value))."""
    risk_per_tick = spec.tick_value
    total_risk = Decimal(str(setup.risk_ticks)) * risk_per_tick
    if total_risk <= 0:
        return 0
    contracts = int(
        (Decimal(str(cfg.max_risk_per_trade_usd)) / total_risk)
        .to_integral_value(rounding=ROUND_DOWN)
    )
    return min(max(contracts, 0), cfg.max_contracts_per_trade)


# =========================================================================
# Core simulator
# =========================================================================

class ORBSimulator:
    """Session-by-session ORB backtest simulator.

    Walks 1-min bars day-by-day, reusing the live bot's ORBStrategy.evaluate()
    for signal generation. Bracket orders (TP/SL) are simulated bar-by-bar
    with conservative fill assumptions (SL fills before TP on ambiguous bars,
    adverse slippage on all fills).
    """

    def __init__(self, cfg: IBBacktestConfig) -> None:
        self.cfg = cfg
        self.trades: List[Dict[str, Any]] = []
        self.equity_curve: List[float] = []
        self.daily_results: List[Dict[str, Any]] = []

    def run(
        self,
        bars_by_day: Dict[str, Dict[str, List[Dict[str, Any]]]],
        or_detector_func: Any,
    ) -> None:
        """Run simulation over all days.

        Args:
            bars_by_day: {date_str: {symbol: [bar_dicts]}}
                         bar_dict keys: dt, o, h, l, c, v (Decimal prices)
            or_detector_func: function(bars, spec, cfg) -> Optional[ORBRange]
        """
        equity = self.cfg.account_size
        self.equity_curve = [equity]

        # Build StrategyConfig and StopsConfig from backtest config
        strategy_config = StrategyConfig(
            name="orb",
            breakout_buffer_ticks=self.cfg.breakout_buffer_ticks,
            vwap_confirmation=self.cfg.vwap_confirmation,
            min_atr_ticks=self.cfg.min_atr_ticks,
            max_entry_time=self.cfg.max_entry_time,
            allow_short=self.cfg.allow_short,
            no_reentry_after_stop=self.cfg.no_reentry_after_stop,
        )
        stops_config = StopsConfig(
            stop_type=self.cfg.stop_type,
            stop_buffer_ticks=self.cfg.stop_buffer_ticks,
            reward_risk_ratio=Decimal(str(self.cfg.reward_risk_ratio)),
            trailing_enabled=False,
            eod_flatten_time=self.cfg.eod_flatten_time,
        )
        strategy = ORBStrategy(strategy_config, stops_config)

        for date_str in sorted(bars_by_day.keys()):
            day_bars = bars_by_day[date_str]
            day_trades = self._simulate_day(day_bars, strategy, or_detector_func)

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
        strategy: ORBStrategy,
        or_detector_func: Any,
    ) -> List[Dict[str, Any]]:
        """Simulate one trading day across all symbols."""
        risk = DailyRiskState(self.cfg)
        trades: List[Dict[str, Any]] = []

        for symbol in self.cfg.symbols:
            bars = day_bars.get(symbol, [])
            if not bars:
                continue

            spec = CONTRACTS.get(symbol)
            if not spec:
                continue

            symbol_trades = self._simulate_symbol_day(
                symbol, bars, spec, strategy, or_detector_func, risk,
            )
            trades.extend(symbol_trades)

        return trades

    def _simulate_symbol_day(
        self,
        symbol: str,
        bars: List[Dict[str, Any]],
        spec: FuturesSpec,
        strategy: ORBStrategy,
        or_detector_func: Any,
        risk: DailyRiskState,
    ) -> List[Dict[str, Any]]:
        """Simulate one symbol for one day."""
        trades: List[Dict[str, Any]] = []

        # Phase 1: Detect Opening Range from the day's bars
        or_range = or_detector_func(bars, spec, self.cfg)
        if or_range is None or not or_range.valid:
            return trades

        # Phase 2: Walk bars after OR, checking exits then entries
        vwap_calc = VWAPCalculator()
        atr_calc = ATRCalculator()

        max_entry = time.fromisoformat(self.cfg.max_entry_time)
        eod_flatten = time.fromisoformat(self.cfg.eod_flatten_time)
        or_end = time.fromisoformat(self.cfg.or_end)

        for bar in bars:
            # Coerce to Decimal (bars from cache are already Decimal, but be safe)
            h = Decimal(str(bar["h"])) if not isinstance(bar["h"], Decimal) else bar["h"]
            l = Decimal(str(bar["l"])) if not isinstance(bar["l"], Decimal) else bar["l"]
            c = Decimal(str(bar["c"])) if not isinstance(bar["c"], Decimal) else bar["c"]
            v = Decimal(str(bar["v"])) if not isinstance(bar["v"], Decimal) else bar["v"]

            # Update indicators with every bar (warm up VWAP/ATR from session start)
            vwap = vwap_calc.update(h, l, c, v)
            atr = atr_calc.update(h, l, c)

            bar_time = bar["dt"]
            t = bar_time.time()

            # --- Check exits FIRST ---
            if risk.has_position(symbol):
                exit_result = self._check_exit(risk.positions[symbol], bar, spec)
                if exit_result:
                    trade = risk.close_position(
                        symbol, exit_result["price"], exit_result["reason"],
                        bar_time, spec, self.cfg,
                    )
                    trades.append(trade)

                # EOD flatten: close any remaining position
                elif t >= eod_flatten and risk.has_position(symbol):
                    trade = risk.close_position(
                        symbol, c, "EOD", bar_time, spec, self.cfg,
                    )
                    trades.append(trade)

            # --- Check entries (only during ACTIVE_TRADING, after OR) ---
            if t < or_end or t >= max_entry:
                continue
            if risk.has_position(symbol):
                continue
            if not risk.can_trade(symbol, Direction.LONG) and not risk.can_trade(symbol, Direction.SHORT):
                continue

            # Build market state and evaluate strategy
            state = FuturesMarketState(
                symbol=symbol,
                last_price=c,
                vwap=vwap,
                atr_14=atr,
                volume=v,
                session_phase=SessionPhase.ACTIVE_TRADING,
                timestamp=bar_time,
            )

            result = strategy.evaluate(state, or_range)
            if result.has_setup and result.setup:
                setup = result.setup
                if risk.can_trade(symbol, setup.direction):
                    contracts = size_trade(setup, spec, self.cfg)
                    if contracts > 0:
                        # Apply adverse entry slippage
                        slip = Decimal(str(self.cfg.slippage_ticks)) * spec.tick_size
                        if setup.direction == Direction.LONG:
                            adj_entry = setup.entry_price + slip
                        else:
                            adj_entry = setup.entry_price - slip

                        risk.open_position(
                            symbol=symbol,
                            direction=setup.direction,
                            entry_price=adj_entry,
                            stop_price=setup.stop_price,
                            target_price=setup.target_price,
                            contracts=contracts,
                            bar_time=bar_time,
                            risk_ticks=setup.risk_ticks,
                        )

                        logger.debug(
                            "ENTRY: %s %s @ %.2f (SL=%.2f TP=%.2f) x%d",
                            setup.direction.value, symbol,
                            float(adj_entry), float(setup.stop_price),
                            float(setup.target_price), contracts,
                        )

        return trades

    def _check_exit(
        self,
        pos: Dict[str, Any],
        bar: Dict[str, Any],
        spec: FuturesSpec,
    ) -> Optional[Dict[str, Any]]:
        """Check if bar triggers TP or SL (bracket order simulation).

        Conservative: if both could trigger in the same bar, SL fills first.
        Adverse slippage is applied to exit fills.
        """
        h = Decimal(str(bar["h"])) if not isinstance(bar["h"], Decimal) else bar["h"]
        l = Decimal(str(bar["l"])) if not isinstance(bar["l"], Decimal) else bar["l"]
        slip = Decimal(str(self.cfg.slippage_ticks)) * spec.tick_size

        stop = pos["stop_price"]
        target = pos["target_price"]

        if pos["direction"] == Direction.LONG:
            # Check SL first (conservative assumption)
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
