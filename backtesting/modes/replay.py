"""Replay mode: high-fidelity simulation replaying historical data through
the exact same logic as the live trading bot.

Uses ReplaySimulator with the full 7-stage exit pipeline (SL, TP, breakeven,
trailing, ROI, momentum fade, regime change) and enriched entry checks
(cooldowns, daily cap per symbol, correlation filter, optional Kelly sizing).

Follows the same signal-scoring pattern as sweep.py and threshold.py.
"""

from __future__ import annotations

import argparse
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
from backtesting.simulator import ReplaySimulator
from backtesting.stats import BacktestResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _score(
    model: object,
    feature_names: list[str] | None,
    candles: list[dict],
    ind: dict,
    idx: int,
    bb_upper: np.ndarray,
    bb_lower: np.ndarray,
    signal_type: float,
    use_ml: bool,
) -> float:
    """Wrap _extract_features + _predict.  Returns 1.0 if ML is disabled."""
    if not use_ml:
        return 1.0
    feats = _extract_features(candles, ind, idx, bb_upper, bb_lower, signal_type)
    if feats is None:
        return 0.0
    return _predict(model, feats, feature_names)


def _print_replay_summary(
    cfg: BacktestConfig,
    result: BacktestResult,
    sim: ReplaySimulator,
    days: int,
    tf: str,
    threshold: float,
    use_kelly: bool,
    use_ml: bool,
    n_signals_raw: int,
    n_signals_dedup: int,
) -> None:
    """Print a detailed replay summary with config, stats, and exit breakdown."""
    print()
    print("=" * 90)
    print("REPLAY SUMMARY")
    print("=" * 90)

    # Config block
    print(f"\nPeriod:      last {days} days ({tf})")
    print(f"Account:     ${cfg.account_size:.2f}")
    print(f"Sizing:      {cfg.position_pct*100:.0f}% @ {cfg.leverage}x"
          f"{'  (Kelly)' if use_kelly else ''}")
    ml_str = f"ON (threshold={threshold:.2f})" if use_ml else "OFF"
    print(f"ML model:    {ml_str}")
    print(f"TP / SL:     {cfg.tp_pct*100:.1f}% / {cfg.sl_pct*100:.1f}%  "
          f"(R:R = 1:{cfg.tp_pct/cfg.sl_pct:.1f})" if cfg.sl_pct > 0 else "")
    print(f"Breakeven:   {cfg.breakeven_threshold_pct*100:.1f}%")
    print(f"Trail ATR:   {cfg.trailing_atr_mult}x")
    print(f"MomFade:     min_profit={cfg.momentum_exit_min_profit_pct*100:.1f}%  "
          f"rsi_slope_thresh={cfg.momentum_rsi_slope_threshold}")
    print(f"Cooldown:    {cfg.cooldown_minutes}min (SL: {cfg.cooldown_after_sl_minutes}min)")
    print(f"Max/sym/day: {cfg.max_trades_per_symbol_per_day}")
    print(f"Signals:     {n_signals_raw} raw -> {n_signals_dedup} deduped")
    print()

    # Stats block
    print(f"{'Metric':<25} {'Value':>12}")
    print("-" * 40)
    print(f"{'Trades':<25} {result.count:>12}")
    print(f"{'Wins':<25} {result.wins:>12}")
    print(f"{'Win Rate':<25} {result.win_rate:>11.1f}%")
    print(f"{'Net P&L':<25} ${result.net_pnl:>+10.2f}")
    print(f"{'Total Fees':<25} ${result.total_fees:>10.2f}")
    print(f"{'Max Drawdown':<25} ${result.max_drawdown:>10.2f}")
    print(f"{'Profit Factor':<25} {result.profit_factor:>12.2f}")
    print(f"{'Sharpe Ratio':<25} {result.sharpe:>12.2f}")
    print(f"{'Unique Assets':<25} {result.unique_assets:>12}")
    print(f"{'Final Equity':<25} ${sim.equity:>10.2f}")

    # Exit reason breakdown
    if sim.exit_reasons:
        print()
        print(f"{'Exit Reason':<15} {'Count':>6} {'Pct':>7}")
        print("-" * 30)
        total_exits = sum(sim.exit_reasons.values())
        for reason in ["SL", "TP", "TRAIL", "BREAKEVEN", "ROI", "MOM_FADE",
                        "REGIME", "CLOSE"]:
            count = sim.exit_reasons.get(reason, 0)
            if count > 0:
                pct = count / total_exits * 100
                print(f"  {reason:<13} {count:>6} {pct:>6.1f}%")
        # Any remaining reasons
        for reason, count in sorted(sim.exit_reasons.items()):
            if reason not in {"SL", "TP", "TRAIL", "BREAKEVEN", "ROI",
                              "MOM_FADE", "REGIME", "CLOSE"}:
                pct = count / total_exits * 100
                print(f"  {reason:<13} {count:>6} {pct:>6.1f}%")
        print(f"  {'TOTAL':<13} {total_exits:>6}")

    # Top/bottom trades
    if result.trades:
        print()
        sorted_trades = sorted(result.trades, key=lambda t: t["net"], reverse=True)
        n_show = min(5, len(sorted_trades))
        print(f"Top {n_show} trades:")
        for t in sorted_trades[:n_show]:
            d = "LONG" if t["direction"] == 1 else "SHORT"
            print(f"  {t['symbol']:<10} {d:<5} ${t['net']:>+.4f} ({t['reason']})  "
                  f"notional=${t['notional']:.1f}")
        print(f"\nBottom {n_show} trades:")
        for t in sorted_trades[-n_show:]:
            d = "LONG" if t["direction"] == 1 else "SHORT"
            print(f"  {t['symbol']:<10} {d:<5} ${t['net']:>+.4f} ({t['reason']})  "
                  f"notional=${t['notional']:.1f}")

    print("=" * 90)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> None:
    """Run a full replay simulation."""
    days = args.days if args.days is not None else 7
    tf = args.timeframe or "15m"
    account = args.account or 86.0
    threshold = getattr(args, "threshold", None) or 0.52
    use_kelly = getattr(args, "kelly", False)
    use_ml = not getattr(args, "no_ml", False)
    verbose = getattr(args, "verbose", False)
    bar_log_path = getattr(args, "bar_log", None)

    cfg = load_config(timeframe=tf, lookback_days=days, account_size=account)

    warmup = {"5m": 650, "15m": 200, "1h": 200}.get(tf, 200)
    cfg.warmup_bars = warmup
    tf_scale = {"5m": 3, "15m": 1, "1h": 1}.get(tf, 1)

    print("=" * 90)
    print(f"REPLAY ENGINE: Last {days}d -- All Assets ({tf})")
    print("=" * 90)
    print(f"Config: TP {cfg.tp_pct*100:.1f}% | SL {cfg.sl_pct*100:.1f}% | "
          f"{cfg.leverage}x leverage | ${cfg.account_size} | "
          f"{cfg.position_pct*100:.0f}% size")
    ml_label = f"ON (threshold={threshold:.2f})" if use_ml else "OFF"
    print(f"ML: {ml_label} | "
          f"Kelly: {'ON' if use_kelly else 'OFF'} | "
          f"Trail ATR: {cfg.trailing_atr_mult}x")
    print()

    # Load ML model
    model = None
    feature_names: list[str] | None = None
    has_breakout = True

    if use_ml:
        model_path = (
            Path(__file__).resolve().parent.parent.parent
            / "models" / "trade_model.joblib"
        )
        if not model_path.exists():
            print(f"WARNING: ML model not found at {model_path} -- running without ML")
            use_ml = False
        else:
            import joblib
            payload = joblib.load(model_path)
            model = payload["model"]
            feature_names = (
                list(model.get_booster().feature_names)
                if model.get_booster().feature_names else None
            )
            n_model_features = getattr(model, "n_features_in_", 13)
            has_breakout = n_model_features >= 13
            print(f"ML model loaded: {n_model_features} features, "
                  f"breakout={'ON' if has_breakout else 'OFF'}")

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

    # Compute indicators + Bollinger bands
    print("Computing indicators...")
    asset_indicators: dict[str, dict] = {}
    asset_bb: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for asset, candles in asset_candles.items():
        ind = compute_indicators(candles, cfg, tf_scale)
        _, bb_upper, bb_lower = calc_bollinger(ind["closes"], 20 * tf_scale)
        asset_indicators[asset] = ind
        asset_bb[asset] = (bb_upper, bb_lower)

    # Build timeline (sorted unique timestamps after signal cutoff)
    all_timestamps: set[int] = set()
    for candles in asset_candles.values():
        for c in candles:
            if c["t"] >= signal_cutoff_ms:
                all_timestamps.add(c["t"])
    timeline = sorted(all_timestamps)

    asset_time_idx: dict[str, dict[int, int]] = {}
    for asset, candles in asset_candles.items():
        asset_time_idx[asset] = {c["t"]: i for i, c in enumerate(candles)}

    # -----------------------------------------------------------------
    # Pre-score ALL signals (3 paths) with ML probability
    # -----------------------------------------------------------------
    print("Scoring all signals with ML model...")
    # (timestamp, asset, direction, proba, signal_label)
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
                    proba = _score(
                        model, feature_names, candles, ind, bar_idx,
                        bb_upper, bb_lower, signal_type=0.0, use_ml=use_ml)
                    scored.append((ts, asset, sig, proba, "ema"))
                    n_ema += 1

            # PATH 2: Volume breakout (CHAOS + TREND)
            if has_breakout and _is_chaos_or_trend(ind, bar_idx):
                sig_vb = signal_volume_breakout_entry(ind, bar_idx)
                if sig_vb != 0:
                    proba = _score(
                        model, feature_names, candles, ind, bar_idx,
                        bb_upper, bb_lower, signal_type=1.0, use_ml=use_ml)
                    scored.append((ts, asset, sig_vb, proba, "vb"))
                    n_vb += 1

            # PATH 3: Momentum burst (CHAOS + TREND)
            if _is_chaos_or_trend(ind, bar_idx):
                sig_mb = signal_momentum_burst_entry(ind, bar_idx)
                if sig_mb != 0:
                    proba = _score(
                        model, feature_names, candles, ind, bar_idx,
                        bb_upper, bb_lower, signal_type=2.0, use_ml=use_ml)
                    scored.append((ts, asset, sig_mb, proba, "mb"))
                    n_mb += 1

    n_signals_raw = len(scored)
    print(f"Scored {n_signals_raw} signals "
          f"({n_ema} ema + {n_vb} vb + {n_mb} mb)")

    if not scored:
        print("No signals found. Try a longer period with --days.")
        return

    # Sort chronologically
    scored.sort(key=lambda x: x[0])

    # Dedup: same symbol+timestamp -> keep highest proba
    deduped: dict[int, dict[str, tuple[int, float, str]]] = {}
    for ts, asset, direction, proba, label in scored:
        if ts not in deduped:
            deduped[ts] = {}
        existing = deduped[ts].get(asset)
        if existing is None or proba > existing[1]:
            deduped[ts][asset] = (direction, proba, label)

    n_signals_dedup = sum(len(v) for v in deduped.values())
    print(f"After dedup: {n_signals_dedup} unique (symbol, timestamp) entries")

    # -----------------------------------------------------------------
    # Walk the timeline: exits first, then entries
    # -----------------------------------------------------------------
    print("\nRunning replay simulation...")
    sim = ReplaySimulator(
        cfg,
        label="replay",
        leverage_caps=leverage_caps,
        timeframe=tf,
        use_kelly=use_kelly,
    )

    # Optional bar-level log
    bar_log_file = None
    if bar_log_path:
        bar_log_file = open(bar_log_path, "w")
        bar_log_file.write("timestamp,action,symbol,direction,price,reason,equity\n")

    for ts in timeline:
        # --- Exits first ---
        for sym in list(sim.open_positions.keys()):
            if ts not in asset_time_idx.get(sym, {}):
                continue
            bar_idx = asset_time_idx[sym][ts]
            candle = asset_candles[sym][bar_idx]
            ind = asset_indicators[sym]

            # Current regime and RSI slope at this bar
            current_regime = bool(ind["is_trend"][bar_idx])
            rsi_slope_val = ind["rsi_slope"][bar_idx]
            current_rsi_slope = float(rsi_slope_val) if not np.isnan(rsi_slope_val) else 0.0

            trades_before = len(sim.trades)
            sim.check_exits(sym, candle, current_regime, current_rsi_slope)

            # Log if a trade was closed
            if bar_log_file and len(sim.trades) > trades_before:
                t = sim.trades[-1]
                bar_log_file.write(
                    f"{ts},CLOSE,{t['symbol']},{t['direction']},"
                    f"{t['exit']},{t['reason']},{sim.equity:.4f}\n")

            if verbose and len(sim.trades) > trades_before:
                t = sim.trades[-1]
                d = "LONG" if t["direction"] == 1 else "SHORT"
                print(f"  [{ts}] CLOSE {t['symbol']} {d} "
                      f"${t['net']:+.4f} ({t['reason']})")

        # --- Entries ---
        if ts in deduped:
            for asset, (direction, proba, _sig_label) in deduped[ts].items():
                if proba < threshold:
                    continue
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

                opened = sim.try_open(
                    asset, direction, entry_price, ts,
                    ml_proba=proba,
                    entry_regime=entry_regime,
                    _entry_rsi_slope=entry_rsi_slope,
                    entry_atr_pct=entry_atr_pct,
                )

                if opened and bar_log_file:
                    bar_log_file.write(
                        f"{ts},OPEN,{asset},{direction},"
                        f"{entry_price},{_sig_label},{sim.equity:.4f}\n")

                if opened and verbose:
                    d = "LONG" if direction == 1 else "SHORT"
                    print(f"  [{ts}] OPEN  {asset} {d} "
                          f"@{entry_price:.4f} (p={proba:.3f})")

    # Force-close remaining positions
    sim.force_close_all(asset_candles)

    if bar_log_file:
        bar_log_file.close()
        print(f"\nBar log written to: {bar_log_path}")

    # -----------------------------------------------------------------
    # Output
    # -----------------------------------------------------------------
    result = BacktestResult.from_simulator(sim, "replay")

    if getattr(args, "json", False):
        import json
        out = result.to_dict()
        out["exit_reasons"] = sim.exit_reasons
        print(json.dumps(out, indent=2))
    else:
        _print_replay_summary(
            cfg, result, sim, days, tf, threshold,
            use_kelly, use_ml, n_signals_raw, n_signals_dedup)
