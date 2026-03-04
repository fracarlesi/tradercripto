#!/usr/bin/env python3
"""Compare two trained ML models (old vs new) on the same backtest data.

Loads both models, fetches candle data ONCE, scores all signals with each
model, and runs PortfolioSimulator for each.  Prints a side-by-side table.

Usage:
    python3 -m crypto_bot.scripts.compare_models \
        --old models/trade_model.joblib \
        --new models/trade_model_v2.joblib \
        --days 14 --threshold 0.52
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backtesting.api import fetch_all_candles, get_all_assets_with_info
from backtesting.config import load_config
from backtesting.indicators import calc_bollinger, compute_indicators
from backtesting.modes.threshold import (
    _extract_features,
    _is_chaos_or_trend,
    _predict,
)
from backtesting.signals import (
    signal_ema_crossover_entry,
    signal_momentum_burst_entry,
    signal_volume_breakout_entry,
)
from backtesting.simulator import PortfolioSimulator
from backtesting.stats import BacktestResult


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

def _load_model(path: str) -> dict:
    """Load a joblib model payload and return a normalised dict.

    Keys returned:
      - model: XGBClassifier (required)
      - lgb_model: LGBMClassifier | None
      - feature_names: list[str] | None
      - optimal_threshold: float
      - n_features: int
      - has_breakout: bool
    """
    import joblib

    p = Path(path)
    if not p.exists():
        print(f"ERROR: model not found at {p}")
        sys.exit(1)

    payload = joblib.load(p)
    model = payload["model"]
    lgb_model = payload.get("lgb_model")

    booster = model.get_booster()
    feature_names: list[str] | None = (
        list(booster.feature_names) if booster.feature_names else None
    )

    n_features = getattr(model, "n_features_in_", 13)
    has_breakout = n_features >= 13

    return {
        "model": model,
        "lgb_model": lgb_model,
        "feature_names": feature_names,
        "optimal_threshold": payload.get("optimal_threshold", 0.55),
        "n_features": n_features,
        "has_breakout": has_breakout,
        "feature_importances": payload.get("feature_importances", {}),
        "path": str(p),
    }


def _predict_ensemble(
    payload: dict,
    features: dict,
) -> float:
    """Score a signal using XGB (+ optional LGB ensemble), with backward compat."""
    model = payload["model"]
    feature_names = payload["feature_names"]

    # XGBoost prediction (handles fewer-feature backward compat via _predict)
    xgb_proba = _predict(model, features, feature_names)

    # LightGBM ensemble (if present)
    lgb_model = payload.get("lgb_model")
    if lgb_model is not None:
        row = pd.DataFrame([features])
        if feature_names:
            cols = [f for f in feature_names if f in row.columns]
            row = row[cols].astype(float)
        else:
            row = row.astype(float)
        n_lgb = getattr(lgb_model, "n_features_in_", row.shape[1])
        if row.shape[1] > n_lgb:
            row = row.iloc[:, :n_lgb]
        lgb_proba = float(lgb_model.predict_proba(row)[:, 1][0])
        return (xgb_proba + lgb_proba) / 2.0

    return xgb_proba


# ---------------------------------------------------------------------------
# Signal scoring
# ---------------------------------------------------------------------------

def _score_all_signals(
    payload: dict,
    asset_candles: dict[str, list[dict]],
    asset_indicators: dict[str, dict],
    asset_indicators_1h: dict[str, dict],
    asset_bb: dict[str, tuple[np.ndarray, np.ndarray]],
    asset_time_idx: dict[str, dict[int, int]],
    timeline: list[int],
    warmup: int,
    btc_ind: dict | None,
    btc_time_idx_map: dict[int, int] | None,
) -> list[tuple[int, str, int, float, str]]:
    """Score every signal across all assets with a single model payload.

    Returns list of (timestamp, asset, direction, probability, signal_label).
    """
    has_breakout = payload["has_breakout"]
    scored: list[tuple[int, str, int, float, str]] = []

    for asset in asset_candles:
        candles = asset_candles[asset]
        ind = asset_indicators[asset]
        ind_1h = asset_indicators_1h[asset]
        bb_upper, bb_lower = asset_bb[asset]

        a_btc_ind = btc_ind if asset != "BTC" else None
        a_btc_time_idx = btc_time_idx_map if asset != "BTC" else None

        for ts in timeline:
            if ts not in asset_time_idx.get(asset, {}):
                continue
            bidx = asset_time_idx[asset][ts]
            if bidx < warmup:
                continue

            # PATH 1: EMA crossover (TREND only)
            if ind["is_trend"][bidx]:
                sig = signal_ema_crossover_entry(ind, bidx)
                if sig != 0:
                    feats = _extract_features(
                        candles, ind, bidx, bb_upper, bb_lower,
                        signal_type=0.0, direction=sig,
                        btc_ind=a_btc_ind, btc_time_idx=a_btc_time_idx,
                        ind_1h=ind_1h,
                    )
                    if feats is not None:
                        p = _predict_ensemble(payload, feats)
                        scored.append((ts, asset, sig, p, "ema"))

            # PATH 2: Volume breakout (CHAOS + TREND)
            if has_breakout and _is_chaos_or_trend(ind, bidx):
                sig = signal_volume_breakout_entry(ind, bidx)
                if sig != 0:
                    feats = _extract_features(
                        candles, ind, bidx, bb_upper, bb_lower,
                        signal_type=1.0, direction=sig,
                        btc_ind=a_btc_ind, btc_time_idx=a_btc_time_idx,
                        ind_1h=ind_1h,
                    )
                    if feats is not None:
                        p = _predict_ensemble(payload, feats)
                        scored.append((ts, asset, sig, p, "vb"))

            # PATH 3: Momentum burst (CHAOS + TREND)
            if _is_chaos_or_trend(ind, bidx):
                sig = signal_momentum_burst_entry(ind, bidx)
                if sig != 0:
                    feats = _extract_features(
                        candles, ind, bidx, bb_upper, bb_lower,
                        signal_type=2.0, direction=sig,
                        btc_ind=a_btc_ind, btc_time_idx=a_btc_time_idx,
                        ind_1h=ind_1h,
                    )
                    if feats is not None:
                        p = _predict_ensemble(payload, feats)
                        scored.append((ts, asset, sig, p, "mb"))

    return scored


# ---------------------------------------------------------------------------
# Dedup + simulate
# ---------------------------------------------------------------------------

def _dedup_signals(
    signals: list[tuple[int, str, int, float, str]],
) -> dict[int, dict[str, tuple[int, float, str]]]:
    """Per-symbol dedup: at same timestamp, keep highest probability."""
    deduped: dict[int, dict[str, tuple[int, float, str]]] = {}
    for ts, asset, direction, proba, label in signals:
        if ts not in deduped:
            deduped[ts] = {}
        existing = deduped[ts].get(asset)
        if existing is None or proba > existing[1]:
            deduped[ts][asset] = (direction, proba, label)
    return deduped


def _simulate(
    deduped: dict[int, dict[str, tuple[int, float, str]]],
    timeline: list[int],
    threshold: float,
    cfg: object,
    leverage_caps: dict[str, int],
    asset_candles: dict[str, list[dict]],
    asset_time_idx: dict[str, dict[int, int]],
    label: str,
) -> BacktestResult:
    """Run PortfolioSimulator on deduped signals at given threshold."""
    sim = PortfolioSimulator(cfg, label=label, leverage_caps=leverage_caps)  # type: ignore[arg-type]

    for ts in timeline:
        # Check exits first
        for sym in list(sim.open_positions.keys()):
            if ts in asset_time_idx.get(sym, {}):
                sim.check_exits(sym, asset_candles[sym][asset_time_idx[sym][ts]])

        # Try opens
        if ts in deduped:
            for asset, (direction, proba, _sig_label) in deduped[ts].items():
                if proba >= threshold:
                    bar_idx = asset_time_idx[asset][ts]
                    entry_price = asset_candles[asset][bar_idx]["c"]
                    sim.try_open(asset, direction, entry_price, ts)

    sim.force_close_all(asset_candles)
    return BacktestResult.from_simulator(sim, label)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare two ML models on the same backtest data",
    )
    parser.add_argument("--old", required=True, help="Path to old model .joblib")
    parser.add_argument("--new", required=True, help="Path to new model .joblib")
    parser.add_argument("--days", type=int, default=14, help="Lookback days (default 14)")
    parser.add_argument("--threshold", type=float, default=0.52, help="ML threshold (default 0.52)")
    parser.add_argument("--account", type=float, default=64.0, help="Account size in $ (default 64)")
    parser.add_argument("--timeframe", type=str, default="15m", help="Candle timeframe (default 15m)")
    args = parser.parse_args()

    # ── Load both models ──────────────────────────────────────────────
    print("Loading models...")
    old_payload = _load_model(args.old)
    new_payload = _load_model(args.new)

    print(f"  OLD: {args.old} — {old_payload['n_features']} features, "
          f"calibrated={old_payload['optimal_threshold']:.4f}, "
          f"ensemble={'xgb+lgb' if old_payload['lgb_model'] else 'xgb'}")
    print(f"  NEW: {args.new} — {new_payload['n_features']} features, "
          f"calibrated={new_payload['optimal_threshold']:.4f}, "
          f"ensemble={'xgb+lgb' if new_payload['lgb_model'] else 'xgb'}")
    print()

    # ── Load config ───────────────────────────────────────────────────
    tf = args.timeframe
    cfg = load_config(timeframe=tf, lookback_days=args.days, account_size=args.account)
    warmup = {"5m": 650, "15m": 200, "1h": 200}.get(tf, 200)
    cfg.warmup_bars = warmup
    tf_scale = {"5m": 3, "15m": 1, "1h": 1}.get(tf, 1)

    # ── Fetch data ONCE ───────────────────────────────────────────────
    print(f"Fetching candle data for {args.days}d ({tf})...")
    now_ms = int(time.time() * 1000)
    extra_days = 4 if tf == "5m" else 3
    start_ms = now_ms - (args.days + extra_days) * 86_400_000
    signal_cutoff_ms = now_ms - args.days * 86_400_000

    assets, leverage_caps = get_all_assets_with_info()
    asset_candles, _, _ = fetch_all_candles(
        assets, tf, start_ms, now_ms, cfg.exclude_symbols, warmup,
    )

    if not asset_candles:
        print("No assets with enough data.")
        return

    # ── Compute indicators ────────────────────────────────────────────
    print("Computing indicators...")
    asset_indicators: dict[str, dict] = {}
    asset_indicators_1h: dict[str, dict] = {}
    asset_bb: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for asset, candles in asset_candles.items():
        ind = compute_indicators(candles, cfg, tf_scale)
        ind_1h = compute_indicators(candles, cfg, tf_scale * 4)
        _, bb_upper, bb_lower = calc_bollinger(ind["closes"], 20 * tf_scale)
        asset_indicators[asset] = ind
        asset_indicators_1h[asset] = ind_1h
        asset_bb[asset] = (bb_upper, bb_lower)

    # ── Build timeline ────────────────────────────────────────────────
    all_timestamps: set[int] = set()
    for candles in asset_candles.values():
        for c in candles:
            if c["t"] >= signal_cutoff_ms:
                all_timestamps.add(c["t"])
    timeline = sorted(all_timestamps)

    asset_time_idx: dict[str, dict[int, int]] = {}
    for asset, candles in asset_candles.items():
        asset_time_idx[asset] = {c["t"]: i for i, c in enumerate(candles)}

    btc_ind: dict | None = asset_indicators.get("BTC")
    btc_time_idx_map: dict[int, int] | None = asset_time_idx.get("BTC")

    # ── Score signals with BOTH models ────────────────────────────────
    print("Scoring signals with OLD model...")
    old_signals = _score_all_signals(
        old_payload, asset_candles, asset_indicators, asset_indicators_1h,
        asset_bb, asset_time_idx, timeline, warmup, btc_ind, btc_time_idx_map,
    )
    print(f"  {len(old_signals)} raw signals scored")

    print("Scoring signals with NEW model...")
    new_signals = _score_all_signals(
        new_payload, asset_candles, asset_indicators, asset_indicators_1h,
        asset_bb, asset_time_idx, timeline, warmup, btc_ind, btc_time_idx_map,
    )
    print(f"  {len(new_signals)} raw signals scored")
    print()

    # ── Dedup ─────────────────────────────────────────────────────────
    old_deduped = _dedup_signals(old_signals)
    new_deduped = _dedup_signals(new_signals)

    # ── Simulate ──────────────────────────────────────────────────────
    threshold = args.threshold
    old_name = Path(args.old).stem
    new_name = Path(args.new).stem

    print(f"Simulating at threshold={threshold:.2f}...")
    old_result = _simulate(
        old_deduped, timeline, threshold, cfg, leverage_caps,
        asset_candles, asset_time_idx, label=f"OLD ({old_name})",
    )
    new_result = _simulate(
        new_deduped, timeline, threshold, cfg, leverage_caps,
        asset_candles, asset_time_idx, label=f"NEW ({new_name})",
    )

    # ── Signal breakdown ──────────────────────────────────────────────
    def _count_by_type(
        signals: list[tuple[int, str, int, float, str]],
        thr: float,
    ) -> tuple[int, int, int]:
        ema = vb = mb = 0
        for _, _, _, proba, label in signals:
            if proba >= thr:
                if label == "ema":
                    ema += 1
                elif label == "vb":
                    vb += 1
                else:
                    mb += 1
        return ema, vb, mb

    old_ema, old_vb, old_mb = _count_by_type(old_signals, threshold)
    new_ema, new_vb, new_mb = _count_by_type(new_signals, threshold)

    # ── Print comparison table ────────────────────────────────────────
    print()
    print("=" * 120)
    print(f"MODEL COMPARISON — Last {args.days}d — threshold={threshold:.2f} — "
          f"${args.account} account — {tf}")
    print("=" * 120)
    print(f"Config: TP {cfg.tp_pct*100:.1f}% | SL {cfg.sl_pct*100:.1f}% | "
          f"{cfg.leverage}x leverage | {cfg.position_pct*100:.0f}% size | "
          f"max {cfg.max_positions} positions")
    print()

    header = (f"{'Model':<30} {'Trades':>6} {'EMA':>5} {'VB':>5} {'MB':>5} "
              f"{'Win%':>6} {'Net P&L':>10} {'MaxDD':>8} {'Fees':>8} "
              f"{'PF':>6} {'Sharpe':>7} {'Assets':>6}")
    print(header)
    print("-" * 120)

    for r, (ema_c, vb_c, mb_c) in [
        (old_result, (old_ema, old_vb, old_mb)),
        (new_result, (new_ema, new_vb, new_mb)),
    ]:
        print(f"{r.label:<30} {r.count:>6} {ema_c:>5} {vb_c:>5} {mb_c:>5} "
              f"{r.win_rate:>5.1f}% ${r.net_pnl:>+9.2f} ${r.max_drawdown:>7.2f} "
              f"${r.total_fees:>7.2f} {r.profit_factor:>6.2f} {r.sharpe:>7.2f} "
              f"{r.unique_assets:>5}")

    print("=" * 120)

    # ── Delta row ─────────────────────────────────────────────────────
    d_trades = new_result.count - old_result.count
    d_winrate = new_result.win_rate - old_result.win_rate
    d_pnl = new_result.net_pnl - old_result.net_pnl
    d_maxdd = new_result.max_drawdown - old_result.max_drawdown
    d_fees = new_result.total_fees - old_result.total_fees
    d_pf = new_result.profit_factor - old_result.profit_factor
    d_sharpe = new_result.sharpe - old_result.sharpe
    d_assets = new_result.unique_assets - old_result.unique_assets

    print(f"{'DELTA (NEW - OLD)':<30} {d_trades:>+6} "
          f"{'':>5} {'':>5} {'':>5} "
          f"{d_winrate:>+5.1f}% ${d_pnl:>+9.2f} ${d_maxdd:>+7.2f} "
          f"${d_fees:>+7.2f} {d_pf:>+6.2f} {d_sharpe:>+7.2f} "
          f"{d_assets:>+5}")
    print()

    # ── Verdict ───────────────────────────────────────────────────────
    improvements = 0
    regressions = 0

    if d_pnl > 0:
        improvements += 1
    elif d_pnl < 0:
        regressions += 1

    if d_sharpe > 0:
        improvements += 1
    elif d_sharpe < 0:
        regressions += 1

    if d_pf > 0:
        improvements += 1
    elif d_pf < 0:
        regressions += 1

    # Lower MaxDD is better
    if d_maxdd < 0:
        improvements += 1
    elif d_maxdd > 0:
        regressions += 1

    if improvements > regressions:
        verdict = "NEW model is BETTER"
    elif regressions > improvements:
        verdict = "OLD model is BETTER"
    else:
        verdict = "Models are roughly EQUIVALENT"

    print(f"Verdict: {verdict} "
          f"({improvements} improvements, {regressions} regressions out of 4 metrics)")

    # ── Feature importance diff (top 10) ──────────────────────────────
    old_imp = old_payload.get("feature_importances", {})
    new_imp = new_payload.get("feature_importances", {})
    if old_imp and new_imp:
        all_features = sorted(
            set(old_imp.keys()) | set(new_imp.keys()),
            key=lambda f: new_imp.get(f, 0),
            reverse=True,
        )
        print()
        print("Feature Importance (top 10):")
        print(f"  {'Feature':<25} {'OLD':>8} {'NEW':>8} {'Delta':>8}")
        print(f"  {'-'*25} {'-'*8} {'-'*8} {'-'*8}")
        for feat in all_features[:10]:
            o = old_imp.get(feat, 0.0)
            n = new_imp.get(feat, 0.0)
            d = n - o
            print(f"  {feat:<25} {o:>8.4f} {n:>8.4f} {d:>+8.4f}")


if __name__ == "__main__":
    main()
