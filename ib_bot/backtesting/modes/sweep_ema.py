"""
IB Backtesting - EMA Momentum Parameter Sweep
================================================

Tests multiple EMA crossover configurations on cached MES/MNQ data
and prints a comparison table.

Usage:
    python -m ib_bot.backtesting sweep-ema --days 90
    python -m ib_bot.backtesting sweep-ema --days 90 --verbose --trades
"""

from __future__ import annotations

import logging
from argparse import Namespace
from dataclasses import replace
from datetime import date, timedelta
from typing import Any, Dict, List, Tuple

from ..data import IBDataFetcher
from ..simulator_ema import EMASimulator, EMAStrategyConfig
from ..stats import IBBacktestResult

logger = logging.getLogger(__name__)

# =========================================================================
# Sweep configurations
# =========================================================================

SWEEP_CONFIGS: List[Tuple[str, Dict[str, Any]]] = [
    (
        "EMA-A: baseline",
        {
            # EMA9/21, RSI filter, R:R 1.5, ATR stop 2x
        },
    ),
    (
        "EMA-B: tight stops",
        {
            "atr_stop_multiplier": 1.5,
            "reward_risk_ratio": 1.5,
        },
    ),
    (
        "EMA-C: wide stops",
        {
            "atr_stop_multiplier": 3.0,
            "reward_risk_ratio": 2.0,
        },
    ),
    (
        "EMA-D: fast EMA",
        {
            "ema_fast": 5,
            "ema_slow": 13,
            "reward_risk_ratio": 1.5,
        },
    ),
    (
        "EMA-E: no RSI filter",
        {
            "rsi_long_min": 0.0,
            "rsi_long_max": 100.0,
            "rsi_short_min": 0.0,
            "rsi_short_max": 100.0,
        },
    ),
    (
        "EMA-F: aggressive",
        {
            "ema_fast": 5,
            "ema_slow": 13,
            "rsi_long_min": 0.0,
            "rsi_long_max": 100.0,
            "rsi_short_min": 0.0,
            "rsi_short_max": 100.0,
            "reward_risk_ratio": 2.0,
            "max_trades_per_day": 6,
        },
    ),
]


def _build_configs(
    base: EMAStrategyConfig,
) -> List[Tuple[str, EMAStrategyConfig]]:
    """Build all sweep configs from the base config."""
    configs: List[Tuple[str, EMAStrategyConfig]] = []
    for name, overrides in SWEEP_CONFIGS:
        cfg = replace(base, **overrides)
        configs.append((name, cfg))
    return configs


def _print_comparison_table(
    results: List[Tuple[str, IBBacktestResult]],
) -> None:
    """Print a side-by-side comparison table of all configs."""
    print()
    print("=" * 110)
    print("  EMA MOMENTUM SWEEP RESULTS")
    print("=" * 110)
    print()

    # Header
    header = (
        f"  {'Config':<24s} | {'#Trades':>7s} | {'Win%':>6s} | "
        f"{'Net P&L':>10s} | {'PF':>6s} | {'Sharpe':>7s} | "
        f"{'MaxDD':>10s} | {'AvgWin':>8s} | {'AvgLoss':>8s}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    best_pnl = -999_999.0
    best_name = ""

    for name, r in results:
        line = (
            f"  {name:<24s} | {r.count:>7d} | {r.win_rate:>5.1f}% | "
            f"${r.net_pnl:>+9,.2f} | {r.profit_factor:>6.2f} | "
            f"{r.sharpe:>7.2f} | ${r.max_drawdown:>9,.2f} | "
            f"${r.avg_win:>7,.2f} | ${r.avg_loss:>7,.2f}"
        )
        print(line)

        if r.net_pnl > best_pnl:
            best_pnl = r.net_pnl
            best_name = name

    print()
    print(f"  >>> Best by Net P&L: {best_name} (${best_pnl:+,.2f})")

    # Best risk-adjusted
    positive = [(n, r) for n, r in results if r.net_pnl > 0 and r.count >= 3]
    if positive:
        best_sharpe_name, best_sharpe_r = max(positive, key=lambda x: x[1].sharpe)
        print(
            f"  >>> Best risk-adjusted: {best_sharpe_name} "
            f"(Sharpe={best_sharpe_r.sharpe:.2f}, PF={best_sharpe_r.profit_factor:.2f})"
        )

    print()
    print("=" * 110)


async def run_sweep_ema(args: Namespace) -> None:
    """Run EMA momentum parameter sweep.

    Steps:
        1. Build EMA strategy config variants
        2. Fetch data ONCE for MES + MNQ
        3. Run EMASimulator for each config
        4. Print comparison table
    """
    symbols = list(set(["MES", "MNQ"] + (args.symbols or [])))

    base_cfg = EMAStrategyConfig(
        symbols=symbols,
        account_size=args.account,
        lookback_days=args.days,
    )

    configs = _build_configs(base_cfg)

    end_date = date.today()
    start_date = end_date - timedelta(days=base_cfg.lookback_days)

    print(f"\n  EMA Sweep: {len(configs)} configs x {len(symbols)} symbols x {base_cfg.lookback_days}d")
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
        logger.error("No data fetched. Check IB connection and cached data.")
        return

    # --- Run each config ---
    results: List[Tuple[str, IBBacktestResult]] = []

    for name, cfg in configs:
        cfg = replace(cfg, symbols=symbols)

        logger.info("Running config: %s", name)
        sim = EMASimulator(cfg)
        sim.run(bars_by_day)

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
