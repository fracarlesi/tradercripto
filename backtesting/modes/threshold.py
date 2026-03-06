"""Threshold mode: compare P&L across ML threshold levels.

Loads the trained ML model, runs both signal paths (EMA crossover + volume
breakout) on all assets, scores each signal with the model, and simulates
trades at each threshold from 0.50 to 0.70.  Outputs a comparison table
with real P&L, win rate, max drawdown, profit factor and Sharpe.
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from backtesting.api import fetch_all_candles, get_all_assets_with_info
from backtesting.config import load_config
from backtesting.indicators import calc_bollinger, compute_indicators
from backtesting.signals import (
    signal_ema_crossover_entry,
    signal_momentum_burst_entry,
    signal_volume_breakout_entry,
)
from backtesting.simulator import PortfolioSimulator
from backtesting.stats import (
    BacktestResult,
    print_results_json,
    print_top_bottom_trades,
)


# ---------------------------------------------------------------------------
# Feature extraction (mirrors ml_dataset._extract_features)
# ---------------------------------------------------------------------------

def _extract_features(
    candles: list[dict],
    ind: dict,
    idx: int,
    bb_upper: np.ndarray,
    bb_lower: np.ndarray,
    signal_type: float,
    direction: int = 1,
    btc_ind: dict | None = None,
    btc_time_idx: dict[int, int] | None = None,
    ind_1h: dict | None = None,
) -> dict | None:
    """Extract ML feature dict from backtesting arrays.

    Returns None if any core indicator is NaN (can't score).
    """
    close = candles[idx]["c"]
    open_price = candles[idx]["o"]
    ema9 = ind["ema9"][idx]
    ema21 = ind["ema21"][idx]
    ema200 = ind["ema200"][idx]
    adx_val = ind["adx"][idx]
    rsi_val = ind["rsi"][idx]
    atr_pct_val = ind["atr_pct"][idx]

    if any(np.isnan(v) for v in [ema9, ema21, ema200, adx_val, rsi_val]):
        return None

    # Signed EMA spread
    signed_ema_spread = (ema9 - ema21) / ema21 * 100 if ema21 != 0 else 0.0

    # Volume ratio
    vol_sma = ind["vol_sma20"][idx]
    if not np.isnan(vol_sma) and vol_sma > 0:
        volume_ratio = ind["volumes"][idx] / vol_sma
    else:
        volume_ratio = 1.0

    # Bollinger position
    bu, bl = bb_upper[idx], bb_lower[idx]
    if not np.isnan(bu) and not np.isnan(bl) and (bu - bl) > 0:
        bb_position = (close - bl) / (bu - bl)
    else:
        bb_position = 0.5

    # EMA slopes (4-bar lookback)
    if idx >= 4 and not np.isnan(ind["ema9"][idx - 4]) and ind["ema9"][idx - 4] > 0:
        ema9_slope = (ema9 - ind["ema9"][idx - 4]) / ind["ema9"][idx - 4]
    else:
        ema9_slope = 0.0

    if idx >= 4 and not np.isnan(ind["ema21"][idx - 4]) and ind["ema21"][idx - 4] > 0:
        ema21_slope = (ema21 - ind["ema21"][idx - 4]) / ind["ema21"][idx - 4]
    else:
        ema21_slope = 0.0

    # Close vs EMA200
    close_vs_ema200 = (close - ema200) / ema200 * 100 if ema200 > 0 else 0.0

    # Regime encoding
    is_trend = ind["is_trend"][idx]
    if is_trend:
        regime_encoded = 2.0
    elif not np.isnan(adx_val) and adx_val <= 20:
        regime_encoded = 0.0
    else:
        regime_encoded = 1.0

    # Session bin + is_weekend
    ts_ms = candles[idx]["t"]
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    h = dt.hour
    session = 0 if h < 8 else (1 if h < 13 else (2 if h < 21 else 3))
    is_weekend = 1 if dt.weekday() >= 5 else 0

    # Candle body pct
    candle_body_pct = abs(close - open_price) / open_price * 100 if open_price > 0 else 0.0

    # RSI slope (2-bar lookback)
    rsi_slope = float(rsi_val - ind["rsi"][idx - 2]) if idx >= 2 and not np.isnan(ind["rsi"][idx - 2]) else 0.0

    # ATR percentile: rank of current ATR in last 100 bars [0,1]
    atr_vals = ind["atr_pct"]
    lookback = min(100, idx + 1)
    if lookback > 1 and not np.isnan(atr_pct_val):
        window = atr_vals[idx - lookback + 1: idx + 1]
        valid = window[~np.isnan(window)]
        if len(valid) > 1:
            atr_percentile = float(np.searchsorted(np.sort(valid), atr_pct_val)) / len(valid)
        else:
            atr_percentile = 0.5
    else:
        atr_percentile = 0.5

    # --- BTC context features ---
    btc_trend = 0.0
    btc_rsi = 50.0
    btc_ema9_slope = 0.0
    if btc_ind is not None and btc_time_idx is not None:
        btc_idx = btc_time_idx.get(ts_ms)
        if btc_idx is not None:
            b_ema9 = btc_ind["ema9"][btc_idx]
            b_ema21 = btc_ind["ema21"][btc_idx]
            if not np.isnan(b_ema9) and not np.isnan(b_ema21):
                btc_trend = 1.0 if b_ema9 > b_ema21 else (-1.0 if b_ema9 < b_ema21 else 0.0)
            if not np.isnan(btc_ind["rsi"][btc_idx]):
                btc_rsi = float(btc_ind["rsi"][btc_idx])
            if btc_idx >= 4 and not np.isnan(btc_ind["ema9"][btc_idx - 4]) and btc_ind["ema9"][btc_idx - 4] > 0:
                btc_ema9_slope = (btc_ind["ema9"][btc_idx] - btc_ind["ema9"][btc_idx - 4]) / btc_ind["ema9"][btc_idx - 4]

    # --- Multi-TF alignment features ---
    rsi_1h = float(rsi_val)
    adx_1h = float(adx_val)
    tf_alignment = 0.0
    if ind_1h is not None:
        if not np.isnan(ind_1h["rsi"][idx]):
            rsi_1h = float(ind_1h["rsi"][idx])
        if not np.isnan(ind_1h["adx"][idx]):
            adx_1h = float(ind_1h["adx"][idx])
        if (not np.isnan(ind_1h["ema9"][idx]) and not np.isnan(ind_1h["ema21"][idx])):
            dir_15m = 1.0 if ema9 > ema21 else -1.0
            dir_1h = 1.0 if ind_1h["ema9"][idx] > ind_1h["ema21"][idx] else -1.0
            tf_alignment = 1.0 if dir_15m == dir_1h else -1.0

    return {
        "adx": float(adx_val),
        "rsi": float(rsi_val),
        "atr_pct": float(atr_pct_val),
        "volume_ratio": volume_ratio,
        "bb_position": bb_position,
        "ema9_slope": ema9_slope,
        "ema21_slope": ema21_slope,
        "close_vs_ema200": close_vs_ema200,
        "regime_encoded": regime_encoded,
        "session": session,
        "signal_type": signal_type,
        "candle_body_pct": candle_body_pct,
        "rsi_slope": rsi_slope,
        # Tier 1
        "is_weekend": is_weekend,
        "atr_percentile": atr_percentile,
        "signed_ema_spread": signed_ema_spread,
        "direction": float(direction),
        # Tier 2
        "btc_trend": btc_trend,
        "btc_rsi": btc_rsi,
        "btc_ema9_slope": btc_ema9_slope,
        "tf_alignment": tf_alignment,
        "rsi_1h": rsi_1h,
        "adx_1h": adx_1h,
    }


# ---------------------------------------------------------------------------
# Regime helpers
# ---------------------------------------------------------------------------

def _is_chaos_or_trend(ind: dict, idx: int) -> bool:
    """True if bar is in TREND or CHAOS regime (not RANGE)."""
    if ind["is_trend"][idx]:
        return True
    adx_val = ind["adx"][idx]
    if np.isnan(adx_val):
        return False
    return adx_val > 20  # ADX > 20 → CHAOS (not RANGE)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

THRESHOLDS = [0.50, 0.52, 0.55, 0.58, 0.60, 0.62, 0.65, 0.68, 0.70]


def run(args: argparse.Namespace) -> None:
    days = args.days if args.days is not None else 30
    tf = args.timeframe or "15m"
    cfg = load_config(timeframe=tf, lookback_days=days, account_size=args.account)

    warmup = {"5m": 650, "15m": 200, "1h": 200}.get(tf, 200)
    cfg.warmup_bars = warmup
    tf_scale = {"5m": 3, "15m": 1, "1h": 1}.get(tf, 1)

    # Load ML model
    model_path = Path(__file__).resolve().parent.parent.parent / "models" / "trade_model.joblib"
    if not model_path.exists():
        print(f"ERROR: ML model not found at {model_path}")
        return

    import joblib
    payload = joblib.load(model_path)
    model = payload["model"]
    feature_names = list(model.get_booster().feature_names) if model.get_booster().feature_names else None
    optimal_threshold = payload.get("optimal_threshold", 0.55)

    n_model_features = getattr(model, "n_features_in_", 13)
    has_breakout = n_model_features >= 13

    print("=" * 100)
    print(f"BACKTEST THRESHOLD: ML P&L at each threshold — Last {days}d — All Assets ({tf})")
    print("=" * 100)
    print(f"Config: TP {cfg.tp_pct*100:.1f}% | SL {cfg.sl_pct*100:.1f}% | "
          f"{cfg.leverage}x leverage | ${cfg.account_size} | "
          f"{cfg.position_pct*100:.0f}% size")
    print(f"Model: {n_model_features} features | calibrated threshold: {optimal_threshold:.4f} | "
          f"breakout: {'ON' if has_breakout else 'OFF'}")
    print()

    # Fetch data
    now_ms = int(time.time() * 1000)
    extra_days = 4 if tf == "5m" else 3
    start_ms = now_ms - (days + extra_days) * 86_400_000
    signal_cutoff_ms = now_ms - days * 86_400_000

    assets, leverage_caps = get_all_assets_with_info()
    asset_candles, _, _ = fetch_all_candles(
        assets, tf, start_ms, now_ms, cfg.exclude_symbols, warmup)

    if not asset_candles:
        print("No assets with enough data.")
        return

    # Compute indicators + Bollinger for all assets
    print("Computing indicators...")
    asset_indicators: dict[str, dict] = {}
    asset_indicators_1h: dict[str, dict] = {}
    asset_bb: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for asset, candles in asset_candles.items():
        ind = compute_indicators(candles, cfg, tf_scale)
        ind_1h = compute_indicators(candles, cfg, tf_scale * 4)  # 1h equivalent
        _, bb_upper, bb_lower = calc_bollinger(ind["closes"], 20 * tf_scale)
        asset_indicators[asset] = ind
        asset_indicators_1h[asset] = ind_1h
        asset_bb[asset] = (bb_upper, bb_lower)

    # BTC context indicators (populated after asset_time_idx is built)
    btc_ind: dict | None = None
    btc_time_idx_map: dict[int, int] | None = None

    # Build timeline (sorted unique timestamps after warmup period)
    all_timestamps: set[int] = set()
    for candles in asset_candles.values():
        for c in candles:
            if c["t"] >= signal_cutoff_ms:
                all_timestamps.add(c["t"])
    timeline = sorted(all_timestamps)

    asset_time_idx: dict[str, dict[int, int]] = {}
    for asset, candles in asset_candles.items():
        asset_time_idx[asset] = {c["t"]: i for i, c in enumerate(candles)}

    # BTC context indicators
    if "BTC" in asset_indicators:
        btc_ind = asset_indicators["BTC"]
        btc_time_idx_map = asset_time_idx.get("BTC")

    # Pre-score all signals once (avoid re-running ML for each threshold)
    print("Scoring all signals with ML model...")
    # Each entry: (timestamp, asset, direction, probability, signal_label)
    scored_signals: list[tuple[int, str, int, float, str]] = []
    n_crossover = n_breakout = n_momentum_burst = 0

    for asset in asset_candles:
        candles = asset_candles[asset]
        ind = asset_indicators[asset]
        ind_1h = asset_indicators_1h[asset]
        bb_upper, bb_lower = asset_bb[asset]

        # For BTC itself, don't pass BTC context (use defaults)
        a_btc_ind = btc_ind if asset != "BTC" else None
        a_btc_time_idx = btc_time_idx_map if asset != "BTC" else None

        for ts in timeline:
            if ts not in asset_time_idx[asset]:
                continue
            bar_idx = asset_time_idx[asset][ts]
            if bar_idx < warmup:
                continue

            # Fix 3: ATR vs SL gate — skip if single candle range exceeds stop loss
            atr_pct_val = ind["atr_pct"][bar_idx]
            if not np.isnan(atr_pct_val) and atr_pct_val > cfg.sl_pct * 100:
                continue

            # PATH 1: EMA crossover (TREND only)
            if ind["is_trend"][bar_idx]:
                sig = signal_ema_crossover_entry(ind, bar_idx)
                if sig != 0:
                    features = _extract_features(
                        candles, ind, bar_idx, bb_upper, bb_lower,
                        signal_type=0.0, direction=sig,
                        btc_ind=a_btc_ind, btc_time_idx=a_btc_time_idx,
                        ind_1h=ind_1h,
                    )
                    if features is not None:
                        proba = _predict(model, features, feature_names)
                        scored_signals.append((ts, asset, sig, proba, "ema"))
                        n_crossover += 1

            # PATH 2: Volume breakout (CHAOS + TREND)
            if has_breakout and _is_chaos_or_trend(ind, bar_idx):
                sig_vb = signal_volume_breakout_entry(ind, bar_idx)
                if sig_vb != 0:
                    features = _extract_features(
                        candles, ind, bar_idx, bb_upper, bb_lower,
                        signal_type=1.0, direction=sig_vb,
                        btc_ind=a_btc_ind, btc_time_idx=a_btc_time_idx,
                        ind_1h=ind_1h,
                    )
                    if features is not None:
                        proba = _predict(model, features, feature_names)
                        scored_signals.append((ts, asset, sig_vb, proba, "vb"))
                        n_breakout += 1

            # PATH 3: Momentum burst (CHAOS + TREND)
            if _is_chaos_or_trend(ind, bar_idx):
                sig_mb = signal_momentum_burst_entry(ind, bar_idx)
                if sig_mb != 0:
                    features = _extract_features(
                        candles, ind, bar_idx, bb_upper, bb_lower,
                        signal_type=2.0, direction=sig_mb,
                        btc_ind=a_btc_ind, btc_time_idx=a_btc_time_idx,
                        ind_1h=ind_1h,
                    )
                    if features is not None:
                        proba = _predict(model, features, feature_names)
                        scored_signals.append((ts, asset, sig_mb, proba, "mb"))
                        n_momentum_burst += 1

    print(f"Scored {len(scored_signals)} signals "
          f"({n_crossover} crossover + {n_breakout} breakout + "
          f"{n_momentum_burst} momentum burst)")
    print()

    if not scored_signals:
        print("No signals found. Try a longer period with --days.")
        return

    # Sort by timestamp for chronological simulation
    scored_signals.sort(key=lambda x: x[0])

    # Per-symbol dedup: at same timestamp, keep highest probability
    deduped: dict[int, dict[str, tuple[int, float, str]]] = {}
    for ts, asset, direction, proba, label in scored_signals:
        if ts not in deduped:
            deduped[ts] = {}
        existing = deduped[ts].get(asset)
        if existing is None or proba > existing[1]:
            deduped[ts][asset] = (direction, proba, label)

    # Run simulation for each threshold
    results: list[BacktestResult] = []
    # threshold → (ema_count, vb_count, mb_count)
    signal_counts: dict[str, tuple[int, int, int]] = {}

    for threshold in THRESHOLDS:
        label = f"T={threshold:.2f}"
        if abs(threshold - optimal_threshold) < 0.005:
            label += " (calibrated)"

        sim = PortfolioSimulator(cfg, label=label, leverage_caps=leverage_caps)
        ema_count = vb_count = mb_count = 0

        for ts in timeline:
            # Check exits first
            for sym in list(sim.open_positions.keys()):
                if ts in asset_time_idx.get(sym, {}):
                    sim.check_exits(sym, asset_candles[sym][asset_time_idx[sym][ts]])

            # Try opens from deduped signals
            if ts in deduped:
                for asset, (direction, proba, sig_label) in deduped[ts].items():
                    if proba >= threshold:
                        bar_idx = asset_time_idx[asset][ts]
                        entry_price = asset_candles[asset][bar_idx]["c"]
                        if sim.try_open(asset, direction, entry_price, ts):
                            if sig_label == "ema":
                                ema_count += 1
                            elif sig_label == "vb":
                                vb_count += 1
                            else:
                                mb_count += 1

        sim.force_close_all(asset_candles)
        results.append(BacktestResult.from_simulator(sim, label))
        signal_counts[label] = (ema_count, vb_count, mb_count)

    # Output
    if args.json:
        print_results_json(results)
    else:
        # Extended table with signal breakdown
        print("=" * 130)
        print(f"{'Threshold':<22} {'Trades':>6} {'EMA':>5} {'VB':>5} {'MB':>5} {'Wins':>5} {'Win%':>6} "
              f"{'Net P&L':>9} {'MaxDD':>8} {'Fees':>8} {'PF':>6} {'Sharpe':>7} {'Assets':>6}")
        print("-" * 130)
        for r in results:
            counts = signal_counts.get(r.label, (0, 0, 0))
            ema_c, vb_c, mb_c = counts[0], counts[1], counts[2]
            marker = " ◀" if "calibrated" in r.label else ""
            print(f"{r.label:<22} {r.count:>6} {ema_c:>5} {vb_c:>5} {mb_c:>5} {r.wins:>5} {r.win_rate:>5.1f}% "
                  f"${r.net_pnl:>+7.2f} ${r.max_drawdown:>6.2f} "
                  f"${r.total_fees:>6.2f} {r.profit_factor:>6.2f} {r.sharpe:>7.2f} {r.unique_assets:>5}{marker}")
        print("=" * 130)

        # Find sweet spot
        profitable = [r for r in results if r.net_pnl > 0]
        if profitable:
            best = max(profitable, key=lambda r: r.net_pnl)
            print(f"\nBest by Net P&L: {best.label} "
                  f"(${best.net_pnl:+.2f}, {best.win_rate:.1f}% win rate, "
                  f"{best.count} trades)")
            best_sharpe = max(profitable, key=lambda r: r.sharpe)
            print(f"Best by Sharpe:  {best_sharpe.label} "
                  f"(Sharpe={best_sharpe.sharpe:.2f}, ${best_sharpe.net_pnl:+.2f})")
        else:
            print("\nNo profitable threshold found in this period.")

        # Show top trades for the best threshold
        if profitable:
            best_result = max(profitable, key=lambda r: r.net_pnl)
            print_top_bottom_trades(best_result.trades, best_result.label)


def _predict(model: object, features: dict, feature_names: list[str] | None) -> float:
    """Run ML prediction, handling backward compat with fewer features."""
    row = pd.DataFrame([features])

    if feature_names:
        # Add missing features with 0.0 default so XGBoost gets all expected cols
        for f in feature_names:
            if f not in row.columns:
                row[f] = 0.0
        row = row[feature_names].astype(float)
    else:
        row = row.astype(float)

    return float(model.predict_proba(row)[:, 1][0])  # type: ignore[union-attr]
