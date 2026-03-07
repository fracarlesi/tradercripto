#!/usr/bin/env python3
"""Grid replay: fetch data ONCE, test multiple configs.

Avoids hitting Hyperliquid API rate limits by reusing the same candle data
across all parameter combinations.
"""
from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# IMPORTANT: Import LightGBM before XGBoost to avoid libomp SIGSEGV on macOS ARM64
try:
    import lightgbm  # noqa: F401
except ImportError:
    pass

from backtesting.api import fetch_all_candles, get_all_assets_with_info
from backtesting.config import BacktestConfig, load_config
from backtesting.indicators import calc_bollinger, compute_indicators
from backtesting.modes.threshold import _extract_features, _is_chaos_or_trend, _predict
from backtesting.signals import (
    signal_ema_crossover_entry,
    signal_momentum_burst_entry,
    signal_volume_breakout_entry,
)
from backtesting.simulator import ReplaySimulator
from backtesting.stats import BacktestResult


@dataclass
class GridConfig:
    label: str
    threshold: float
    tp_pct: float      # e.g. 3.5 means 3.5%
    sl_pct: float      # e.g. 1.0 means 1.0%
    breakeven: float    # 99 = disabled
    trailing: float     # 0 = disabled


# ── Configs to test ──────────────────────────────────────────────────────
CONFIGS = [
    # Fine grid around winner (T=0.58, TP=3.5, SL=1.0)
    # Vary threshold
    GridConfig("T=0.52 TP=3.5 SL=1.0", 0.52, 3.5, 1.0, 99, 0),
    GridConfig("T=0.54 TP=3.5 SL=1.0", 0.54, 3.5, 1.0, 99, 0),
    GridConfig("T=0.56 TP=3.5 SL=1.0", 0.56, 3.5, 1.0, 99, 0),
    GridConfig("T=0.58 TP=3.5 SL=1.0", 0.58, 3.5, 1.0, 99, 0),  # previous winner
    # Vary TP with T=0.58
    GridConfig("T=0.58 TP=3.0 SL=1.0", 0.58, 3.0, 1.0, 99, 0),
    GridConfig("T=0.58 TP=4.0 SL=1.0", 0.58, 4.0, 1.0, 99, 0),
    GridConfig("T=0.58 TP=4.5 SL=1.0", 0.58, 4.5, 1.0, 99, 0),
    # Vary SL with T=0.58, TP=3.5
    GridConfig("T=0.58 TP=3.5 SL=0.8", 0.58, 3.5, 0.8, 99, 0),
    GridConfig("T=0.58 TP=3.5 SL=1.2", 0.58, 3.5, 1.2, 99, 0),
    GridConfig("T=0.58 TP=3.5 SL=1.5", 0.58, 3.5, 1.5, 99, 0),
    # Best combos with lower threshold (more trades)
    GridConfig("T=0.54 TP=4.0 SL=1.0", 0.54, 4.0, 1.0, 99, 0),
    GridConfig("T=0.56 TP=4.0 SL=1.0", 0.56, 4.0, 1.0, 99, 0),
]

DAYS = 90
TF = "15m"


def main() -> None:
    base_cfg = load_config(timeframe=TF, lookback_days=DAYS)
    warmup = 200
    base_cfg.warmup_bars = warmup

    # ── 1. Fetch data ONCE ───────────────────────────────────────────────
    print("=" * 90)
    print(f"GRID REPLAY: {len(CONFIGS)} configs x {DAYS}d ({TF})")
    print("=" * 90)
    print("\nStep 1: Fetching data (one time only)...")

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (DAYS + 3) * 86_400_000
    signal_cutoff_ms = now_ms - DAYS * 86_400_000

    assets, leverage_caps = get_all_assets_with_info()
    asset_candles, errors, skipped = fetch_all_candles(
        assets, TF, start_ms, now_ms, base_cfg.exclude_symbols, warmup
    )

    if not asset_candles:
        print("No assets with enough data.")
        return

    # ── 2. Compute indicators ONCE ───────────────────────────────────────
    print("Step 2: Computing indicators...")
    tf_scale = 1
    asset_indicators: dict[str, dict] = {}
    asset_bb: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for asset, candles in asset_candles.items():
        ind = compute_indicators(candles, base_cfg, tf_scale)
        _, bb_upper, bb_lower = calc_bollinger(ind["closes"], 20 * tf_scale)
        asset_indicators[asset] = ind
        asset_bb[asset] = (bb_upper, bb_lower)

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

    # ── 3. Load ML model ONCE ────────────────────────────────────────────
    print("Step 3: Loading ML model...")
    model_path = Path(__file__).resolve().parent / "models" / "trade_model.joblib"
    import joblib
    payload = joblib.load(model_path)
    model = payload["model"]
    lgb_model = payload.get("lgb_model", None)
    feature_names = (
        list(model.get_booster().feature_names)
        if model.get_booster().feature_names else None
    )
    has_breakout = getattr(model, "n_features_in_", 13) >= 13
    ensemble_str = "xgb+lgb" if lgb_model else "xgb"
    print(f"  Model: {getattr(model, 'n_features_in_', '?')} features, "
          f"ensemble={ensemble_str}")

    # ── 4. Score ALL signals ONCE (without ATR gate — applied per config) ─
    print("Step 4: Scoring all signals...")

    # Store: (timestamp, asset, direction, proba, signal_label, atr_pct)
    scored: list[tuple[int, str, int, float, str, float]] = []

    for asset in asset_candles:
        candles = asset_candles[asset]
        ind = asset_indicators[asset]
        bb_upper, bb_lower = asset_bb[asset]

        for ts in timeline:
            if ts not in asset_time_idx[asset]:
                continue
            bar_idx = asset_time_idx[asset][ts]
            if bar_idx < warmup:
                continue

            atr_pct_val = ind["atr_pct"][bar_idx]
            atr_pct_f = float(atr_pct_val) if not np.isnan(atr_pct_val) else 0.0

            # PATH 1: EMA crossover (TREND only)
            if ind["is_trend"][bar_idx]:
                sig = signal_ema_crossover_entry(ind, bar_idx)
                if sig != 0:
                    rsi_val = ind["rsi"][bar_idx]
                    if sig == -1 and not np.isnan(rsi_val) and rsi_val < 35:
                        sig = 0
                if sig != 0:
                    feats = _extract_features(candles, ind, bar_idx, bb_upper, bb_lower, 0.0)
                    proba = _predict(model, feats, feature_names, lgb_model) if feats else 0.0
                    scored.append((ts, asset, sig, proba, "ema", atr_pct_f))

            # PATH 2: Volume breakout
            if has_breakout and _is_chaos_or_trend(ind, bar_idx):
                sig_vb = signal_volume_breakout_entry(ind, bar_idx)
                if sig_vb != 0:
                    feats = _extract_features(candles, ind, bar_idx, bb_upper, bb_lower, 1.0)
                    proba = _predict(model, feats, feature_names, lgb_model) if feats else 0.0
                    scored.append((ts, asset, sig_vb, proba, "vb", atr_pct_f))

            # PATH 3: Momentum burst
            if _is_chaos_or_trend(ind, bar_idx):
                sig_mb = signal_momentum_burst_entry(ind, bar_idx)
                if sig_mb != 0:
                    feats = _extract_features(candles, ind, bar_idx, bb_upper, bb_lower, 2.0)
                    proba = _predict(model, feats, feature_names, lgb_model) if feats else 0.0
                    scored.append((ts, asset, sig_mb, proba, "mb", atr_pct_f))

    print(f"  Total signals scored: {len(scored)}")
    scored.sort(key=lambda x: x[0])

    # ── 5. Run simulation for EACH config ────────────────────────────────
    print(f"\nStep 5: Simulating {len(CONFIGS)} configs...\n")
    results: list[tuple[GridConfig, BacktestResult | None, dict]] = []

    for gc in CONFIGS:
        print(f"  Simulating: {gc.label} ...", end="", flush=True)

        cfg = copy.deepcopy(base_cfg)
        cfg.tp_pct = gc.tp_pct / 100.0
        cfg.sl_pct = gc.sl_pct / 100.0
        cfg.breakeven_threshold_pct = gc.breakeven / 100.0
        cfg.trailing_atr_mult = gc.trailing

        # Apply ATR gate and threshold filter per config
        sl_pct_100 = gc.sl_pct  # already in % form
        deduped: dict[int, dict[str, tuple[int, float, str]]] = {}
        for ts, asset, direction, proba, label, atr_pct in scored:
            # ATR gate: skip if ATR > SL %
            if atr_pct > sl_pct_100:
                continue
            # Threshold gate
            if proba < gc.threshold:
                continue
            if ts not in deduped:
                deduped[ts] = {}
            existing = deduped[ts].get(asset)
            if existing is None or proba > existing[1]:
                deduped[ts][asset] = (direction, proba, label)

        # Run simulation
        sim = ReplaySimulator(
            cfg, label=gc.label, leverage_caps=leverage_caps,
            timeframe=TF, use_kelly=False,
        )

        for ts in timeline:
            # Exits first
            for sym in list(sim.open_positions.keys()):
                if ts not in asset_time_idx.get(sym, {}):
                    continue
                bar_idx = asset_time_idx[sym][ts]
                candle = asset_candles[sym][bar_idx]
                ind = asset_indicators[sym]
                current_regime = bool(ind["is_trend"][bar_idx])
                rsi_slope_val = ind["rsi_slope"][bar_idx]
                current_rsi_slope = float(rsi_slope_val) if not np.isnan(rsi_slope_val) else 0.0
                sim.check_exits(sym, candle, current_regime, current_rsi_slope)

            # Entries
            if ts in deduped:
                for asset, (direction, proba, _label) in deduped[ts].items():
                    if asset not in asset_time_idx or ts not in asset_time_idx[asset]:
                        continue
                    bar_idx = asset_time_idx[asset][ts]
                    candle = asset_candles[asset][bar_idx]
                    ind = asset_indicators[asset]
                    entry_price = candle["c"]
                    entry_regime = bool(ind["is_trend"][bar_idx])
                    rsi_slope_val = ind["rsi_slope"][bar_idx]
                    entry_rsi_slope = float(rsi_slope_val) if not np.isnan(rsi_slope_val) else 0.0
                    entry_atr_pct = float(ind["atr_pct"][bar_idx])
                    sim.try_open(
                        asset, direction, entry_price, ts,
                        ml_proba=proba, entry_regime=entry_regime,
                        _entry_rsi_slope=entry_rsi_slope,
                        entry_atr_pct=entry_atr_pct,
                    )

        sim.force_close_all(asset_candles)
        result = BacktestResult.from_simulator(sim, gc.label)
        results.append((gc, result, sim.exit_reasons))
        print(f" {result.count} trades, P&L ${result.net_pnl:+.2f}")

    # ── 6. Print comparison table ────────────────────────────────────────
    print("\n\n")
    print("=" * 110)
    print(f"GRID REPLAY RESULTS: Pure TP/SL (breakeven=OFF, trailing=OFF) -- {DAYS} days")
    print("=" * 110)
    header = (f"{'Config':<24} {'Trades':>7} {'Wins':>5} {'WinRate':>8} "
              f"{'Net P&L':>10} {'Fees':>8} {'MaxDD':>8} {'PF':>6} "
              f"{'Sharpe':>7} {'Assets':>7}")
    print(header)
    print("-" * 110)

    for gc, result, _ in results:
        if result is None:
            print(f"{gc.label:<24} {'FAILED':>7}")
            continue
        print(f"{gc.label:<24} "
              f"{result.count:>7} "
              f"{result.wins:>5} "
              f"{result.win_rate:>7.1f}% "
              f"${result.net_pnl:>+9.2f} "
              f"${result.total_fees:>7.2f} "
              f"${result.max_drawdown:>7.2f} "
              f"{result.profit_factor:>6.2f} "
              f"{result.sharpe:>7.2f} "
              f"{result.unique_assets:>7}")

    # Exit reasons
    print(f"\n{'EXIT REASONS':}")
    print("-" * 90)
    print(f"{'Config':<24} {'SL':>6} {'TP':>6} {'TRAIL':>6} {'BE':>6} {'OTHER':>6}")
    print("-" * 90)
    for gc, result, exit_reasons in results:
        if result is None:
            continue
        sl = exit_reasons.get("SL", 0)
        tp = exit_reasons.get("TP", 0)
        trail = exit_reasons.get("TRAIL", 0)
        be = exit_reasons.get("BREAKEVEN", 0)
        other = sum(v for k, v in exit_reasons.items()
                    if k not in {"SL", "TP", "TRAIL", "BREAKEVEN"})
        print(f"{gc.label:<24} {sl:>6} {tp:>6} {trail:>6} {be:>6} {other:>6}")

    print("=" * 110)

    # JSON output
    print("\n\nJSON_RESULTS_START")
    json_out = []
    for gc, result, exit_reasons in results:
        if result is None:
            continue
        d = result.to_dict()
        d["config"] = gc.label
        d["exit_reasons"] = exit_reasons
        json_out.append(d)
    print(json.dumps(json_out, indent=2))
    print("JSON_RESULTS_END")


if __name__ == "__main__":
    main()
