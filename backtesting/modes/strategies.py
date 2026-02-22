"""Strategies mode: compare alternative entry strategies on all assets.

Replaces backtest_strategies.py (~568 lines).
Uses PortfolioSimulator instead of single-asset simulation.
"""

from __future__ import annotations

import argparse
import time
from typing import Callable

from backtesting.api import fetch_all_candles, get_all_assets
from backtesting.config import BacktestConfig, load_config
from backtesting.indicators import (
    calc_bollinger,
    calc_donchian,
    compute_indicators,
)
from backtesting.signals import (
    signal_ema_no_regime,
    signal_mean_reversion,
    signal_momentum_breakout,
    signal_rsi_reversal,
    signal_trend_momentum,
)
from backtesting.simulator import PortfolioSimulator
from backtesting.stats import (
    BacktestResult,
    print_comparison_table,
    print_results_json,
)

SignalFn = Callable[[dict, int, BacktestConfig], int]

STRATEGIES: list[tuple[str, SignalFn, bool]] = [
    ("Trend Momentum (live)", signal_trend_momentum, False),
    ("RSI Reversal", signal_rsi_reversal, False),
    ("EMA9/21 No Regime", signal_ema_no_regime, False),
    ("Momentum Breakout", signal_momentum_breakout, True),   # needs Donchian
    ("Mean Reversion", signal_mean_reversion, True),         # needs Bollinger
]


def run(args: argparse.Namespace) -> None:
    days = args.days if args.days is not None else 7
    tf = args.timeframe or "15m"
    cfg = load_config(timeframe=tf, lookback_days=days,
                      account_size=args.account or 86.0)

    tf_scale = {"5m": 3, "15m": 1, "1h": 1}.get(tf, 1)
    warmup = {"5m": 650, "15m": 200, "1h": 200}.get(tf, 200)
    cfg.warmup_bars = warmup

    print("=" * 80)
    print(f"BACKTEST STRATEGIES: 5 Strategies - Last {days}d - All Assets ({tf})")
    print("=" * 80)
    print(f"Config: TP {cfg.tp_pct*100:.1f}% | SL {cfg.sl_pct*100:.1f}% | "
          f"{cfg.leverage}x leverage | ${cfg.account_size} | "
          f"{cfg.position_pct*100:.0f}% size")
    print()

    now_ms = int(time.time() * 1000)
    extra_days = 4 if tf == "5m" else 3
    start_ms = now_ms - (days + extra_days) * 86_400_000
    signal_cutoff_ms = now_ms - days * 86_400_000

    assets = get_all_assets()
    asset_candles, _, _ = fetch_all_candles(
        assets, tf, start_ms, now_ms, cfg.exclude_symbols, warmup)

    if not asset_candles:
        print("No assets with enough data.")
        return

    # Compute base indicators + extras for some strategies
    asset_indicators: dict[str, dict] = {}
    for asset, candles in asset_candles.items():
        ind = compute_indicators(candles, cfg, tf_scale)
        # Add Bollinger and Donchian for strategies that need them
        _, bb_upper, bb_lower = calc_bollinger(ind["closes"], 20 * tf_scale)
        don_upper, don_lower = calc_donchian(ind["highs"], ind["lows"], 20 * tf_scale)
        ind["bb_upper"] = bb_upper
        ind["bb_lower"] = bb_lower
        ind["don_upper"] = don_upper
        ind["don_lower"] = don_lower
        asset_indicators[asset] = ind

    # Build timeline
    all_timestamps: set[int] = set()
    for candles in asset_candles.values():
        for c in candles:
            if c["t"] >= signal_cutoff_ms:
                all_timestamps.add(c["t"])
    timeline = sorted(all_timestamps)

    asset_time_idx: dict[str, dict[int, int]] = {}
    for asset, candles in asset_candles.items():
        asset_time_idx[asset] = {c["t"]: i for i, c in enumerate(candles)}

    # Run each strategy
    results: list[BacktestResult] = []
    for strat_name, signal_fn, _ in STRATEGIES:
        sim = PortfolioSimulator(cfg, label=strat_name)
        for ts in timeline:
            for sym in list(sim.open_positions.keys()):
                if ts in asset_time_idx.get(sym, {}):
                    sim.check_exits(sym, asset_candles[sym][asset_time_idx[sym][ts]])
            for asset in asset_candles:
                if ts not in asset_time_idx[asset]:
                    continue
                bar_idx = asset_time_idx[asset][ts]
                signal = signal_fn(asset_indicators[asset], bar_idx, cfg)
                if signal != 0:
                    sim.try_open(asset, signal, asset_candles[asset][bar_idx]["c"], ts)
        sim.force_close_all(asset_candles)
        results.append(BacktestResult.from_simulator(sim))

    if args.json:
        print_results_json(results)
    else:
        print_comparison_table(results)
        best = max(results, key=lambda r: r.net_pnl)
        print(f"\nBest by Net P&L: {best.label} (${best.net_pnl:+.2f})")
        best_wr = max((r for r in results if r.count > 0),
                      key=lambda r: r.win_rate, default=None)
        if best_wr:
            print(f"Best by Win Rate: {best_wr.label} ({best_wr.win_rate:.1f}%)")
