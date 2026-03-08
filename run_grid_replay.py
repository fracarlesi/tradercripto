#!/usr/bin/env python3
"""Grid replay v2: realistic execution friction + max hold time.

Fetches data ONCE, tests all parameter combinations.
Outputs comparison tables sorted by PF, Net P&L, MaxDD.
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
    tp_pct: float       # e.g. 3.5 means 3.5%
    sl_pct: float       # e.g. 1.0 means 1.0%
    breakeven: float    # 99 = disabled
    trailing: float     # 0 = disabled
    max_hold_hours: float = 4.0     # 0 = disabled
    maker_fill_rate: float = 1.0    # 1.0 = perfect fills
    maker_fail_action: str = "taker"  # "taker" or "skip"


# ── Grid dimensions ──────────────────────────────────────────────────────
THRESHOLDS = [0.54, 0.56, 0.58, 0.60, 0.62]
TP_VALUES = [2.0, 2.5, 3.0, 3.5, 4.0]
SL_VALUES = [0.8, 1.0, 1.2]
MAX_HOLD_VALUES = [2.0, 4.0, 6.0]
EXEC_MODES = [
    ("ideal", 1.0, "taker"),            # Original: perfect maker fills
    ("maker_70_taker", 0.70, "taker"),   # 70% maker, 30% taker fallback
    ("maker_70_skip", 0.70, "skip"),     # 70% maker, 30% skipped
]

DAYS = 90
TF = "15m"


def build_configs() -> list[GridConfig]:
    """Build full grid of configs."""
    configs: list[GridConfig] = []
    for exec_name, fill_rate, fail_action in EXEC_MODES:
        for t in THRESHOLDS:
            for tp in TP_VALUES:
                for sl in SL_VALUES:
                    for mh in MAX_HOLD_VALUES:
                        label = (f"T={t:.2f} TP={tp} SL={sl} "
                                 f"MH={mh:.0f}h {exec_name}")
                        configs.append(GridConfig(
                            label=label,
                            threshold=t,
                            tp_pct=tp,
                            sl_pct=sl,
                            breakeven=99,
                            trailing=0,
                            max_hold_hours=mh,
                            maker_fill_rate=fill_rate,
                            maker_fail_action=fail_action,
                        ))
    return configs


def run_single_sim(
    gc: GridConfig,
    base_cfg: BacktestConfig,
    scored: list[tuple[int, str, int, float, str, float]],
    timeline: list[int],
    asset_candles: dict[str, list[dict]],
    asset_time_idx: dict[str, dict[int, int]],
    asset_indicators: dict[str, dict],
    leverage_caps: dict[str, int],
) -> tuple[BacktestResult | None, dict[str, int], dict[str, int]]:
    """Run one simulation config. Returns (result, exit_reasons, exec_stats)."""
    cfg = copy.deepcopy(base_cfg)
    cfg.tp_pct = gc.tp_pct / 100.0
    cfg.sl_pct = gc.sl_pct / 100.0
    cfg.breakeven_threshold_pct = gc.breakeven / 100.0
    cfg.trailing_atr_mult = gc.trailing
    cfg.max_hold_hours = gc.max_hold_hours
    cfg.maker_fill_rate = gc.maker_fill_rate
    cfg.maker_fail_action = gc.maker_fail_action

    # Apply ATR gate and threshold filter per config
    sl_pct_100 = gc.sl_pct
    deduped: dict[int, dict[str, tuple[int, float, str]]] = {}
    for ts, asset, direction, proba, label, atr_pct in scored:
        if atr_pct > sl_pct_100:
            continue
        if proba < gc.threshold:
            continue
        if ts not in deduped:
            deduped[ts] = {}
        existing = deduped[ts].get(asset)
        if existing is None or proba > existing[1]:
            deduped[ts][asset] = (direction, proba, label)

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
    exec_stats = {
        "maker_fills": sim.maker_fills,
        "taker_fallbacks": sim.taker_fallbacks,
        "skipped_entries": sim.skipped_entries,
    }
    return result, sim.exit_reasons, exec_stats


def main() -> None:
    configs = build_configs()
    base_cfg = load_config(timeframe=TF, lookback_days=DAYS)
    warmup = 200
    base_cfg.warmup_bars = warmup

    print("=" * 110)
    print(f"GRID REPLAY v2 (realistic): {len(configs)} configs x {DAYS}d ({TF})")
    print("=" * 110)
    print(f"Dimensions: {len(THRESHOLDS)} thresholds x {len(TP_VALUES)} TP x "
          f"{len(SL_VALUES)} SL x {len(MAX_HOLD_VALUES)} max_hold x "
          f"{len(EXEC_MODES)} exec_modes")

    # ── 1. Fetch data ONCE ───────────────────────────────────────────────
    print("\nStep 1: Fetching data...")
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (DAYS + 3) * 86_400_000
    signal_cutoff_ms = now_ms - DAYS * 86_400_000

    assets, leverage_caps = get_all_assets_with_info()
    asset_candles, _, _ = fetch_all_candles(
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

    # ── 4. Score ALL signals ONCE ─────────────────────────────────────────
    print("Step 4: Scoring all signals...")
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

            if has_breakout and _is_chaos_or_trend(ind, bar_idx):
                sig_vb = signal_volume_breakout_entry(ind, bar_idx)
                if sig_vb != 0:
                    feats = _extract_features(candles, ind, bar_idx, bb_upper, bb_lower, 1.0)
                    proba = _predict(model, feats, feature_names, lgb_model) if feats else 0.0
                    scored.append((ts, asset, sig_vb, proba, "vb", atr_pct_f))

            if _is_chaos_or_trend(ind, bar_idx):
                sig_mb = signal_momentum_burst_entry(ind, bar_idx)
                if sig_mb != 0:
                    feats = _extract_features(candles, ind, bar_idx, bb_upper, bb_lower, 2.0)
                    proba = _predict(model, feats, feature_names, lgb_model) if feats else 0.0
                    scored.append((ts, asset, sig_mb, proba, "mb", atr_pct_f))

    print(f"  Total signals scored: {len(scored)}")
    scored.sort(key=lambda x: x[0])

    # ── 5. Run all simulations ────────────────────────────────────────────
    print(f"\nStep 5: Simulating {len(configs)} configs...")
    all_results: list[tuple[GridConfig, BacktestResult | None, dict, dict]] = []

    t0 = time.time()
    for i, gc in enumerate(configs):
        if (i + 1) % 50 == 0 or i == 0:
            elapsed = time.time() - t0
            print(f"  [{i+1}/{len(configs)}] {elapsed:.1f}s elapsed...")

        result, exit_reasons, exec_stats = run_single_sim(
            gc, base_cfg, scored, timeline,
            asset_candles, asset_time_idx, asset_indicators, leverage_caps,
        )
        all_results.append((gc, result, exit_reasons, exec_stats))

    elapsed = time.time() - t0
    print(f"\n  Done: {len(configs)} configs in {elapsed:.1f}s "
          f"({elapsed/len(configs):.2f}s/config)")

    # Filter out failures
    valid = [(gc, r, er, es) for gc, r, er, es in all_results if r is not None and r.count > 0]
    if not valid:
        print("No valid results.")
        return

    # ── 6. Print results sorted by Profit Factor ──────────────────────────
    print("\n\n")
    print("=" * 130)
    print(f"TOP 30 BY PROFIT FACTOR — {DAYS}d realistic replay")
    print("=" * 130)
    header = (f"{'Config':<40} {'Trades':>6} {'Wins':>5} {'WR%':>5} "
              f"{'Net P&L':>9} {'Fees':>7} {'MaxDD':>7} {'PF':>6} "
              f"{'Sharpe':>7} {'MkrF':>5} {'TkrF':>5} {'Skip':>5} {'MH':>4}")
    print(header)
    print("-" * 130)

    by_pf = sorted(valid, key=lambda x: x[1].profit_factor, reverse=True)
    for gc, result, exit_reasons, exec_stats in by_pf[:30]:
        mh_count = exit_reasons.get("MAX_HOLD", 0)
        print(f"{gc.label:<40} "
              f"{result.count:>6} "
              f"{result.wins:>5} "
              f"{result.win_rate:>4.0f}% "
              f"${result.net_pnl:>+8.2f} "
              f"${result.total_fees:>6.2f} "
              f"${result.max_drawdown:>6.2f} "
              f"{result.profit_factor:>6.2f} "
              f"{result.sharpe:>7.2f} "
              f"{exec_stats['maker_fills']:>5} "
              f"{exec_stats['taker_fallbacks']:>5} "
              f"{exec_stats['skipped_entries']:>5} "
              f"{mh_count:>4}")

    # ── 7. Print results sorted by Net P&L ────────────────────────────────
    print("\n\n")
    print("=" * 130)
    print(f"TOP 30 BY NET P&L — {DAYS}d realistic replay")
    print("=" * 130)
    print(header)
    print("-" * 130)

    by_pnl = sorted(valid, key=lambda x: x[1].net_pnl, reverse=True)
    for gc, result, exit_reasons, exec_stats in by_pnl[:30]:
        mh_count = exit_reasons.get("MAX_HOLD", 0)
        print(f"{gc.label:<40} "
              f"{result.count:>6} "
              f"{result.wins:>5} "
              f"{result.win_rate:>4.0f}% "
              f"${result.net_pnl:>+8.2f} "
              f"${result.total_fees:>6.2f} "
              f"${result.max_drawdown:>6.2f} "
              f"{result.profit_factor:>6.2f} "
              f"{result.sharpe:>7.2f} "
              f"{exec_stats['maker_fills']:>5} "
              f"{exec_stats['taker_fallbacks']:>5} "
              f"{exec_stats['skipped_entries']:>5} "
              f"{mh_count:>4}")

    # ── 8. Exit reasons for top 10 by PF ──────────────────────────────────
    print("\n\n")
    print("=" * 130)
    print("EXIT REASONS — Top 10 by PF")
    print("-" * 130)
    print(f"{'Config':<40} {'SL':>5} {'TP':>5} {'TRAIL':>5} {'BE':>5} "
          f"{'MAX_HOLD':>8} {'OTHER':>5} {'SKIP':>5}")
    print("-" * 130)
    for gc, result, exit_reasons, exec_stats in by_pf[:10]:
        sl = exit_reasons.get("SL", 0)
        tp = exit_reasons.get("TP", 0)
        trail = exit_reasons.get("TRAIL", 0)
        be = exit_reasons.get("BREAKEVEN", 0)
        mh = exit_reasons.get("MAX_HOLD", 0)
        other = sum(v for k, v in exit_reasons.items()
                    if k not in {"SL", "TP", "TRAIL", "BREAKEVEN", "MAX_HOLD"})
        skip = exec_stats["skipped_entries"]
        print(f"{gc.label:<40} {sl:>5} {tp:>5} {trail:>5} {be:>5} "
              f"{mh:>8} {other:>5} {skip:>5}")

    # ── 9. Comparison: ideal vs realistic ─────────────────────────────────
    print("\n\n")
    print("=" * 130)
    print("COMPARISON: Ideal vs Realistic Execution (T=0.58 TP=4.0 SL=1.0)")
    print("-" * 130)
    compare_label_prefix = "T=0.58 TP=4.0 SL=1.0 MH=4h"
    for gc, result, exit_reasons, exec_stats in all_results:
        if result is None:
            continue
        if gc.threshold == 0.58 and gc.tp_pct == 4.0 and gc.sl_pct == 1.0 and gc.max_hold_hours == 4.0:
            mh = exit_reasons.get("MAX_HOLD", 0)
            print(f"  {gc.label:<40} "
                  f"Trades={result.count:>4} "
                  f"P&L=${result.net_pnl:>+8.2f} "
                  f"PF={result.profit_factor:.2f} "
                  f"MaxDD=${result.max_drawdown:.2f} "
                  f"MkrF={exec_stats['maker_fills']} "
                  f"TkrF={exec_stats['taker_fallbacks']} "
                  f"Skip={exec_stats['skipped_entries']} "
                  f"MaxHold={mh}")

    # ── 10. JSON output ───────────────────────────────────────────────────
    print("\n\nJSON_RESULTS_START")
    json_out = []
    for gc, result, exit_reasons, exec_stats in all_results:
        if result is None:
            continue
        d = result.to_dict()
        d["config"] = gc.label
        d["threshold"] = gc.threshold
        d["tp_pct"] = gc.tp_pct
        d["sl_pct"] = gc.sl_pct
        d["max_hold_hours"] = gc.max_hold_hours
        d["maker_fill_rate"] = gc.maker_fill_rate
        d["maker_fail_action"] = gc.maker_fail_action
        d["exit_reasons"] = exit_reasons
        d["exec_stats"] = exec_stats
        json_out.append(d)
    print(json.dumps(json_out, indent=2))
    print("JSON_RESULTS_END")

    print(f"\n{'='*130}")
    print("DONE")
    print(f"{'='*130}")


if __name__ == "__main__":
    main()
