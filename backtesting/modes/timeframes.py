"""Timeframes mode: compare 5m/15m/1h on all assets.

Replaces backtest_timeframes.py (~635 lines).
Fixes: slope 0.001 -> 0.0003 (from config), adds hysteresis.
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta, timezone

import numpy as np

from backtesting.api import fetch_all_candles, get_all_assets_with_info
from backtesting.config import load_config
from backtesting.indicators import compute_indicators
from backtesting.signals import signal_trend_momentum
from backtesting.simulator import PortfolioSimulator
from backtesting.stats import (
    BacktestResult,
    print_comparison_table,
    print_results_json,
)

TIMEFRAMES = ["5m", "15m", "1h"]
TF_CONFIG = {
    "5m":  {"scale": 3, "warmup": 650, "fetch_extra_days": 5},
    "15m": {"scale": 1, "warmup": 200, "fetch_extra_days": 3},
    "1h":  {"scale": 1, "warmup": 200, "fetch_extra_days": 10},
}


def run(args: argparse.Namespace) -> None:
    days = args.days if args.days is not None else 7
    cfg_base = load_config(lookback_days=days,
                           account_size=args.account)

    print("=" * 80)
    print(f"TIMEFRAME BACKTEST: 5m vs 15m vs 1h - Last {days}d")
    print("=" * 80)
    print(f"Strategy: EMA9/EMA21 + RSI + Regime (ADX entry>={cfg_base.trend_adx_entry_min}, "
          f"exit>={cfg_base.trend_adx_exit_min})")
    print(f"TP: {cfg_base.tp_pct*100:.1f}% | SL: {cfg_base.sl_pct*100:.1f}% | "
          f"Fees: entry {cfg_base.entry_fee_pct*100:.3f}% + exit {cfg_base.exit_fee_pct*100:.3f}%/side")
    print(f"Account: ${cfg_base.account_size} | {cfg_base.leverage}x | "
          f"{cfg_base.position_pct*100:.0f}% size | "
          f"Max {cfg_base.max_daily_trades} trades/day")
    print("=" * 80)
    print()

    now_ms = int(time.time() * 1000)
    signal_cutoff_ms = now_ms - days * 86_400_000

    # Fetch assets once
    all_assets, leverage_caps = get_all_assets_with_info()

    results: list[BacktestResult] = []

    for tf in TIMEFRAMES:
        tc = TF_CONFIG[tf]
        cfg = load_config(
            timeframe=tf,
            lookback_days=days,
            account_size=args.account or 86.0,
        )
        cfg.warmup_bars = tc["warmup"]
        start_ms = now_ms - (days + tc["fetch_extra_days"]) * 86_400_000

        print(f"--- Timeframe: {tf} ---")
        asset_candles, errors, skipped = fetch_all_candles(
            all_assets, tf, start_ms, now_ms,
            cfg.exclude_symbols, tc["warmup"],
        )

        if not asset_candles:
            print(f"  No data for {tf}")
            results.append(BacktestResult(label=tf))
            continue

        # Compute indicators
        asset_indicators: dict[str, dict] = {}
        for asset, candles in asset_candles.items():
            asset_indicators[asset] = compute_indicators(
                candles, cfg, tc["scale"])

        # Build timeline
        all_timestamps: set[int] = set()
        for candles in asset_candles.values():
            for c in candles:
                if c["t"] >= signal_cutoff_ms:
                    all_timestamps.add(c["t"])
        timeline = sorted(all_timestamps)

        if not timeline:
            results.append(BacktestResult(label=tf))
            continue

        asset_time_idx: dict[str, dict[int, int]] = {}
        for asset, candles in asset_candles.items():
            asset_time_idx[asset] = {c["t"]: i for i, c in enumerate(candles)}

        # Run simulation
        sim = PortfolioSimulator(cfg, label=tf, leverage_caps=leverage_caps)
        for ts in timeline:
            for sym in list(sim.open_positions.keys()):
                if ts in asset_time_idx.get(sym, {}):
                    sim.check_exits(sym, asset_candles[sym][asset_time_idx[sym][ts]])
            for asset in asset_candles:
                if ts not in asset_time_idx[asset]:
                    continue
                bar_idx = asset_time_idx[asset][ts]
                signal = signal_trend_momentum(
                    asset_indicators[asset], bar_idx, cfg)
                if signal != 0:
                    sim.try_open(asset, signal,
                                asset_candles[asset][bar_idx]["c"], ts)

        sim.force_close_all(asset_candles)
        results.append(BacktestResult.from_simulator(sim))

    # Output
    if args.json:
        print_results_json(results)
    else:
        print()
        print_comparison_table(results)

        # Recommendation
        print()
        print("RECOMMENDATION")
        print("-" * 40)
        valid = [r for r in results if r.count > 0]
        if valid:
            best_pnl = max(valid, key=lambda r: r.net_pnl)
            best_wr = max(valid, key=lambda r: r.win_rate)
            most_trades = max(valid, key=lambda r: r.count)
            print(f"Best PnL:      {best_pnl.label} (${best_pnl.net_pnl:+.2f})")
            print(f"Best Win Rate: {best_wr.label} ({best_wr.win_rate:.1f}%)")
            print(f"Most Trades:   {most_trades.label} ({most_trades.count} trades)")
