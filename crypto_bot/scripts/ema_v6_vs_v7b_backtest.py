"""
EMA High Signal — v6 vs v7b Backtest Comparison
================================================
Compares two versions of the EMA High breakout strategy:
  - v6: LONG only, fixed 5-bar exit, SL at candle low
  - v7b: LONG only, aggressive trailing SL from +0.5R, strength exit 3R,
         violation exit (close < EMA4 High after 1R), max hold 8 bars

Usage:
    python -m crypto_bot.scripts.ema_v6_vs_v7b_backtest \
        --daily --strategy-a-only
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from crypto_bot.services.market_state import calculate_ema
from crypto_bot.scripts.squeeze_strategy_compare import (
    fetch_or_load_candles,
    compute_ema_high_signal,
    SqueezeCompareConfig,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class BacktestConfig:
    symbols: list[str] = field(default_factory=lambda: [
        "BTC", "ETH", "SOL", "LINK", "BNB", "ADA", "AVAX", "ARB", "OP",
        "DOT", "WIF", "VIRTUAL", "FARTCOIN", "HYPE", "TAO",
    ])
    data_dir: Path = Path("data/candles")
    # EMA signal params
    ema_high_period: int = 4
    ema_trend_period: int = 21
    sma_trend_period: int = 50
    sma_rising_lookback: int = 5
    # v6 params
    max_hold_bars_v6: int = 5
    # v7b params
    trailing_start_r: float = 0.5       # trailing SL activates at +0.5R
    trailing_distance_r: float = 1.0    # trailing SL stays 1R below peak
    strength_exit_r: float = 3.0        # take profit at 3R
    violation_min_r: float = 1.0        # violation exit requires at least 1R reached
    max_hold_bars_v7b: int = 8
    # position sizing
    initial_capital: float = 100.0
    leverage: int = 3
    position_pct: float = 0.25
    maker_fee: float = 0.0002
    taker_fee: float = 0.0005


# ---------------------------------------------------------------------------
# Trade dataclasses
# ---------------------------------------------------------------------------

@dataclass
class V6Trade:
    symbol: str
    entry_price: float
    entry_bar: int
    signal_low: float
    stop_price: float
    exit_price: float = 0.0
    exit_bar: int = 0
    exit_reason: str = ""
    pnl_pct: float = 0.0
    pnl_usd: float = 0.0


@dataclass
class V7bTrade:
    symbol: str
    entry_price: float
    entry_bar: int
    risk_1r: float          # absolute distance for 1R
    stop_price: float       # initial SL (candle low)
    trailing_active: bool = False
    max_r_reached: float = 0.0
    trailing_sl_price: float = 0.0  # absolute price of trailing SL
    exit_price: float = 0.0
    exit_bar: int = 0
    exit_reason: str = ""
    pnl_pct: float = 0.0
    pnl_usd: float = 0.0


# ---------------------------------------------------------------------------
# Per-symbol backtester
# ---------------------------------------------------------------------------

@dataclass
class SymbolResult:
    symbol: str
    v6_trades: list[V6Trade] = field(default_factory=list)
    v7b_trades: list[V7bTrade] = field(default_factory=list)
    v6_equity: float = 0.0
    v7b_equity: float = 0.0


def run_symbol_backtest(
    symbol: str,
    candles: list[dict],
    cfg: BacktestConfig,
) -> SymbolResult:
    """Run v6 and v7b backtest on a single symbol."""

    result = SymbolResult(symbol=symbol)
    n = len(candles)
    start_bar = max(cfg.sma_trend_period + cfg.sma_rising_lookback + 10, 60)

    if n <= start_bar:
        logger.warning("%s: only %d bars, need >%d. Skipping.", symbol, n, start_bar)
        return result

    # Build SqueezeCompareConfig for reusing compute_ema_high_signal
    squeeze_cfg = SqueezeCompareConfig(
        ema_high_period=cfg.ema_high_period,
        ema_trend_period=cfg.ema_trend_period,
        sma_trend_period=cfg.sma_trend_period,
        sma_rising_lookback=cfg.sma_rising_lookback,
    )

    equity_v6 = cfg.initial_capital
    equity_v7b = cfg.initial_capital

    open_v6: V6Trade | None = None
    open_v7b: V7bTrade | None = None

    closes = np.array([c["close"] for c in candles])
    highs = np.array([c["high"] for c in candles])
    lows = np.array([c["low"] for c in candles])

    for bar in range(start_bar, n):
        c = candles[bar]

        # ------- v6 exit -------
        if open_v6 is not None:
            bars_held = bar - open_v6.entry_bar

            if c["low"] <= open_v6.stop_price:
                open_v6.exit_price = open_v6.stop_price
                open_v6.exit_bar = bar
                open_v6.exit_reason = "stop"
            elif bars_held >= cfg.max_hold_bars_v6:
                open_v6.exit_price = c["close"]
                open_v6.exit_bar = bar
                open_v6.exit_reason = "max_hold"

            if open_v6.exit_bar > 0:
                open_v6.pnl_pct = (open_v6.exit_price - open_v6.entry_price) / open_v6.entry_price * 100
                notional = equity_v6 * cfg.position_pct * cfg.leverage
                open_v6.pnl_usd = notional * open_v6.pnl_pct / 100 - notional * cfg.taker_fee
                equity_v6 += open_v6.pnl_usd
                result.v6_trades.append(open_v6)
                open_v6 = None

        # ------- v7b exit -------
        if open_v7b is not None:
            bars_held = bar - open_v7b.entry_bar

            # Current R-multiple from bar high
            bar_max_r = (c["high"] - open_v7b.entry_price) / open_v7b.risk_1r if open_v7b.risk_1r > 0 else 0
            bar_close_r = (c["close"] - open_v7b.entry_price) / open_v7b.risk_1r if open_v7b.risk_1r > 0 else 0

            # Update max R reached
            if bar_max_r > open_v7b.max_r_reached:
                open_v7b.max_r_reached = bar_max_r

            # Trailing SL activation and update
            # Activates when max_r >= 0.5R, stays 1R below peak
            if open_v7b.max_r_reached >= cfg.trailing_start_r:
                open_v7b.trailing_active = True
                # trailing_sl_r = max_r_reached - 1.0 (in R-units)
                trailing_sl_r = open_v7b.max_r_reached - cfg.trailing_distance_r
                new_trailing_price = open_v7b.entry_price + trailing_sl_r * open_v7b.risk_1r
                # Only move SL up, never down
                if new_trailing_price > open_v7b.trailing_sl_price:
                    open_v7b.trailing_sl_price = new_trailing_price

            # Determine effective SL
            if open_v7b.trailing_active:
                # Trailing SL could be below initial SL at early stages,
                # so use the higher of the two
                eff_sl = max(open_v7b.trailing_sl_price, open_v7b.stop_price)
            else:
                eff_sl = open_v7b.stop_price

            # Check exits in priority order
            exit_price = None
            exit_reason = ""

            # 1. Stop loss / trailing SL hit
            if c["low"] <= eff_sl:
                exit_price = eff_sl
                exit_reason = "trailing_sl" if open_v7b.trailing_active else "stop"

            # 2. Strength exit: close reaches 3R
            if exit_price is None and bar_close_r >= cfg.strength_exit_r:
                exit_price = open_v7b.entry_price + cfg.strength_exit_r * open_v7b.risk_1r
                exit_reason = "strength_3r"

            # 3. Violation exit: reached at least 1R and close < EMA(4) High
            if exit_price is None and open_v7b.max_r_reached >= cfg.violation_min_r:
                hist_end = bar + 1
                ema_check = calculate_ema(highs[:hist_end], cfg.ema_high_period)
                if c["close"] < ema_check[-1]:
                    exit_price = c["close"]
                    exit_reason = "violation"

            # 4. Fallback: max bars
            if exit_price is None and bars_held >= cfg.max_hold_bars_v7b:
                exit_price = c["close"]
                exit_reason = "max_hold"

            if exit_price is not None:
                open_v7b.exit_price = exit_price
                open_v7b.exit_bar = bar
                open_v7b.exit_reason = exit_reason
                open_v7b.pnl_pct = (exit_price - open_v7b.entry_price) / open_v7b.entry_price * 100

                notional = equity_v7b * cfg.position_pct * cfg.leverage
                open_v7b.pnl_usd = notional * open_v7b.pnl_pct / 100 - notional * cfg.taker_fee
                equity_v7b += open_v7b.pnl_usd
                result.v7b_trades.append(open_v7b)
                open_v7b = None

        # ------- Entries (only if no open position) -------
        # History slice for signal detection
        hist_start = max(0, bar - cfg.sma_trend_period - cfg.sma_rising_lookback - 10)
        h_closes = closes[hist_start:bar + 1]
        h_highs = highs[hist_start:bar + 1]
        h_lows = lows[hist_start:bar + 1]

        # v6 entry (LONG only)
        if open_v6 is None:
            signal, entry_price, signal_low = compute_ema_high_signal(
                h_closes, h_highs, h_lows, squeeze_cfg,
            )
            if signal == "long":
                notional = equity_v6 * cfg.position_pct * cfg.leverage
                equity_v6 -= notional * cfg.maker_fee
                open_v6 = V6Trade(
                    symbol=symbol,
                    entry_price=entry_price,
                    entry_bar=bar,
                    signal_low=signal_low,
                    stop_price=signal_low,
                )

        # v7b entry (LONG only)
        if open_v7b is None:
            signal, entry_price, signal_low = compute_ema_high_signal(
                h_closes, h_highs, h_lows, squeeze_cfg,
            )
            if signal == "long":
                risk_1r = entry_price - signal_low
                notional = equity_v7b * cfg.position_pct * cfg.leverage
                equity_v7b -= notional * cfg.maker_fee
                open_v7b = V7bTrade(
                    symbol=symbol,
                    entry_price=entry_price,
                    entry_bar=bar,
                    risk_1r=risk_1r,
                    stop_price=signal_low,
                )

    # ------- Close remaining open trades at last bar -------
    last_c = candles[-1]

    if open_v6 is not None:
        open_v6.exit_price = last_c["close"]
        open_v6.exit_bar = n - 1
        open_v6.exit_reason = "end"
        open_v6.pnl_pct = (open_v6.exit_price - open_v6.entry_price) / open_v6.entry_price * 100
        notional = equity_v6 * cfg.position_pct * cfg.leverage
        open_v6.pnl_usd = notional * open_v6.pnl_pct / 100 - notional * cfg.taker_fee
        equity_v6 += open_v6.pnl_usd
        result.v6_trades.append(open_v6)

    if open_v7b is not None:
        open_v7b.exit_price = last_c["close"]
        open_v7b.exit_bar = n - 1
        open_v7b.exit_reason = "end"
        open_v7b.pnl_pct = (open_v7b.exit_price - open_v7b.entry_price) / open_v7b.entry_price * 100
        notional = equity_v7b * cfg.position_pct * cfg.leverage
        open_v7b.pnl_usd = notional * open_v7b.pnl_pct / 100 - notional * cfg.taker_fee
        equity_v7b += open_v7b.pnl_usd
        result.v7b_trades.append(open_v7b)

    result.v6_equity = equity_v6
    result.v7b_equity = equity_v7b
    return result


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def _win_rate(trades: list) -> float:
    if not trades:
        return 0.0
    return sum(1 for t in trades if t.pnl_usd > 0) / len(trades) * 100


def _profit_factor(trades: list) -> float:
    gross_win = sum(t.pnl_usd for t in trades if t.pnl_usd > 0)
    gross_loss = abs(sum(t.pnl_usd for t in trades if t.pnl_usd <= 0))
    if gross_loss == 0:
        return float("inf") if gross_win > 0 else 0.0
    return gross_win / gross_loss


def _total_return_pct(equity: float, initial: float) -> float:
    return (equity - initial) / initial * 100


def _avg_r(trades: list) -> float:
    """Average R-multiple at exit."""
    if not trades:
        return 0.0
    r_vals = []
    for t in trades:
        if hasattr(t, "risk_1r") and t.risk_1r > 0:
            r_vals.append((t.exit_price - t.entry_price) / t.risk_1r)
    return sum(r_vals) / len(r_vals) if r_vals else 0.0


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_results(results: list[SymbolResult], cfg: BacktestConfig) -> None:
    """Print per-asset table, aggregated totals, exit stats, and comparison."""

    header = (
        f"{'Symbol':<10s} | "
        f"{'v6 Tr':>5s} {'v6 WR':>6s} {'v6 PF':>6s} {'v6 Ret':>8s} | "
        f"{'v7b Tr':>6s} {'v7b WR':>6s} {'v7b PF':>6s} {'v7b Ret':>8s}"
    )
    sep = "-" * len(header)

    print()
    print("=" * len(header))
    print("  EMA High Signal -- v6 vs v7b Backtest Comparison")
    print(f"  Capital: ${cfg.initial_capital:.0f} | Leverage: {cfg.leverage}x | Position: {cfg.position_pct*100:.0f}%")
    print(f"  v7b: trailing SL from +{cfg.trailing_start_r}R, distance {cfg.trailing_distance_r}R, "
          f"strength {cfg.strength_exit_r}R, violation min {cfg.violation_min_r}R, max hold {cfg.max_hold_bars_v7b}")
    print("=" * len(header))
    print(header)
    print(sep)

    all_v6: list[V6Trade] = []
    all_v7b: list[V7bTrade] = []
    n_symbols = 0

    for r in results:
        v6_tr = len(r.v6_trades)
        v6_wr = _win_rate(r.v6_trades)
        v6_pf = _profit_factor(r.v6_trades)
        v6_ret = _total_return_pct(r.v6_equity, cfg.initial_capital)

        v7b_tr = len(r.v7b_trades)
        v7b_wr = _win_rate(r.v7b_trades)
        v7b_pf = _profit_factor(r.v7b_trades)
        v7b_ret = _total_return_pct(r.v7b_equity, cfg.initial_capital)

        pf_v6_s = f"{v6_pf:.2f}" if v6_pf < 100 else "inf"
        pf_v7b_s = f"{v7b_pf:.2f}" if v7b_pf < 100 else "inf"

        print(
            f"{r.symbol:<10s} | "
            f"{v6_tr:>5d} {v6_wr:>5.1f}% {pf_v6_s:>6s} {v6_ret:>+7.1f}% | "
            f"{v7b_tr:>6d} {v7b_wr:>5.1f}% {pf_v7b_s:>6s} {v7b_ret:>+7.1f}%"
        )

        all_v6.extend(r.v6_trades)
        all_v7b.extend(r.v7b_trades)
        n_symbols += 1

    print(sep)

    # Aggregated totals
    avg_v6_ret = sum(_total_return_pct(r.v6_equity, cfg.initial_capital) for r in results) / max(n_symbols, 1)
    avg_v7b_ret = sum(_total_return_pct(r.v7b_equity, cfg.initial_capital) for r in results) / max(n_symbols, 1)

    agg_v6_pf = _profit_factor(all_v6)
    agg_v7b_pf = _profit_factor(all_v7b)
    pf_v6_s = f"{agg_v6_pf:.2f}" if agg_v6_pf < 100 else "inf"
    pf_v7b_s = f"{agg_v7b_pf:.2f}" if agg_v7b_pf < 100 else "inf"

    print(
        f"{'TOTAL':<10s} | "
        f"{len(all_v6):>5d} {_win_rate(all_v6):>5.1f}% {pf_v6_s:>6s} {avg_v6_ret:>+7.1f}% | "
        f"{len(all_v7b):>6d} {_win_rate(all_v7b):>5.1f}% {pf_v7b_s:>6s} {avg_v7b_ret:>+7.1f}%"
    )
    print(sep)

    # ---- Direct comparison ----
    print()
    print("  --- Direct Comparison: v6 -> v7b ---")
    print(f"  v6  avg return per asset:  {avg_v6_ret:+.2f}%")
    print(f"  v7b avg return per asset:  {avg_v7b_ret:+.2f}%")
    delta = avg_v7b_ret - avg_v6_ret
    direction = "BETTER" if delta > 0 else "WORSE" if delta < 0 else "SAME"
    print(f"  Delta (v7b - v6):          {delta:+.2f}% ({direction})")
    print(f"  v6  total trades:          {len(all_v6)}")
    print(f"  v7b total trades:          {len(all_v7b)}")
    print(f"  v6  overall WR:            {_win_rate(all_v6):.1f}%")
    print(f"  v7b overall WR:            {_win_rate(all_v7b):.1f}%")
    print(f"  v6  profit factor:         {pf_v6_s}")
    print(f"  v7b profit factor:         {pf_v7b_s}")
    print(f"  v7b avg R at exit:         {_avg_r(all_v7b):+.2f}R")

    # ---- v7b Exit Reason Breakdown ----
    if all_v7b:
        reasons: dict[str, list[V7bTrade]] = {}
        for t in all_v7b:
            reasons.setdefault(t.exit_reason, []).append(t)

        print()
        print("  --- v7b Exit Reason Breakdown ---")
        print(f"  {'Reason':<16s} {'Count':>5s} {'%':>6s} {'WR':>6s} {'Avg R':>7s} {'PF':>6s}")
        print(f"  {'-'*50}")
        for reason in ["trailing_sl", "strength_3r", "violation", "stop", "max_hold", "end"]:
            trades_r = reasons.get(reason, [])
            if not trades_r:
                continue
            cnt = len(trades_r)
            pct = cnt / len(all_v7b) * 100
            wr = _win_rate(trades_r)
            avg_r = _avg_r(trades_r)
            pf = _profit_factor(trades_r)
            pf_s = f"{pf:.2f}" if pf < 100 else "inf"
            print(f"  {reason:<16s} {cnt:>5d} {pct:>5.1f}% {wr:>5.1f}% {avg_r:>+6.2f}R {pf_s:>6s}")

        # How many trades activated trailing
        trailing_activated = sum(1 for t in all_v7b if t.trailing_active)
        print()
        print(f"  Trailing SL activated:     {trailing_activated}/{len(all_v7b)} ({trailing_activated/len(all_v7b)*100:.1f}%)")
        # How many reached breakeven (max_r >= 1.0R means SL at 0R = entry)
        breakeven_reached = sum(1 for t in all_v7b if t.max_r_reached >= 1.0)
        print(f"  Breakeven reached (1R+):   {breakeven_reached}/{len(all_v7b)} ({breakeven_reached/len(all_v7b)*100:.1f}%)")

    # ---- v6 Exit Reason Breakdown ----
    if all_v6:
        reasons_v6: dict[str, int] = {}
        for t in all_v6:
            reasons_v6[t.exit_reason] = reasons_v6.get(t.exit_reason, 0) + 1
        print()
        print("  --- v6 Exit Reasons ---")
        for reason, count in sorted(reasons_v6.items(), key=lambda x: -x[1]):
            print(f"    {reason:<16s}: {count:>4d} ({count/len(all_v6)*100:.1f}%)")

    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="EMA High Signal -- v6 vs v7b Backtest",
    )
    parser.add_argument("--symbols", nargs="+", default=None,
                        help="Override default symbol list")
    parser.add_argument("--data-dir", default="data/candles")
    parser.add_argument("--capital", type=float, default=100.0)
    parser.add_argument("--leverage", type=int, default=3)
    parser.add_argument("--position-pct", type=float, default=0.25)
    parser.add_argument("--daily", action="store_true",
                        help="Use daily candles (resample from 15m if needed)")
    parser.add_argument("--strategy-a-only", action="store_true",
                        help="Compatibility flag (ignored, both strategies always run)")
    # v7b tunable
    parser.add_argument("--trailing-start", type=float, default=0.5,
                        help="R-multiple to activate trailing SL (default: 0.5)")
    parser.add_argument("--trailing-distance", type=float, default=1.0,
                        help="Trailing SL distance in R below peak (default: 1.0)")
    parser.add_argument("--strength-exit", type=float, default=3.0)
    parser.add_argument("--violation-min", type=float, default=1.0)
    parser.add_argument("--max-hold-v6", type=int, default=5)
    parser.add_argument("--max-hold-v7b", type=int, default=8)
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir

    cfg = BacktestConfig(
        data_dir=data_dir,
        initial_capital=args.capital,
        leverage=args.leverage,
        position_pct=args.position_pct,
        trailing_start_r=args.trailing_start,
        trailing_distance_r=args.trailing_distance,
        strength_exit_r=args.strength_exit,
        violation_min_r=args.violation_min,
        max_hold_bars_v6=args.max_hold_v6,
        max_hold_bars_v7b=args.max_hold_v7b,
    )
    if args.symbols:
        cfg.symbols = args.symbols

    # Load candles
    interval = "1d" if args.daily else "15m"
    candles_by_symbol = await fetch_or_load_candles(
        assets=cfg.symbols,
        data_dir=data_dir,
        interval=interval,
    )

    if not candles_by_symbol:
        logger.error("No candle data available. Exiting.")
        sys.exit(1)

    # Run per-symbol backtest
    results: list[SymbolResult] = []
    for symbol, candles in candles_by_symbol.items():
        logger.info("Backtesting %s: %d bars", symbol, len(candles))
        r = run_symbol_backtest(symbol, candles, cfg)
        results.append(r)

    # Print results
    print_results(results, cfg)


if __name__ == "__main__":
    asyncio.run(main())
