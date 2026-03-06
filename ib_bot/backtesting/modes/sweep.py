"""
IB Backtesting - Parameter Sweep Mode
=======================================

Tests multiple ORB configurations on the SAME cached data and prints
a comparison table to identify the best parameter set.

Usage:
    python -m ib_bot.backtesting sweep --days 30
    python -m ib_bot.backtesting sweep --days 60 --verbose
"""

from __future__ import annotations

import logging
from argparse import Namespace
from dataclasses import replace
from datetime import date, timedelta
from typing import Any, Dict, List, Tuple

from ..config import IBBacktestConfig, load_backtest_config
from ..data import IBDataFetcher
from ..orb_detector import detect_opening_range
from ..simulator import ORBSimulator
from ..stats import IBBacktestResult

logger = logging.getLogger(__name__)

# =========================================================================
# Sweep configurations
# =========================================================================

SWEEP_CONFIGS: List[Tuple[str, Dict[str, Any]]] = [
    (
        "A: baseline",
        {},
    ),
    (
        "B: wider range",
        {
            "min_range_ticks": 4,
            "max_range_ticks": 120,
        },
    ),
    (
        "C: longer entry",
        {
            "max_entry_time": "14:00",
        },
    ),
    (
        "D: relaxed all",
        {
            "min_range_ticks": 4,
            "max_range_ticks": 120,
            "max_entry_time": "14:00",
            "breakout_buffer_ticks": 1,
            "min_atr_ticks": 2,
        },
    ),
    (
        "E: more trades/day",
        {
            "min_range_ticks": 4,
            "max_range_ticks": 120,
            "max_entry_time": "14:00",
            "breakout_buffer_ticks": 1,
            "min_atr_ticks": 2,
            "max_trades_per_day": 4,
            "no_reentry_after_stop": False,
        },
    ),
    (
        "F: aggressive R:R",
        {
            "min_range_ticks": 4,
            "max_range_ticks": 120,
            "max_entry_time": "14:00",
            "breakout_buffer_ticks": 1,
            "min_atr_ticks": 2,
            "reward_risk_ratio": 2.0,
        },
    ),
]


def _build_configs(
    base: IBBacktestConfig,
) -> List[Tuple[str, IBBacktestConfig]]:
    """Build all sweep configs from the base config."""
    configs: List[Tuple[str, IBBacktestConfig]] = []
    for name, overrides in SWEEP_CONFIGS:
        cfg = replace(base, **overrides)
        configs.append((name, cfg))
    return configs


def _print_comparison_table(
    results: List[Tuple[str, IBBacktestResult]],
) -> None:
    """Print a side-by-side comparison table of all configs."""
    print()
    print("=" * 100)
    print("  PARAMETER SWEEP RESULTS")
    print("=" * 100)
    print()

    # Header
    header = (
        f"  {'Config':<22s} | {'#Trades':>7s} | {'Win%':>6s} | "
        f"{'Net P&L':>10s} | {'PF':>6s} | {'Sharpe':>7s} | {'MaxDD':>10s}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    best_pnl = -999_999.0
    best_name = ""

    for name, r in results:
        line = (
            f"  {name:<22s} | {r.count:>7d} | {r.win_rate:>5.1f}% | "
            f"${r.net_pnl:>+9,.2f} | {r.profit_factor:>6.2f} | "
            f"{r.sharpe:>7.2f} | ${r.max_drawdown:>9,.2f}"
        )
        print(line)

        if r.net_pnl > best_pnl:
            best_pnl = r.net_pnl
            best_name = name

    print()
    print(f"  >>> Best by Net P&L: {best_name} (${best_pnl:+,.2f})")
    print()

    # Also recommend best risk-adjusted (highest Sharpe with positive P&L)
    positive = [(n, r) for n, r in results if r.net_pnl > 0 and r.count >= 3]
    if positive:
        best_sharpe_name, best_sharpe_r = max(positive, key=lambda x: x[1].sharpe)
        print(
            f"  >>> Best risk-adjusted: {best_sharpe_name} "
            f"(Sharpe={best_sharpe_r.sharpe:.2f}, PF={best_sharpe_r.profit_factor:.2f})"
        )
        print()

    print("=" * 100)


async def run_sweep(args: Namespace) -> None:
    """Run parameter sweep: fetch data once, run N configs, compare results.

    Steps:
        1. Load base config from trading.yaml
        2. Build sweep config variants
        3. Fetch data ONCE for all symbols (MES + MNQ)
        4. Run ORBSimulator for each config on the same data
        5. Print comparison table
    """
    # Always include MNQ in sweep
    symbols = list(set(["MES", "MNQ"] + (args.symbols or [])))

    base_cfg = load_backtest_config(
        symbols=symbols,
        account_size=args.account,
        lookback_days=args.days,
    )

    configs = _build_configs(base_cfg)

    end_date = date.today()
    start_date = end_date - timedelta(days=base_cfg.lookback_days)

    print(f"\n  Sweep: {len(configs)} configs x {len(symbols)} symbols x {base_cfg.lookback_days}d")
    print(f"  Symbols: {symbols}")
    print(f"  Period: {start_date} -> {end_date}\n")

    # --- Fetch data ONCE ---
    fetcher = IBDataFetcher(
        host=args.ib_host,
        port=args.ib_port,
        client_id=2,
        cache_dir=base_cfg.cache_dir,
    )

    await fetcher.connect()

    try:
        bars_by_day: Dict[str, Dict[str, list]] = {}
        for symbol in symbols:
            symbol_bars = await fetcher.fetch_days(symbol, start_date, end_date)
            for day_str, bars in symbol_bars.items():
                bars_by_day.setdefault(day_str, {})[symbol] = bars
    finally:
        await fetcher.disconnect()

    logger.info("Data ready: %d trading days", len(bars_by_day))

    if not bars_by_day:
        logger.error("No data fetched. Check IB connection and symbol availability.")
        return

    # --- Run each config on the same data ---
    results: List[Tuple[str, IBBacktestResult]] = []

    for name, cfg in configs:
        # Ensure symbols list matches what we fetched
        cfg = replace(cfg, symbols=symbols)

        logger.info("Running config: %s", name)
        sim = ORBSimulator(cfg)
        sim.run(bars_by_day, detect_opening_range)

        result = IBBacktestResult(
            label=name,
            trades=sim.trades,
            equity_curve=sim.equity_curve,
            daily_results=sim.daily_results,
            initial_equity=cfg.account_size,
        )
        results.append((name, result))

        logger.info(
            "  %s: %d trades, P&L=$%.2f",
            name, result.count, result.net_pnl,
        )

    # --- Print comparison ---
    _print_comparison_table(results)

    # Optionally print individual trade logs
    if getattr(args, "trades", False):
        from ..stats import print_summary, print_trade_log

        for name, result in results:
            print_summary(result)
            print_trade_log(result)
