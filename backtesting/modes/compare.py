"""Compare multiple ReplaySimulator configs on the same candle data.

Fetches candles ONCE, then runs N configurations sequentially.
Much faster than calling `replay --days 7` N times.

Usage:
    python -m backtesting.modes.compare
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

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
class ConfigVariant:
    label: str
    overrides: dict[str, float]


def _score(
    model: object,
    feature_names: list[str] | None,
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
) -> float:
    feats = _extract_features(
        candles, ind, idx, bb_upper, bb_lower, signal_type,
        direction=direction, btc_ind=btc_ind, btc_time_idx=btc_time_idx,
        ind_1h=ind_1h,
    )
    if feats is None:
        return 0.0
    return _predict(model, feats, feature_names)


def run_comparison(
    days: int = 7,
    account: float = 64.0,
    threshold: float = 0.52,
    variants: list[ConfigVariant] | None = None,
) -> None:
    tf = "15m"
    cfg_base = load_config(timeframe=tf, lookback_days=days, account_size=account)
    warmup = 200
    cfg_base.warmup_bars = warmup

    # Default variants if not specified
    if variants is None:
        variants = [
            ConfigVariant("A) SL=1.0 MF=0.5 (current)", {}),
            ConfigVariant("B) SL=1.0 MF=1.0",           {"momentum_exit_min_profit_pct": 0.01}),
            ConfigVariant("C) SL=1.0 MF=1.5",           {"momentum_exit_min_profit_pct": 0.015}),
            ConfigVariant("D) SL=1.0 MF=OFF",           {"momentum_exit_min_profit_pct": 9.0}),
            ConfigVariant("E) SL=0.5 MF=0.5",           {"sl_pct": 0.005}),
            ConfigVariant("F) SL=1.5 MF=0.5",           {"sl_pct": 0.015}),
            ConfigVariant("G) SL=1.5 MF=1.0",           {"sl_pct": 0.015, "momentum_exit_min_profit_pct": 0.01}),
        ]

    # ── Fetch data ONCE ──────────────────────────────────────────────────
    print(f"Fetching candle data for {days}d ...")
    now_ms = int(time.time() * 1000)
    extra_days = 3
    start_ms = now_ms - (days + extra_days) * 86_400_000
    signal_cutoff_ms = now_ms - days * 86_400_000

    assets, leverage_caps = get_all_assets_with_info()
    asset_candles, _, _ = fetch_all_candles(
        assets, tf, start_ms, now_ms, cfg_base.exclude_symbols, warmup,
    )

    # Compute indicators
    asset_indicators: dict[str, dict] = {}
    asset_indicators_1h: dict[str, dict] = {}
    asset_bb: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for asset, candles in asset_candles.items():
        ind = compute_indicators(candles, cfg_base)
        ind_1h = compute_indicators(candles, cfg_base, timeframe_scale=4)
        _, bb_upper, bb_lower = calc_bollinger(ind["closes"], 20)
        asset_indicators[asset] = ind
        asset_indicators_1h[asset] = ind_1h
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

    # Load ML model
    model_path = Path(__file__).resolve().parent.parent.parent / "models" / "trade_model.joblib"
    import joblib
    payload = joblib.load(model_path)
    model = payload["model"]
    feature_names: list[str] | None = (
        list(model.get_booster().feature_names)
        if model.get_booster().feature_names else None
    )
    n_features = getattr(model, "n_features_in_", 13)
    has_breakout = n_features >= 13

    # BTC context indicators
    btc_ind: dict | None = asset_indicators.get("BTC")
    btc_time_idx_map: dict[int, int] | None = asset_time_idx.get("BTC")

    # ── Pre-score all signals ONCE ───────────────────────────────────────
    print("Scoring signals ...")
    # Signal: (ts, asset, direction, proba, label, bar_idx)
    raw_signals: list[tuple[int, str, int, float, str, int]] = []

    for asset in asset_candles:
        candles = asset_candles[asset]
        ind = asset_indicators[asset]
        ind_1h = asset_indicators_1h[asset]
        bb_upper, bb_lower = asset_bb[asset]

        # For BTC itself, don't pass BTC context
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
                    p = _score(model, feature_names, candles, ind, bidx,
                               bb_upper, bb_lower, 0.0, direction=sig,
                               btc_ind=a_btc_ind, btc_time_idx=a_btc_time_idx,
                               ind_1h=ind_1h)
                    if p > 0:
                        raw_signals.append((ts, asset, sig, p, "ema", bidx))

            # PATH 2: Volume breakout (CHAOS + TREND)
            if has_breakout and _is_chaos_or_trend(ind, bidx):
                sig = signal_volume_breakout_entry(ind, bidx)
                if sig != 0:
                    p = _score(model, feature_names, candles, ind, bidx,
                               bb_upper, bb_lower, 1.0, direction=sig,
                               btc_ind=a_btc_ind, btc_time_idx=a_btc_time_idx,
                               ind_1h=ind_1h)
                    if p > 0:
                        raw_signals.append((ts, asset, sig, p, "vb", bidx))

            # PATH 3: Momentum burst (CHAOS + TREND)
            if _is_chaos_or_trend(ind, bidx):
                sig = signal_momentum_burst_entry(ind, bidx)
                if sig != 0:
                    p = _score(model, feature_names, candles, ind, bidx,
                               bb_upper, bb_lower, 2.0, direction=sig,
                               btc_ind=a_btc_ind, btc_time_idx=a_btc_time_idx,
                               ind_1h=ind_1h)
                    if p > 0:
                        raw_signals.append((ts, asset, sig, p, "mb", bidx))

    raw_signals.sort(key=lambda x: x[0])

    # Dedup: same symbol+ts → keep highest proba
    deduped: dict[int, dict[str, tuple[int, float, str, int]]] = {}
    for ts, asset, direction, proba, label, bidx in raw_signals:
        if ts not in deduped:
            deduped[ts] = {}
        ex = deduped[ts].get(asset)
        if ex is None or proba > ex[1]:
            deduped[ts][asset] = (direction, proba, label, bidx)

    n_signals = sum(len(v) for v in deduped.values())
    print(f"Signals: {len(raw_signals)} raw -> {n_signals} deduped\n")

    # ── Run each variant ─────────────────────────────────────────────────
    results: list[tuple[str, BacktestResult, dict[str, int]]] = []

    for variant in variants:
        cfg = copy.copy(cfg_base)
        for k, v in variant.overrides.items():
            setattr(cfg, k, v)

        sim = ReplaySimulator(
            cfg, label=variant.label,
            leverage_caps=leverage_caps,
        )

        for ts in timeline:
            # Exits
            for sym in list(sim.open_positions.keys()):
                if ts not in asset_time_idx.get(sym, {}):
                    continue
                bidx = asset_time_idx[sym][ts]
                candle = asset_candles[sym][bidx]
                ind = asset_indicators[sym]
                regime = bool(ind["is_trend"][bidx])
                rsi_slope = float(ind["rsi_slope"][bidx]) \
                    if not np.isnan(ind["rsi_slope"][bidx]) else 0.0
                sim.check_exits(sym, candle,
                                current_regime=regime,
                                current_rsi_slope=rsi_slope)

            # Entries
            if ts in deduped:
                candidates = sorted(
                    deduped[ts].items(), key=lambda x: x[1][1], reverse=True
                )
                for asset, (direction, proba, _sig_label, bidx) in candidates:
                    if proba < threshold:
                        continue
                    ind = asset_indicators[asset]
                    candle = asset_candles[asset][bidx]
                    sim.try_open(
                        asset, direction, candle["c"], ts,
                        ml_proba=proba,
                        entry_regime=bool(ind["is_trend"][bidx]),
                        entry_atr_pct=float(ind["atr_pct"][bidx]),
                    )

        sim.force_close_all(asset_candles)

        result = BacktestResult.from_simulator(sim, variant.label)
        exit_reasons: dict[str, int] = {}
        for t in result.trades:
            r = t.get("reason", "?")
            exit_reasons[r] = exit_reasons.get(r, 0) + 1
        results.append((variant.label, result, exit_reasons))

    # ── Print comparison table ───────────────────────────────────────────
    print()
    print("=" * 120)
    print(f"REPLAY COMPARISON — Last {days}d — {len(variants)} configs — ${account} account")
    print("=" * 120)
    print(f"{'Config':<30} {'Trades':>6} {'Win%':>6} {'Net P&L':>10} "
          f"{'PF':>6} {'Sharpe':>7} {'MaxDD':>8} "
          f"{'SL':>5} {'TP':>5} {'MF':>5} {'ROI':>5} {'REG':>5} {'TRL':>5}")
    print("-" * 120)

    for label, result, exits in results:
        sl_n = exits.get("SL", 0)
        tp_n = exits.get("TP", 0)
        mf_n = exits.get("MOM_FADE", 0)
        roi_n = exits.get("ROI", 0)
        reg_n = exits.get("REGIME", 0)
        trl_n = exits.get("TRAIL_SL", 0)

        print(f"{label:<30} {result.count:>6} {result.win_rate:>5.1f}% "
              f"${result.net_pnl:>+9.2f} "
              f"{result.profit_factor:>6.2f} {result.sharpe:>7.2f} "
              f"${result.max_drawdown:>7.2f} "
              f"{sl_n:>5} {tp_n:>5} {mf_n:>5} {roi_n:>5} {reg_n:>5} {trl_n:>5}")

    print("=" * 120)


if __name__ == "__main__":
    run_comparison()
