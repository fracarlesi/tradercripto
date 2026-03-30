"""
EMA High Signal — v6 vs v7 Backtest Comparison
================================================
Compares two versions of the EMA High breakout strategy:
  - v6: LONG only, fixed 5-bar exit, no BP protection
  - v7: LONG + SHORT, BP protection, trailing SL, triple exit

Usage:
    python -m crypto_bot.scripts.ema_v6_vs_v7_backtest \
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
    # v7 params
    bp_threshold_r: float = 2.0
    strength_exit_r: float = 3.0
    violation_min_r: float = 1.0
    max_hold_bars_v7: int = 8
    trailing_step_r: float = 0.5  # per ogni +1R sopra bp_threshold
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
class V7Trade:
    symbol: str
    direction: str  # "long" or "short"
    entry_price: float
    entry_bar: int
    risk_1r: float  # absolute distance for 1R
    stop_price: float
    bp_activated: bool = False
    max_r_reached: float = 0.0
    trailing_sl: float = 0.0
    exit_price: float = 0.0
    exit_bar: int = 0
    exit_reason: str = ""
    pnl_pct: float = 0.0
    pnl_usd: float = 0.0


# ---------------------------------------------------------------------------
# Signal detection: SHORT (v7 only)
# ---------------------------------------------------------------------------

def compute_ema_low_signal(
    closes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    cfg: BacktestConfig,
) -> tuple[str | None, float, float]:
    """Compute EMA-Low breakdown signal (SHORT).

    Signal SHORT if:
    - close[-1] < EMA(lows, 4)[-1]        (breakdown)
    - close[-2] > EMA(lows, 4)[-2]        (was above yesterday)
    - close[-3] > EMA(lows, 4)[-3]        (was above 2 days ago)
    - EMA(closes, 21)[-1] < SMA(closes, 50)[-1]  (downtrend)
    - SMA(closes, 50) falling for lookback bars
    """
    n = len(closes)
    min_required = max(cfg.sma_trend_period + cfg.sma_rising_lookback, cfg.ema_high_period + 3)
    if n < min_required:
        return None, 0.0, 0.0

    ema_low = calculate_ema(lows, cfg.ema_high_period)

    # Breakdown: close crosses below EMA(lows)
    if not (closes[-1] < ema_low[-1]):
        return None, 0.0, 0.0
    if not (closes[-2] > ema_low[-2]):
        return None, 0.0, 0.0
    if not (closes[-3] > ema_low[-3]):
        return None, 0.0, 0.0

    # Trend filter: EMA(21) < SMA(50)
    ema_trend = calculate_ema(closes, cfg.ema_trend_period)
    sma_trend = np.convolve(
        closes, np.ones(cfg.sma_trend_period) / cfg.sma_trend_period, mode="valid",
    )

    if len(sma_trend) < cfg.sma_rising_lookback + 1:
        return None, 0.0, 0.0

    if not (ema_trend[-1] < sma_trend[-1]):
        return None, 0.0, 0.0

    # SMA(50) must be falling
    if not (sma_trend[-1] < sma_trend[-cfg.sma_rising_lookback - 1]):
        return None, 0.0, 0.0

    entry_close = float(closes[-1])
    signal_high = float(highs[-1])

    # Guard: inverted bar
    if signal_high <= entry_close:
        return None, 0.0, 0.0

    return "short", entry_close, signal_high


# ---------------------------------------------------------------------------
# Per-symbol backtester
# ---------------------------------------------------------------------------

@dataclass
class SymbolResult:
    symbol: str
    v6_trades: list[V6Trade] = field(default_factory=list)
    v7_long_trades: list[V7Trade] = field(default_factory=list)
    v7_short_trades: list[V7Trade] = field(default_factory=list)
    v6_equity: float = 0.0
    v7_equity: float = 0.0


def run_symbol_backtest(
    symbol: str,
    candles: list[dict],
    cfg: BacktestConfig,
) -> SymbolResult:
    """Run v6 and v7 backtest on a single symbol."""

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
    equity_v7 = cfg.initial_capital

    open_v6: V6Trade | None = None
    open_v7: V7Trade | None = None

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

        # ------- v7 exit -------
        if open_v7 is not None:
            bars_held = bar - open_v7.entry_bar
            is_long = open_v7.direction == "long"

            # Current R-multiple of the bar extremes
            if is_long:
                bar_max_r = (c["high"] - open_v7.entry_price) / open_v7.risk_1r if open_v7.risk_1r > 0 else 0
                bar_close_r = (c["close"] - open_v7.entry_price) / open_v7.risk_1r if open_v7.risk_1r > 0 else 0
            else:
                bar_max_r = (open_v7.entry_price - c["low"]) / open_v7.risk_1r if open_v7.risk_1r > 0 else 0
                bar_close_r = (open_v7.entry_price - c["close"]) / open_v7.risk_1r if open_v7.risk_1r > 0 else 0

            # Update max R reached
            if bar_max_r > open_v7.max_r_reached:
                open_v7.max_r_reached = bar_max_r

            # BP protection activation
            if not open_v7.bp_activated and open_v7.max_r_reached >= cfg.bp_threshold_r:
                open_v7.bp_activated = True
                open_v7.trailing_sl = open_v7.entry_price  # breakeven

            # Trailing SL update (for every +1R above bp_threshold, SL moves +0.5R)
            if open_v7.bp_activated:
                r_above_bp = open_v7.max_r_reached - cfg.bp_threshold_r
                if r_above_bp > 0:
                    trailing_offset = int(r_above_bp) * cfg.trailing_step_r * open_v7.risk_1r
                    if is_long:
                        new_trailing = open_v7.entry_price + trailing_offset
                        if new_trailing > open_v7.trailing_sl:
                            open_v7.trailing_sl = new_trailing
                    else:
                        new_trailing = open_v7.entry_price - trailing_offset
                        if new_trailing < open_v7.trailing_sl:
                            open_v7.trailing_sl = new_trailing

            # Determine effective SL
            if open_v7.bp_activated:
                eff_sl = open_v7.trailing_sl
            else:
                eff_sl = open_v7.stop_price

            # Check exits in priority order
            exit_price = None
            exit_reason = ""

            # 1. Stop loss / trailing SL hit
            if is_long:
                if c["low"] <= eff_sl:
                    exit_price = eff_sl
                    exit_reason = "trailing_sl" if open_v7.bp_activated else "stop"
            else:
                if c["high"] >= eff_sl:
                    exit_price = eff_sl
                    exit_reason = "trailing_sl" if open_v7.bp_activated else "stop"

            # 2. Strength exit: close reaches 3R
            if exit_price is None and bar_close_r >= cfg.strength_exit_r:
                if is_long:
                    exit_price = open_v7.entry_price + cfg.strength_exit_r * open_v7.risk_1r
                else:
                    exit_price = open_v7.entry_price - cfg.strength_exit_r * open_v7.risk_1r
                exit_reason = "strength_3r"

            # 3. Violation exit: reached at least 1R but close violates EMA
            if exit_price is None and open_v7.max_r_reached >= cfg.violation_min_r:
                # Compute current EMA for violation check
                hist_end = bar + 1
                if is_long:
                    ema_check = calculate_ema(highs[:hist_end], cfg.ema_high_period)
                    if c["close"] < ema_check[-1]:
                        exit_price = c["close"]
                        exit_reason = "violation"
                else:
                    ema_check = calculate_ema(lows[:hist_end], cfg.ema_high_period)
                    if c["close"] > ema_check[-1]:
                        exit_price = c["close"]
                        exit_reason = "violation"

            # 4. Fallback: max bars
            if exit_price is None and bars_held >= cfg.max_hold_bars_v7:
                exit_price = c["close"]
                exit_reason = "max_hold"

            if exit_price is not None:
                open_v7.exit_price = exit_price
                open_v7.exit_bar = bar
                open_v7.exit_reason = exit_reason

                if is_long:
                    open_v7.pnl_pct = (exit_price - open_v7.entry_price) / open_v7.entry_price * 100
                else:
                    open_v7.pnl_pct = (open_v7.entry_price - exit_price) / open_v7.entry_price * 100

                notional = equity_v7 * cfg.position_pct * cfg.leverage
                open_v7.pnl_usd = notional * open_v7.pnl_pct / 100 - notional * cfg.taker_fee
                equity_v7 += open_v7.pnl_usd

                if is_long:
                    result.v7_long_trades.append(open_v7)
                else:
                    result.v7_short_trades.append(open_v7)
                open_v7 = None

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

        # v7 entry (LONG + SHORT)
        if open_v7 is None:
            # Try LONG
            signal, entry_price, signal_low = compute_ema_high_signal(
                h_closes, h_highs, h_lows, squeeze_cfg,
            )
            if signal == "long":
                risk_1r = entry_price - signal_low
                notional = equity_v7 * cfg.position_pct * cfg.leverage
                equity_v7 -= notional * cfg.maker_fee
                open_v7 = V7Trade(
                    symbol=symbol,
                    direction="long",
                    entry_price=entry_price,
                    entry_bar=bar,
                    risk_1r=risk_1r,
                    stop_price=signal_low,
                )
            else:
                # Try SHORT
                signal, entry_price, signal_high = compute_ema_low_signal(
                    h_closes, h_highs, h_lows, cfg,
                )
                if signal == "short":
                    risk_1r = signal_high - entry_price
                    notional = equity_v7 * cfg.position_pct * cfg.leverage
                    equity_v7 -= notional * cfg.maker_fee
                    open_v7 = V7Trade(
                        symbol=symbol,
                        direction="short",
                        entry_price=entry_price,
                        entry_bar=bar,
                        risk_1r=risk_1r,
                        stop_price=signal_high,
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

    if open_v7 is not None:
        open_v7.exit_price = last_c["close"]
        open_v7.exit_bar = n - 1
        open_v7.exit_reason = "end"
        is_long = open_v7.direction == "long"
        if is_long:
            open_v7.pnl_pct = (open_v7.exit_price - open_v7.entry_price) / open_v7.entry_price * 100
        else:
            open_v7.pnl_pct = (open_v7.entry_price - open_v7.exit_price) / open_v7.entry_price * 100
        notional = equity_v7 * cfg.position_pct * cfg.leverage
        open_v7.pnl_usd = notional * open_v7.pnl_pct / 100 - notional * cfg.taker_fee
        equity_v7 += open_v7.pnl_usd
        if is_long:
            result.v7_long_trades.append(open_v7)
        else:
            result.v7_short_trades.append(open_v7)

    result.v6_equity = equity_v6
    result.v7_equity = equity_v7
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


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_results(results: list[SymbolResult], cfg: BacktestConfig) -> None:
    """Print per-asset table and aggregated totals."""

    header = (
        f"{'Symbol':<10s} | "
        f"{'v6 Tr':>5s} {'v6 WR':>6s} {'v6 PF':>6s} {'v6 Ret':>8s} | "
        f"{'v7L Tr':>6s} {'v7L WR':>6s} {'v7S Tr':>6s} {'v7S WR':>6s} {'v7 PF':>6s} {'v7 Ret':>8s}"
    )
    sep = "-" * len(header)

    print()
    print("=" * len(header))
    print("  EMA High Signal — v6 vs v7 Backtest Comparison")
    print(f"  Capital: ${cfg.initial_capital:.0f} | Leverage: {cfg.leverage}x | Position: {cfg.position_pct*100:.0f}%")
    print("=" * len(header))
    print(header)
    print(sep)

    all_v6: list[V6Trade] = []
    all_v7: list[V7Trade] = []
    total_v6_equity = 0.0
    total_v7_equity = 0.0
    n_symbols = 0

    for r in results:
        v6_tr = len(r.v6_trades)
        v6_wr = _win_rate(r.v6_trades)
        v6_pf = _profit_factor(r.v6_trades)
        v6_ret = _total_return_pct(r.v6_equity, cfg.initial_capital)

        v7l_tr = len(r.v7_long_trades)
        v7l_wr = _win_rate(r.v7_long_trades)
        v7s_tr = len(r.v7_short_trades)
        v7s_wr = _win_rate(r.v7_short_trades)
        v7_all = r.v7_long_trades + r.v7_short_trades
        v7_pf = _profit_factor(v7_all)
        v7_ret = _total_return_pct(r.v7_equity, cfg.initial_capital)

        pf_v6_s = f"{v6_pf:.2f}" if v6_pf < 100 else "inf"
        pf_v7_s = f"{v7_pf:.2f}" if v7_pf < 100 else "inf"

        print(
            f"{r.symbol:<10s} | "
            f"{v6_tr:>5d} {v6_wr:>5.1f}% {pf_v6_s:>6s} {v6_ret:>+7.1f}% | "
            f"{v7l_tr:>6d} {v7l_wr:>5.1f}% {v7s_tr:>6d} {v7s_wr:>5.1f}% {pf_v7_s:>6s} {v7_ret:>+7.1f}%"
        )

        all_v6.extend(r.v6_trades)
        all_v7.extend(r.v7_long_trades + r.v7_short_trades)
        total_v6_equity += r.v6_equity
        total_v7_equity += r.v7_equity
        n_symbols += 1

    print(sep)

    # Aggregated totals
    avg_v6_ret = sum(_total_return_pct(r.v6_equity, cfg.initial_capital) for r in results) / max(n_symbols, 1)
    avg_v7_ret = sum(_total_return_pct(r.v7_equity, cfg.initial_capital) for r in results) / max(n_symbols, 1)
    v7_longs = [t for t in all_v7 if t.direction == "long"]
    v7_shorts = [t for t in all_v7 if t.direction == "short"]

    agg_v6_pf = _profit_factor(all_v6)
    agg_v7_pf = _profit_factor(all_v7)
    pf_v6_s = f"{agg_v6_pf:.2f}" if agg_v6_pf < 100 else "inf"
    pf_v7_s = f"{agg_v7_pf:.2f}" if agg_v7_pf < 100 else "inf"

    print(
        f"{'TOTAL':<10s} | "
        f"{len(all_v6):>5d} {_win_rate(all_v6):>5.1f}% {pf_v6_s:>6s} {avg_v6_ret:>+7.1f}% | "
        f"{len(v7_longs):>6d} {_win_rate(v7_longs):>5.1f}% {len(v7_shorts):>6d} {_win_rate(v7_shorts):>5.1f}% {pf_v7_s:>6s} {avg_v7_ret:>+7.1f}%"
    )
    print(sep)

    # Direct comparison
    print()
    print("  --- Direct Comparison ---")
    print(f"  v6 avg return per asset:  {avg_v6_ret:+.2f}%")
    print(f"  v7 avg return per asset:  {avg_v7_ret:+.2f}%")
    delta = avg_v7_ret - avg_v6_ret
    print(f"  Delta (v7 - v6):          {delta:+.2f}%")
    print(f"  v6 total trades:          {len(all_v6)}")
    print(f"  v7 total trades:          {len(all_v7)} (L:{len(v7_longs)} S:{len(v7_shorts)})")
    print(f"  v6 overall WR:            {_win_rate(all_v6):.1f}%")
    print(f"  v7 overall WR:            {_win_rate(all_v7):.1f}%")
    print(f"  v6 profit factor:         {pf_v6_s}")
    print(f"  v7 profit factor:         {pf_v7_s}")

    # Exit reason breakdown for v7
    if all_v7:
        reasons: dict[str, int] = {}
        for t in all_v7:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        print()
        print("  --- v7 Exit Reasons ---")
        for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"    {reason:<16s}: {count:>4d} ({count/len(all_v7)*100:.1f}%)")

    # Exit reason breakdown for v6
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
        description="EMA High Signal — v6 vs v7 Backtest",
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
    # v7 tunable
    parser.add_argument("--bp-threshold", type=float, default=2.0)
    parser.add_argument("--strength-exit", type=float, default=3.0)
    parser.add_argument("--violation-min", type=float, default=1.0)
    parser.add_argument("--max-hold-v6", type=int, default=5)
    parser.add_argument("--max-hold-v7", type=int, default=8)
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
        bp_threshold_r=args.bp_threshold,
        strength_exit_r=args.strength_exit,
        violation_min_r=args.violation_min,
        max_hold_bars_v6=args.max_hold_v6,
        max_hold_bars_v7=args.max_hold_v7,
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
