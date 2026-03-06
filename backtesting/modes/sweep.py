"""Sweep mode: grid-search TP/SL/threshold/momentum_exit/breakeven for optimal parameters.

Pre-scores all signals once, then replays through every parameter combination.
Outputs a ranked table sorted by Sharpe ratio (or profit factor).
"""

from __future__ import annotations

import argparse
import itertools
import time
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
from backtesting.simulator import PortfolioSimulator
from backtesting.stats import BacktestResult

# ---------------------------------------------------------------------------
# Default sweep grid
# ---------------------------------------------------------------------------

SWEEP_GRID: dict[str, list[float]] = {
    "tp_pct":                       [0.015, 0.020, 0.025, 0.030, 0.035],  # 1.5% - 3.5%
    "sl_pct":                       [0.006, 0.008, 0.010, 0.012, 0.015],  # 0.6% - 1.5%
    "threshold":                    [0.55, 0.58, 0.60, 0.62, 0.65],       # ML probability
    "momentum_exit_min_profit_pct": [0.09],                                # 9.0% = disabled (production)
    "breakeven_threshold_pct":      [0.010],                               # 1.0% = production value
}

# Fixed slippage (middle value) to keep grid manageable
FIXED_SLIPPAGE_PCT: float = 0.0005  # 0.05%


def _score_all_signals(
    asset_candles: dict[str, list[dict]],
    asset_indicators: dict[str, dict],
    asset_bb: dict[str, tuple[np.ndarray, np.ndarray]],
    asset_time_idx: dict[str, dict[int, int]],
    timeline: list[int],
    warmup: int,
    model: object,
    feature_names: list[str] | None,
    has_breakout: bool,
) -> list[tuple[int, str, int, float, str]]:
    """Score all signals (3 paths) across all assets. Returns sorted list."""
    scored: list[tuple[int, str, int, float, str]] = []
    n_ema = n_vb = n_mb = 0

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

            # PATH 1: EMA crossover (TREND only)
            if ind["is_trend"][bar_idx]:
                sig = signal_ema_crossover_entry(ind, bar_idx)
                if sig != 0:
                    feats = _extract_features(
                        candles, ind, bar_idx, bb_upper, bb_lower, signal_type=0.0)
                    if feats is not None:
                        proba = _predict(model, feats, feature_names)
                        scored.append((ts, asset, sig, proba, "ema"))
                        n_ema += 1

            # PATH 2: Volume breakout (CHAOS + TREND)
            if has_breakout and _is_chaos_or_trend(ind, bar_idx):
                sig_vb = signal_volume_breakout_entry(ind, bar_idx)
                if sig_vb != 0:
                    feats = _extract_features(
                        candles, ind, bar_idx, bb_upper, bb_lower, signal_type=1.0)
                    if feats is not None:
                        proba = _predict(model, feats, feature_names)
                        scored.append((ts, asset, sig_vb, proba, "vb"))
                        n_vb += 1

            # PATH 3: Momentum burst (CHAOS + TREND)
            if _is_chaos_or_trend(ind, bar_idx):
                sig_mb = signal_momentum_burst_entry(ind, bar_idx)
                if sig_mb != 0:
                    feats = _extract_features(
                        candles, ind, bar_idx, bb_upper, bb_lower, signal_type=2.0)
                    if feats is not None:
                        proba = _predict(model, feats, feature_names)
                        scored.append((ts, asset, sig_mb, proba, "mb"))
                        n_mb += 1

    print(f"Scored {len(scored)} signals "
          f"({n_ema} ema + {n_vb} vb + {n_mb} mb)")

    # Sort chronologically
    scored.sort(key=lambda x: x[0])
    return scored


def _dedup_signals(
    scored: list[tuple[int, str, int, float, str]],
) -> dict[int, dict[str, tuple[int, float, str]]]:
    """Per-symbol dedup: at same timestamp, keep highest probability."""
    deduped: dict[int, dict[str, tuple[int, float, str]]] = {}
    for ts, asset, direction, proba, label in scored:
        if ts not in deduped:
            deduped[ts] = {}
        existing = deduped[ts].get(asset)
        if existing is None or proba > existing[1]:
            deduped[ts][asset] = (direction, proba, label)
    return deduped


def _simulate_combo(
    cfg: BacktestConfig,
    threshold: float,
    deduped: dict[int, dict[str, tuple[int, float, str]]],
    timeline: list[int],
    asset_candles: dict[str, list[dict]],
    asset_time_idx: dict[str, dict[int, int]],
    label: str,
    leverage_caps: dict[str, int] | None = None,
) -> BacktestResult:
    """Run one simulation with a specific parameter combination."""
    sim = PortfolioSimulator(cfg, label=label, leverage_caps=leverage_caps)

    for ts in timeline:
        # Check exits first
        for sym in list(sim.open_positions.keys()):
            if ts in asset_time_idx.get(sym, {}):
                sim.check_exits(sym, asset_candles[sym][asset_time_idx[sym][ts]])

        # Try opens from deduped signals
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
    feature_names = (
        list(model.get_booster().feature_names)
        if model.get_booster().feature_names else None
    )

    n_model_features = getattr(model, "n_features_in_", 13)
    has_breakout = n_model_features >= 13

    # Build sweep grid
    grid = SWEEP_GRID
    combos = list(itertools.product(
        grid["tp_pct"],
        grid["sl_pct"],
        grid["threshold"],
        grid["momentum_exit_min_profit_pct"],
        grid["breakeven_threshold_pct"],
    ))
    n_combos = len(combos)

    print("=" * 140)
    print(f"PARAMETER SWEEP: {n_combos} combinations -- Last {days}d -- All Assets ({tf})")
    print("=" * 140)
    print(f"Grid: TP% {grid['tp_pct']} | SL% {grid['sl_pct']} | "
          f"Threshold {grid['threshold']}")
    print(f"      MomExit {grid['momentum_exit_min_profit_pct']} | "
          f"Breakeven {grid['breakeven_threshold_pct']} | "
          f"Slippage fixed={FIXED_SLIPPAGE_PCT*100:.2f}%")
    print(f"Base config: {cfg.leverage}x leverage | ${cfg.account_size} | "
          f"{cfg.position_pct*100:.0f}% size")
    print()

    # Fetch data
    now_ms = int(time.time() * 1000)
    extra_days = 4 if tf == "5m" else 3
    start_ms = now_ms - (days + extra_days) * 86_400_000
    signal_cutoff_ms = now_ms - days * 86_400_000

    assets, leverage_caps = get_all_assets_with_info()
    asset_candles, _, _ = fetch_all_candles(
        assets, tf, start_ms, now_ms, cfg.exclude_symbols, warmup)

    capped = sum(1 for v in leverage_caps.values() if v < cfg.leverage)
    print(f"Leverage caps: {capped}/{len(leverage_caps)} assets below {cfg.leverage}x")

    if not asset_candles:
        print("No assets with enough data.")
        return

    # Compute indicators + Bollinger
    print("Computing indicators...")
    asset_indicators: dict[str, dict] = {}
    asset_bb: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for asset, candles in asset_candles.items():
        ind = compute_indicators(candles, cfg, tf_scale)
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

    # Score all signals ONCE
    print("Scoring all signals with ML model...")
    scored = _score_all_signals(
        asset_candles, asset_indicators, asset_bb, asset_time_idx,
        timeline, warmup, model, feature_names, has_breakout)

    if not scored:
        print("No signals found. Try a longer period with --days.")
        return

    deduped = _dedup_signals(scored)

    # Run all combinations
    print(f"\nRunning {n_combos} parameter combinations...")
    results: list[tuple[float, float, float, float, float, BacktestResult]] = []

    for i, (tp, sl, thresh, mom_exit, be_thresh) in enumerate(combos, 1):
        if i % 50 == 0 or i == n_combos:
            print(f"  Progress: {i}/{n_combos}")

        combo_cfg = BacktestConfig(
            account_size=cfg.account_size,
            entry_fee_pct=cfg.entry_fee_pct,
            exit_fee_pct=cfg.exit_fee_pct,
            slippage_pct=FIXED_SLIPPAGE_PCT,
            tp_pct=tp,
            sl_pct=sl,
            position_pct=cfg.position_pct,
            leverage=cfg.leverage,
            max_positions=cfg.max_positions,
            max_daily_trades=cfg.max_daily_trades,
            momentum_exit_min_profit_pct=mom_exit,
            breakeven_threshold_pct=be_thresh,
        )

        label = (f"TP={tp*100:.1f}|SL={sl*100:.1f}|T={thresh:.2f}"
                 f"|ME={mom_exit*100:.1f}|BE={be_thresh*100:.1f}")
        result = _simulate_combo(
            combo_cfg, thresh, deduped, timeline,
            asset_candles, asset_time_idx, label, leverage_caps)

        results.append((tp, sl, thresh, mom_exit, be_thresh, result))

    # Sort by Sharpe (primary), profit factor (secondary)
    results.sort(key=lambda x: (x[5].sharpe, x[5].profit_factor), reverse=True)

    # Output
    if args.json:
        import json
        out = []
        for tp, sl, thresh, mom_exit, be_thresh, r in results:
            d = r.to_dict()
            d.update({
                "tp_pct": tp, "sl_pct": sl, "threshold": thresh,
                "slippage_pct": FIXED_SLIPPAGE_PCT,
                "momentum_exit_min_profit_pct": mom_exit,
                "breakeven_threshold_pct": be_thresh,
            })
            out.append(d)
        print(json.dumps(out, indent=2))
        return

    # Print top 20
    n_top = min(20, len(results))
    n_bottom = min(5, len(results))

    print()
    hdr = (f"{'Rank':<5} {'TP%':>5} {'SL%':>5} {'Thresh':>7} {'MomEx%':>7} {'BE%':>5} "
           f"{'Trades':>6} {'WinR':>5} {'Net P&L':>9} {'PF':>6} {'Sharpe':>7} "
           f"{'MaxDD':>8} {'Assets':>6}")
    print("=" * len(hdr))
    print(hdr)
    print("-" * len(hdr))

    for rank, (tp, sl, thresh, mom_exit, be_thresh, r) in enumerate(results[:n_top], 1):
        print(f"{rank:>4}  {tp*100:>4.1f}% {sl*100:>4.1f}% {thresh:>6.2f} "
              f"{mom_exit*100:>6.1f}% {be_thresh*100:>4.1f}% "
              f"{r.count:>6} {r.win_rate:>4.1f}% "
              f"${r.net_pnl:>+7.2f} {r.profit_factor:>6.2f} {r.sharpe:>7.2f} "
              f"${r.max_drawdown:>6.2f} {r.unique_assets:>5}")

    if len(results) > n_top:
        print(f"{'...':^{len(hdr)}}")
        print(f"\nBottom {n_bottom}:")
        print("-" * len(hdr))
        for rank, (tp, sl, thresh, mom_exit, be_thresh, r) in enumerate(
                results[-n_bottom:], len(results) - n_bottom + 1):
            print(f"{rank:>4}  {tp*100:>4.1f}% {sl*100:>4.1f}% {thresh:>6.2f} "
                  f"{mom_exit*100:>6.1f}% {be_thresh*100:>4.1f}% "
                  f"{r.count:>6} {r.win_rate:>4.1f}% "
                  f"${r.net_pnl:>+7.2f} {r.profit_factor:>6.2f} {r.sharpe:>7.2f} "
                  f"${r.max_drawdown:>6.2f} {r.unique_assets:>5}")

    print("=" * len(hdr))

    # Summary
    profitable = [x for x in results if x[5].net_pnl > 0]
    print(f"\nSummary: {len(profitable)}/{len(results)} profitable combinations")

    if profitable:
        best_sharpe = profitable[0]
        tp, sl, thresh, mom_exit, be_thresh, r = best_sharpe
        print(f"\nBest by Sharpe: TP={tp*100:.1f}% SL={sl*100:.1f}% "
              f"Threshold={thresh:.2f} MomExit={mom_exit*100:.1f}% "
              f"Breakeven={be_thresh*100:.1f}%")
        print(f"  -> {r.count} trades, {r.win_rate:.1f}% win, "
              f"${r.net_pnl:+.2f} P&L, Sharpe={r.sharpe:.2f}, PF={r.profit_factor:.2f}")

        best_pnl = max(profitable, key=lambda x: x[5].net_pnl)
        if best_pnl != best_sharpe:
            tp, sl, thresh, mom_exit, be_thresh, r = best_pnl
            print(f"\nBest by P&L:    TP={tp*100:.1f}% SL={sl*100:.1f}% "
                  f"Threshold={thresh:.2f} MomExit={mom_exit*100:.1f}% "
                  f"Breakeven={be_thresh*100:.1f}%")
            print(f"  -> {r.count} trades, {r.win_rate:.1f}% win, "
                  f"${r.net_pnl:+.2f} P&L, Sharpe={r.sharpe:.2f}, PF={r.profit_factor:.2f}")
    else:
        print("\nNo profitable combination found in this period.")
