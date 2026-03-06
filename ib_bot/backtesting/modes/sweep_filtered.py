"""
IB Backtesting - Filtered Parameter Sweep Mode
================================================

Tests ORB configs with regime/volatility filters (EMA trend, ATR percentile,
VWAP slope) on cached data and prints a comparison table.

Usage:
    python -m ib_bot.backtesting sweep-filtered --days 90
    python -m ib_bot.backtesting sweep-filtered --days 90 --verbose
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
from ..simulator_filtered import FilteredORBSimulator
from ..stats import IBBacktestResult

logger = logging.getLogger(__name__)

# =========================================================================
# Config F params (base for most filtered configs)
# =========================================================================
_CONFIG_F_PARAMS: Dict[str, Any] = {
    "min_range_ticks": 4,
    "max_range_ticks": 120,
    "max_entry_time": "14:00",
    "breakout_buffer_ticks": 1,
    "min_atr_ticks": 2,
    "reward_risk_ratio": 2.0,
}

# Config B params (wider range, rest default)
_CONFIG_B_PARAMS: Dict[str, Any] = {
    "min_range_ticks": 4,
    "max_range_ticks": 120,
}

# =========================================================================
# Sweep configurations with filters
# =========================================================================

SWEEP_FILTERED_CONFIGS: List[Tuple[str, Dict[str, Any]]] = [
    (
        "G: EMA trend only",
        {
            **_CONFIG_F_PARAMS,
            "ema_trend_filter": True,
            "ema_period": 20,
        },
    ),
    (
        "H: ATR filter only",
        {
            **_CONFIG_F_PARAMS,
            "atr_percentile_filter": True,
            "atr_low_pct": 20.0,
            "atr_high_pct": 80.0,
        },
    ),
    (
        "I: VWAP slope only",
        {
            **_CONFIG_F_PARAMS,
            "vwap_slope_filter": True,
            "vwap_min_slope_ticks": 0.5,
        },
    ),
    (
        "J: All filters",
        {
            **_CONFIG_F_PARAMS,
            "ema_trend_filter": True,
            "ema_period": 20,
            "atr_percentile_filter": True,
            "atr_low_pct": 20.0,
            "atr_high_pct": 80.0,
            "vwap_slope_filter": True,
            "vwap_min_slope_ticks": 0.5,
        },
    ),
    (
        "K: EMA + ATR",
        {
            **_CONFIG_F_PARAMS,
            "ema_trend_filter": True,
            "ema_period": 20,
            "atr_percentile_filter": True,
            "atr_low_pct": 20.0,
            "atr_high_pct": 80.0,
        },
    ),
    (
        "L: Conservative all",
        {
            **_CONFIG_B_PARAMS,
            "reward_risk_ratio": 2.0,
            "ema_trend_filter": True,
            "ema_period": 20,
            "atr_percentile_filter": True,
            "atr_low_pct": 20.0,
            "atr_high_pct": 80.0,
            "vwap_slope_filter": True,
            "vwap_min_slope_ticks": 0.5,
        },
    ),
]


def _build_configs(
    base: IBBacktestConfig,
) -> List[Tuple[str, IBBacktestConfig]]:
    """Build all filtered sweep configs from the base config."""
    configs: List[Tuple[str, IBBacktestConfig]] = []
    for name, overrides in SWEEP_FILTERED_CONFIGS:
        cfg = replace(base, **overrides)
        configs.append((name, cfg))
    return configs


def _print_comparison_table(
    results: List[Tuple[str, IBBacktestResult, Dict[str, int]]],
) -> None:
    """Print a side-by-side comparison table with filter stats."""
    print()
    print("=" * 120)
    print("  FILTERED PARAMETER SWEEP RESULTS")
    print("=" * 120)
    print()

    # Header
    header = (
        f"  {'Config':<22s} | {'#Trades':>7s} | {'Win%':>6s} | "
        f"{'Net P&L':>10s} | {'PF':>6s} | {'Sharpe':>7s} | {'MaxDD':>10s} | "
        f"{'EMA-F':>5s} | {'ATR-F':>5s} | {'VWAP-F':>6s}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    best_pnl = -999_999.0
    best_name = ""

    for name, r, fstats in results:
        line = (
            f"  {name:<22s} | {r.count:>7d} | {r.win_rate:>5.1f}% | "
            f"${r.net_pnl:>+9,.2f} | {r.profit_factor:>6.2f} | "
            f"{r.sharpe:>7.2f} | ${r.max_drawdown:>9,.2f} | "
            f"{fstats.get('ema_filtered', 0):>5d} | "
            f"{fstats.get('atr_filtered', 0):>5d} | "
            f"{fstats.get('vwap_filtered', 0):>6d}"
        )
        print(line)

        if r.net_pnl > best_pnl:
            best_pnl = r.net_pnl
            best_name = name

    print()
    print(f"  >>> Best by Net P&L: {best_name} (${best_pnl:+,.2f})")
    print()

    # Best risk-adjusted
    positive = [(n, r) for n, r, _ in results if r.net_pnl > 0 and r.count >= 3]
    if positive:
        best_sharpe_name, best_sharpe_r = max(positive, key=lambda x: x[1].sharpe)
        print(
            f"  >>> Best risk-adjusted: {best_sharpe_name} "
            f"(Sharpe={best_sharpe_r.sharpe:.2f}, PF={best_sharpe_r.profit_factor:.2f})"
        )
        print()

    print("=" * 120)


async def run_sweep_filtered(args: Namespace) -> None:
    """Run filtered parameter sweep: fetch data once, run N configs, compare.

    Steps:
        1. Load base config from trading.yaml
        2. Build filtered sweep config variants
        3. Fetch data ONCE for all symbols (MES + MNQ)
        4. Run FilteredORBSimulator for each config on the same data
        5. Print comparison table with filter statistics
    """
    symbols = list(set(["MES", "MNQ"] + (args.symbols or [])))

    base_cfg = load_backtest_config(
        symbols=symbols,
        account_size=args.account,
        lookback_days=args.days,
    )

    configs = _build_configs(base_cfg)

    end_date = date.today()
    start_date = end_date - timedelta(days=base_cfg.lookback_days)

    print(f"\n  Filtered Sweep: {len(configs)} configs x {len(symbols)} symbols x {base_cfg.lookback_days}d")
    print(f"  Symbols: {symbols}")
    print(f"  Period: {start_date} -> {end_date}\n")

    # --- Fetch data ONCE ---
    fetcher = IBDataFetcher(
        host=args.ib_host,
        port=args.ib_port,
        client_id=3,  # different client_id to avoid collisions
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
    results: List[Tuple[str, IBBacktestResult, Dict[str, int]]] = []

    for name, cfg in configs:
        cfg = replace(cfg, symbols=symbols)

        logger.info("Running config: %s", name)
        sim = FilteredORBSimulator(cfg)
        sim.run(bars_by_day, detect_opening_range)

        result = IBBacktestResult(
            label=name,
            trades=sim.trades,
            equity_curve=sim.equity_curve,
            daily_results=sim.daily_results,
            initial_equity=cfg.account_size,
        )
        filter_stats = sim.get_filter_stats()
        results.append((name, result, filter_stats))

        logger.info(
            "  %s: %d trades, P&L=$%.2f, filters=%s",
            name, result.count, result.net_pnl, filter_stats,
        )

    # --- Print comparison ---
    _print_comparison_table(results)

    # Optionally print individual trade logs
    if getattr(args, "trades", False):
        from ..stats import print_summary, print_trade_log

        for name, result, _ in results:
            print_summary(result)
            print_trade_log(result)
