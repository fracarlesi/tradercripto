"""
EMA High Signal — Trailing SL Parameter Sweep
==============================================
Grid-search over trailing-SL parameters to find the best combination
for the EMA High breakout strategy (LONG only, daily bars).

Tested dimensions:
  - trail_start_r:    [0.5, 1.0, 1.5, 2.0]
  - trail_distance_r: [1.0, 1.5, 2.0, 2.5]
  - max_hold_bars:    [5, 8, 10, 12]
  - strength_exit_r:  [0, 3.0, 4.0, 5.0]   (0 = disabled)
  - violation_min_r:  [0, 0.5, 1.0, 1.5]    (0 = disabled)

Includes v6 baseline (max_hold=5, no trailing) as reference.

Usage:
    python -m crypto_bot.scripts.ema_trailing_sweep \
        --daily --strategy-a-only
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import logging
import sys
import time
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

SYMBOLS = [
    "BTC", "ETH", "SOL", "LINK", "BNB", "ADA", "AVAX", "ARB", "OP",
    "DOT", "WIF", "VIRTUAL", "FARTCOIN", "HYPE", "TAO",
]

# Grid dimensions
TRAIL_START_R = [0.5, 1.0, 1.5, 2.0]
TRAIL_DISTANCE_R = [1.0, 1.5, 2.0, 2.5]
MAX_HOLD_BARS = [5, 8, 10, 12]
STRENGTH_EXIT_R = [0.0, 3.0, 4.0, 5.0]      # 0 = disabled
VIOLATION_MIN_R = [0.0, 0.5, 1.0, 1.5]       # 0 = disabled

# Fixed params
EMA_HIGH_PERIOD = 4
EMA_TREND_PERIOD = 21
SMA_TREND_PERIOD = 50
SMA_RISING_LOOKBACK = 5
INITIAL_CAPITAL = 100.0
LEVERAGE = 3
POSITION_PCT = 0.25
MAKER_FEE = 0.0002
TAKER_FEE = 0.0005


@dataclass
class SweepParams:
    trail_start_r: float
    trail_distance_r: float
    max_hold_bars: int
    strength_exit_r: float   # 0 = disabled
    violation_min_r: float   # 0 = disabled
    is_v6_baseline: bool = False

    @property
    def label(self) -> str:
        if self.is_v6_baseline:
            return "v6_baseline(hold=5,SL=-1R)"
        parts = [
            f"start={self.trail_start_r:.1f}R",
            f"dist={self.trail_distance_r:.1f}R",
            f"hold={self.max_hold_bars}",
        ]
        if self.strength_exit_r > 0:
            parts.append(f"str={self.strength_exit_r:.0f}R")
        else:
            parts.append("str=off")
        if self.violation_min_r > 0:
            parts.append(f"viol={self.violation_min_r:.1f}R")
        else:
            parts.append("viol=off")
        return " | ".join(parts)


@dataclass
class TradeResult:
    entry_price: float
    exit_price: float
    risk_1r: float
    pnl_usd: float
    pnl_pct: float
    exit_reason: str
    trailing_activated: bool = False
    max_r_reached: float = 0.0


@dataclass
class SweepResult:
    params: SweepParams
    trades: list[TradeResult] = field(default_factory=list)
    per_asset_return: dict[str, float] = field(default_factory=dict)

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.pnl_usd > 0) / len(self.trades) * 100

    @property
    def profit_factor(self) -> float:
        gross_win = sum(t.pnl_usd for t in self.trades if t.pnl_usd > 0)
        gross_loss = abs(sum(t.pnl_usd for t in self.trades if t.pnl_usd <= 0))
        if gross_loss == 0:
            return float("inf") if gross_win > 0 else 0.0
        return gross_win / gross_loss

    @property
    def avg_return_per_asset(self) -> float:
        if not self.per_asset_return:
            return 0.0
        return sum(self.per_asset_return.values()) / len(self.per_asset_return)

    @property
    def avg_r_per_trade(self) -> float:
        if not self.trades:
            return 0.0
        r_vals = []
        for t in self.trades:
            if t.risk_1r > 0:
                r_vals.append((t.exit_price - t.entry_price) / t.risk_1r)
        return sum(r_vals) / len(r_vals) if r_vals else 0.0


# ---------------------------------------------------------------------------
# Per-symbol backtester (single param set)
# ---------------------------------------------------------------------------

def run_symbol(
    symbol: str,
    candles: list[dict],
    params: SweepParams,
    squeeze_cfg: SqueezeCompareConfig,
) -> tuple[list[TradeResult], float]:
    """Run backtest for one symbol with given params.
    Returns (trades, final_equity).
    """
    n = len(candles)
    start_bar = max(SMA_TREND_PERIOD + SMA_RISING_LOOKBACK + 10, 60)
    if n <= start_bar:
        return [], INITIAL_CAPITAL

    closes = np.array([c["close"] for c in candles])
    highs = np.array([c["high"] for c in candles])
    lows = np.array([c["low"] for c in candles])

    equity = INITIAL_CAPITAL
    trades: list[TradeResult] = []

    # Open position state
    in_trade = False
    entry_price = 0.0
    entry_bar = 0
    risk_1r = 0.0
    stop_price = 0.0
    trailing_active = False
    max_r_reached = 0.0
    trailing_sl_price = 0.0

    for bar in range(start_bar, n):
        c = candles[bar]

        # ------- EXIT LOGIC -------
        if in_trade:
            bars_held = bar - entry_bar

            if params.is_v6_baseline:
                # v6: simple SL + max hold 5
                exit_price = None
                exit_reason = ""

                if c["low"] <= stop_price:
                    exit_price = stop_price
                    exit_reason = "stop"
                elif bars_held >= 5:
                    exit_price = c["close"]
                    exit_reason = "max_hold"

                if exit_price is not None:
                    pnl_pct = (exit_price - entry_price) / entry_price * 100
                    notional = equity * POSITION_PCT * LEVERAGE
                    pnl_usd = notional * pnl_pct / 100 - notional * TAKER_FEE
                    equity += pnl_usd
                    trades.append(TradeResult(
                        entry_price=entry_price,
                        exit_price=exit_price,
                        risk_1r=risk_1r,
                        pnl_usd=pnl_usd,
                        pnl_pct=pnl_pct,
                        exit_reason=exit_reason,
                        trailing_activated=False,
                        max_r_reached=0.0,
                    ))
                    in_trade = False
            else:
                # Trailing strategy
                bar_max_r = (c["high"] - entry_price) / risk_1r if risk_1r > 0 else 0
                bar_close_r = (c["close"] - entry_price) / risk_1r if risk_1r > 0 else 0

                if bar_max_r > max_r_reached:
                    max_r_reached = bar_max_r

                # Trailing SL activation/update
                if max_r_reached >= params.trail_start_r:
                    trailing_active = True
                    trail_sl_r = max_r_reached - params.trail_distance_r
                    new_trailing_price = entry_price + trail_sl_r * risk_1r
                    if new_trailing_price > trailing_sl_price:
                        trailing_sl_price = new_trailing_price

                # Effective SL
                if trailing_active:
                    eff_sl = max(trailing_sl_price, stop_price)
                else:
                    eff_sl = stop_price

                exit_price = None
                exit_reason = ""

                # 1. SL / trailing SL
                if c["low"] <= eff_sl:
                    exit_price = eff_sl
                    exit_reason = "trailing_sl" if trailing_active else "stop"

                # 2. Strength exit
                if exit_price is None and params.strength_exit_r > 0 and bar_close_r >= params.strength_exit_r:
                    exit_price = entry_price + params.strength_exit_r * risk_1r
                    exit_reason = "strength"

                # 3. Violation exit
                if exit_price is None and params.violation_min_r > 0 and max_r_reached >= params.violation_min_r:
                    hist_end = bar + 1
                    ema_check = calculate_ema(highs[:hist_end], EMA_HIGH_PERIOD)
                    if c["close"] < ema_check[-1]:
                        exit_price = c["close"]
                        exit_reason = "violation"

                # 4. Max hold
                if exit_price is None and bars_held >= params.max_hold_bars:
                    exit_price = c["close"]
                    exit_reason = "max_hold"

                if exit_price is not None:
                    pnl_pct = (exit_price - entry_price) / entry_price * 100
                    notional = equity * POSITION_PCT * LEVERAGE
                    pnl_usd = notional * pnl_pct / 100 - notional * TAKER_FEE
                    equity += pnl_usd
                    trades.append(TradeResult(
                        entry_price=entry_price,
                        exit_price=exit_price,
                        risk_1r=risk_1r,
                        pnl_usd=pnl_usd,
                        pnl_pct=pnl_pct,
                        exit_reason=exit_reason,
                        trailing_activated=trailing_active,
                        max_r_reached=max_r_reached,
                    ))
                    in_trade = False

        # ------- ENTRY LOGIC -------
        if not in_trade:
            hist_start = max(0, bar - SMA_TREND_PERIOD - SMA_RISING_LOOKBACK - 10)
            h_closes = closes[hist_start:bar + 1]
            h_highs = highs[hist_start:bar + 1]
            h_lows = lows[hist_start:bar + 1]

            signal, ep, sl = compute_ema_high_signal(h_closes, h_highs, h_lows, squeeze_cfg)
            if signal == "long":
                entry_price = ep
                stop_price = sl
                risk_1r = entry_price - stop_price
                entry_bar = bar
                trailing_active = False
                max_r_reached = 0.0
                trailing_sl_price = 0.0
                in_trade = True

                notional = equity * POSITION_PCT * LEVERAGE
                equity -= notional * MAKER_FEE

    # Close remaining open trade
    if in_trade:
        last_c = candles[-1]
        exit_price = last_c["close"]
        pnl_pct = (exit_price - entry_price) / entry_price * 100
        notional = equity * POSITION_PCT * LEVERAGE
        pnl_usd = notional * pnl_pct / 100 - notional * TAKER_FEE
        equity += pnl_usd
        trades.append(TradeResult(
            entry_price=entry_price,
            exit_price=exit_price,
            risk_1r=risk_1r,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            exit_reason="end",
            trailing_activated=trailing_active,
            max_r_reached=max_r_reached,
        ))

    return trades, equity


# ---------------------------------------------------------------------------
# Sweep runner
# ---------------------------------------------------------------------------

def run_sweep(
    candles_by_symbol: dict[str, list[dict]],
    param_grid: list[SweepParams],
) -> list[SweepResult]:
    """Run backtest for every param combination across all symbols."""

    squeeze_cfg = SqueezeCompareConfig(
        ema_high_period=EMA_HIGH_PERIOD,
        ema_trend_period=EMA_TREND_PERIOD,
        sma_trend_period=SMA_TREND_PERIOD,
        sma_rising_lookback=SMA_RISING_LOOKBACK,
    )

    total_combos = len(param_grid)
    results: list[SweepResult] = []

    for idx, params in enumerate(param_grid):
        if (idx + 1) % 50 == 0 or idx == 0:
            logger.info("Sweep %d/%d: %s", idx + 1, total_combos, params.label)

        sr = SweepResult(params=params)
        for symbol, candles in candles_by_symbol.items():
            trades, final_equity = run_symbol(symbol, candles, params, squeeze_cfg)
            sr.trades.extend(trades)
            ret_pct = (final_equity - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
            sr.per_asset_return[symbol] = ret_pct

        results.append(sr)

    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_table(title: str, rows: list[SweepResult], top_n: int = 20) -> None:
    """Print a formatted table of sweep results."""
    header = (
        f"{'#':>3s}  {'Trades':>6s} {'WR':>6s} {'PF':>6s} "
        f"{'Avg Ret':>8s} {'Avg R':>7s}  {'Parameters'}"
    )
    sep = "-" * 120

    print()
    print(f"  {title}")
    print(sep)
    print(header)
    print(sep)

    for i, sr in enumerate(rows[:top_n]):
        pf_s = f"{sr.profit_factor:.2f}" if sr.profit_factor < 100 else "inf"
        print(
            f"{i+1:>3d}  {sr.total_trades:>6d} {sr.win_rate:>5.1f}% {pf_s:>6s} "
            f"{sr.avg_return_per_asset:>+7.2f}% {sr.avg_r_per_trade:>+6.2f}R  "
            f"{sr.params.label}"
        )
    print(sep)


def print_comparison(results: list[SweepResult]) -> None:
    """Print direct comparison: v6 vs v7 original vs v7b vs best."""

    # Find named configurations
    v6 = None
    v7_orig = None
    v7b = None
    best_pf = None
    best_ret = None

    for sr in results:
        if sr.params.is_v6_baseline:
            v6 = sr
        # v7 original: trail_start=2.0, trail_distance=2.0, hold=8, strength=0, violation=0
        if (not sr.params.is_v6_baseline
            and sr.params.trail_start_r == 2.0
            and sr.params.trail_distance_r == 2.0
            and sr.params.max_hold_bars == 8
            and sr.params.strength_exit_r == 0.0
            and sr.params.violation_min_r == 0.0):
            v7_orig = sr
        # v7b: trail_start=0.5, trail_distance=1.0, hold=8, strength=3.0, violation=1.0
        if (not sr.params.is_v6_baseline
            and sr.params.trail_start_r == 0.5
            and sr.params.trail_distance_r == 1.0
            and sr.params.max_hold_bars == 8
            and sr.params.strength_exit_r == 3.0
            and sr.params.violation_min_r == 1.0):
            v7b = sr

    # Best by PF (among combos with at least 10 trades)
    valid = [sr for sr in results if sr.total_trades >= 10 and not sr.params.is_v6_baseline]
    if valid:
        best_pf = max(valid, key=lambda x: x.profit_factor)
        best_ret = max(valid, key=lambda x: x.avg_return_per_asset)

    # Best balanced: score = 0.5 * normalized_PF + 0.5 * normalized_return
    if valid:
        pf_values = [sr.profit_factor for sr in valid if sr.profit_factor < 100]
        ret_values = [sr.avg_return_per_asset for sr in valid]
        if pf_values and ret_values:
            pf_min, pf_max = min(pf_values), max(pf_values)
            ret_min, ret_max = min(ret_values), max(ret_values)
            pf_range = pf_max - pf_min if pf_max != pf_min else 1.0
            ret_range = ret_max - ret_min if ret_max != ret_min else 1.0

            def balanced_score(sr: SweepResult) -> float:
                pf = sr.profit_factor if sr.profit_factor < 100 else pf_max
                norm_pf = (pf - pf_min) / pf_range
                norm_ret = (sr.avg_return_per_asset - ret_min) / ret_range
                return 0.5 * norm_pf + 0.5 * norm_ret

            best_balanced = max(valid, key=balanced_score)
        else:
            best_balanced = best_pf
    else:
        best_balanced = None

    sep = "=" * 100
    print()
    print(sep)
    print("  DIRECT COMPARISON")
    print(sep)

    def _print_row(name: str, sr: SweepResult | None) -> None:
        if sr is None:
            print(f"  {name:<25s}  (not found in grid)")
            return
        pf_s = f"{sr.profit_factor:.2f}" if sr.profit_factor < 100 else "inf"
        print(
            f"  {name:<25s}  "
            f"Trades={sr.total_trades:>4d}  "
            f"WR={sr.win_rate:>5.1f}%  "
            f"PF={pf_s:>6s}  "
            f"Ret={sr.avg_return_per_asset:>+7.2f}%  "
            f"AvgR={sr.avg_r_per_trade:>+6.2f}R"
        )
        if not sr.params.is_v6_baseline:
            trailing_pct = (
                sum(1 for t in sr.trades if t.trailing_activated) / len(sr.trades) * 100
                if sr.trades else 0.0
            )
            print(f"  {'':25s}  Trailing activated: {trailing_pct:.1f}%")

    _print_row("v6 baseline", v6)
    _print_row("v7 original (BP 2R)", v7_orig)
    _print_row("v7b aggressive", v7b)
    _print_row("BEST by PF", best_pf)
    _print_row("BEST by Return", best_ret)
    _print_row("BEST balanced (PF+Ret)", best_balanced)

    print(sep)

    if best_balanced:
        print()
        print("  RECOMMENDED CONFIGURATION:")
        p = best_balanced.params
        print(f"    trail_start_r:    {p.trail_start_r}")
        print(f"    trail_distance_r: {p.trail_distance_r}")
        print(f"    max_hold_bars:    {p.max_hold_bars}")
        print(f"    strength_exit_r:  {p.strength_exit_r} {'(disabled)' if p.strength_exit_r == 0 else ''}")
        print(f"    violation_min_r:  {p.violation_min_r} {'(disabled)' if p.violation_min_r == 0 else ''}")
        pf_s = f"{best_balanced.profit_factor:.2f}" if best_balanced.profit_factor < 100 else "inf"
        print(f"    => PF={pf_s}, Ret={best_balanced.avg_return_per_asset:+.2f}%, "
              f"WR={best_balanced.win_rate:.1f}%, AvgR={best_balanced.avg_r_per_trade:+.2f}R")

    # Exit reason breakdown for best balanced
    if best_balanced and best_balanced.trades:
        reasons: dict[str, list[TradeResult]] = {}
        for t in best_balanced.trades:
            reasons.setdefault(t.exit_reason, []).append(t)

        print()
        print("  EXIT REASON BREAKDOWN (best balanced):")
        print(f"  {'Reason':<16s} {'Count':>5s} {'%':>6s} {'WR':>6s} {'Avg R':>7s}")
        print(f"  {'-'*46}")
        for reason in ["trailing_sl", "strength", "violation", "stop", "max_hold", "end"]:
            tr_list = reasons.get(reason, [])
            if not tr_list:
                continue
            cnt = len(tr_list)
            pct = cnt / len(best_balanced.trades) * 100
            wr = sum(1 for t in tr_list if t.pnl_usd > 0) / cnt * 100
            r_vals = [(t.exit_price - t.entry_price) / t.risk_1r for t in tr_list if t.risk_1r > 0]
            avg_r = sum(r_vals) / len(r_vals) if r_vals else 0.0
            print(f"  {reason:<16s} {cnt:>5d} {pct:>5.1f}% {wr:>5.1f}% {avg_r:>+6.2f}R")

    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="EMA High Signal -- Trailing SL Parameter Sweep",
    )
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument("--data-dir", default="data/candles")
    parser.add_argument("--daily", action="store_true",
                        help="Use daily candles")
    parser.add_argument("--strategy-a-only", action="store_true",
                        help="Compatibility flag (ignored)")
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

    symbols = args.symbols or SYMBOLS

    # Load candles
    interval = "1d" if args.daily else "15m"
    candles_by_symbol = await fetch_or_load_candles(
        assets=symbols,
        data_dir=data_dir,
        interval=interval,
    )

    if not candles_by_symbol:
        logger.error("No candle data available. Exiting.")
        sys.exit(1)

    logger.info("Loaded data for %d symbols", len(candles_by_symbol))

    # Build parameter grid
    param_grid: list[SweepParams] = []

    # v6 baseline
    param_grid.append(SweepParams(
        trail_start_r=0.0,
        trail_distance_r=0.0,
        max_hold_bars=5,
        strength_exit_r=0.0,
        violation_min_r=0.0,
        is_v6_baseline=True,
    ))

    # Full grid
    for ts, td, mh, se, vm in itertools.product(
        TRAIL_START_R, TRAIL_DISTANCE_R, MAX_HOLD_BARS, STRENGTH_EXIT_R, VIOLATION_MIN_R,
    ):
        param_grid.append(SweepParams(
            trail_start_r=ts,
            trail_distance_r=td,
            max_hold_bars=mh,
            strength_exit_r=se,
            violation_min_r=vm,
        ))

    total_combos = len(param_grid)
    logger.info("Total parameter combinations: %d (including v6 baseline)", total_combos)

    t0 = time.time()
    results = run_sweep(candles_by_symbol, param_grid)
    elapsed = time.time() - t0
    logger.info("Sweep completed in %.1f seconds (%.1f combos/sec)", elapsed, total_combos / elapsed)

    # Sort and display
    # Filter to combos with at least 10 trades
    valid = [sr for sr in results if sr.total_trades >= 10]
    logger.info("Valid combos (>=10 trades): %d / %d", len(valid), total_combos)

    # Top 20 by PF
    by_pf = sorted(valid, key=lambda x: x.profit_factor, reverse=True)
    print_table("TOP 20 BY PROFIT FACTOR", by_pf, top_n=20)

    # Top 20 by return
    by_ret = sorted(valid, key=lambda x: x.avg_return_per_asset, reverse=True)
    print_table("TOP 20 BY AVG RETURN PER ASSET", by_ret, top_n=20)

    # Comparison
    print_comparison(results)


if __name__ == "__main__":
    asyncio.run(main())
