"""Sizing mode: compare position sizing configs on all assets.

Replaces backtest_sizing.py (~573 lines).
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone

from backtesting.api import fetch_all_candles, get_all_assets
from backtesting.config import load_config
from backtesting.indicators import compute_indicators
from backtesting.signals import signal_trend_momentum
from backtesting.simulator import PortfolioSimulator
from backtesting.stats import (
    BacktestResult,
    print_comparison_table,
    print_results_json,
    print_top_bottom_trades,
)

# Sizing configs to compare
CONFIGS = [
    {"name": "A) Attuale (10%/10x/3)",     "pct": 0.10, "leverage": 10, "max_pos": 3},
    {"name": "B) 15% size (15%/10x/3)",    "pct": 0.15, "leverage": 10, "max_pos": 3},
    {"name": "C) 20x leva (10%/20x/3)",    "pct": 0.10, "leverage": 20, "max_pos": 3},
    {"name": "D) Concentrato (15%/10x/2)", "pct": 0.15, "leverage": 10, "max_pos": 2},
    {"name": "E) Aggressivo (10%/15x/2)",  "pct": 0.10, "leverage": 15, "max_pos": 2},
]


def run(args: argparse.Namespace) -> None:
    days = args.days if args.days is not None else 1
    tf = args.timeframe or "5m"
    cfg = load_config(
        timeframe=tf,
        lookback_days=days,
        account_size=args.account or 86.0,
    )

    # Scale indicator periods for sub-15m timeframes
    tf_scale = {"5m": 3, "15m": 1, "1h": 1}.get(tf, 1)
    warmup = {
        "5m": 650,  # EMA200@15m = EMA600@5m
        "15m": 200,
        "1h": 200,
    }.get(tf, 200)
    cfg.warmup_bars = warmup

    print("=" * 80)
    print(f"BACKTEST SIZING: Trend Momentum - Last {days}d - All Assets ({tf})")
    print("=" * 80)
    print(f"Strategy: EMA9/EMA21 + RSI + TREND regime (ADX entry>={cfg.trend_adx_entry_min}, "
          f"exit>={cfg.trend_adx_exit_min})")
    print(f"TP: {cfg.tp_pct*100:.1f}% | SL: {cfg.sl_pct*100:.1f}% | "
          f"Fees: {cfg.fee_pct*100:.2f}%/side")
    print(f"Account: ${cfg.account_size} | Max {cfg.max_daily_trades} trades/day")
    print()

    now_ms = int(time.time() * 1000)
    extra_days = 4 if tf == "5m" else 3
    start_ms = now_ms - (days + extra_days) * 86_400_000
    signal_cutoff_ms = now_ms - days * 86_400_000

    # Fetch
    assets = get_all_assets()
    asset_candles, errors, skipped = fetch_all_candles(
        assets, tf, start_ms, now_ms, cfg.exclude_symbols, warmup)

    if not asset_candles:
        print("No assets with enough data. Exiting.")
        return

    # Compute indicators (once, shared across sizing configs)
    asset_indicators: dict[str, dict] = {}
    for asset, candles in asset_candles.items():
        asset_indicators[asset] = compute_indicators(candles, cfg, tf_scale)

    # Build unified timeline
    all_timestamps: set[int] = set()
    for candles in asset_candles.values():
        for c in candles:
            if c["t"] >= signal_cutoff_ms:
                all_timestamps.add(c["t"])
    timeline = sorted(all_timestamps)

    t_start = datetime.fromtimestamp(timeline[0] / 1000, tz=timezone.utc)
    t_end = datetime.fromtimestamp(timeline[-1] / 1000, tz=timezone.utc)
    print(f"Signal window: {len(timeline)} bars, "
          f"{t_start:%Y-%m-%d %H:%M} -> {t_end:%Y-%m-%d %H:%M} UTC")
    print()

    # Pre-build timestamp -> bar_idx lookup
    asset_time_idx: dict[str, dict[int, int]] = {}
    for asset, candles in asset_candles.items():
        asset_time_idx[asset] = {c["t"]: i for i, c in enumerate(candles)}

    # Run each sizing config
    results: list[BacktestResult] = []
    for sc in CONFIGS:
        sim = PortfolioSimulator(
            cfg, label=sc["name"],
            position_pct=sc["pct"], leverage=sc["leverage"],
            max_positions=sc["max_pos"],
        )
        for ts in timeline:
            for sym in list(sim.open_positions.keys()):
                if ts in asset_time_idx.get(sym, {}):
                    sim.check_exits(sym, asset_candles[sym][asset_time_idx[sym][ts]])
            for asset in asset_candles:
                if ts not in asset_time_idx[asset]:
                    continue
                bar_idx = asset_time_idx[asset][ts]
                signal = signal_trend_momentum(asset_indicators[asset], bar_idx, cfg)
                if signal != 0:
                    sim.try_open(asset, signal, asset_candles[asset][bar_idx]["c"], ts)
        sim.force_close_all(asset_candles)
        results.append(BacktestResult.from_simulator(sim))

    # Output
    if args.json:
        print_results_json(results)
    else:
        print_comparison_table(results)
        if results[0].trades:
            print_top_bottom_trades(results[0].trades, results[0].label)

        # Notional comparison
        print()
        print("Position sizing math:")
        print("-" * 85)
        print(f"  {'Config':<28} {'Notional':>9} {'TP win':>9} {'SL loss':>9} "
              f"{'Fees RT':>9} {'Net win':>9}")
        for sc in CONFIGS:
            n = cfg.account_size * sc["pct"] * sc["leverage"]
            tp_w = n * cfg.tp_pct
            sl_l = n * cfg.sl_pct
            fees = n * cfg.fee_pct * 2
            print(f"  {sc['name']:<28} ${n:>7.1f}  ${tp_w:>7.3f}  "
                  f"${sl_l:>7.3f}  ${fees:>7.3f}  ${tp_w - fees:>7.3f}")
