"""Regime mode: grid-search regime detection parameters.

Replaces backtest_regime.py (~536 lines).
Fixes: uses canonical NaN-seeded indicators + hysteresis.
"""

from __future__ import annotations

import argparse
import math
import time
from itertools import product

import numpy as np

from backtesting.api import fetch_all_candles, get_all_assets
from backtesting.config import BacktestConfig, load_config
from backtesting.indicators import compute_indicators, compute_regime_series

# Grid-search values (level hysteresis: entry vs exit ADX thresholds)
ADX_ENTRY_THRESHOLDS = [22, 25, 28, 30]
ADX_EXIT_THRESHOLDS = [18, 20, 22]
CONFIRMATION_BARS = [1, 2, 3]


def _run_regime_backtest(
    asset_data: dict[str, dict],
    cfg: BacktestConfig,
    adx_entry: float,
    adx_exit: float,
    confirm_bars: int,
    use_filter: bool,
) -> dict:
    """Run backtest across all assets with given regime params."""
    override_cfg = BacktestConfig(
        **{k: getattr(cfg, k) for k in cfg.__dataclass_fields__}
    )
    override_cfg.trend_adx_entry_min = adx_entry
    override_cfg.trend_adx_exit_min = adx_exit
    override_cfg.confirmation_bars = confirm_bars

    all_trades: list[dict] = []

    for symbol, ind in asset_data.items():
        closes = ind["closes"]
        highs = ind["highs"]
        lows = ind["lows"]
        n = len(closes)
        if n < 200:
            continue

        ema9 = ind["ema9"]
        ema21 = ind["ema21"]
        rsi = ind["rsi"]
        atr_pct = ind["atr_pct"]
        adx = ind["adx"]
        ema200_slope = ind["ema200_slope"]

        # Recompute regime with overridden params
        if use_filter:
            is_trend = compute_regime_series(adx, ema200_slope, override_cfg)
        else:
            is_trend = np.ones(n, dtype=bool)

        in_trade = False
        entry_price = tp_price = sl_price = 0.0
        trade_dir = 0

        for bar in range(200, n):
            price = closes[bar]
            if price <= 0:
                continue

            # Check exits
            if in_trade:
                if trade_dir == 1:
                    if lows[bar] <= sl_price:
                        pnl = (sl_price / entry_price - 1) * cfg.leverage - 2 * cfg.fee_pct * cfg.leverage
                        all_trades.append({"symbol": symbol, "pnl_pct": pnl, "result": "SL"})
                        in_trade = False
                    elif highs[bar] >= tp_price:
                        pnl = (tp_price / entry_price - 1) * cfg.leverage - 2 * cfg.fee_pct * cfg.leverage
                        all_trades.append({"symbol": symbol, "pnl_pct": pnl, "result": "TP"})
                        in_trade = False
                else:
                    if highs[bar] >= sl_price:
                        pnl = (1 - sl_price / entry_price) * cfg.leverage - 2 * cfg.fee_pct * cfg.leverage
                        all_trades.append({"symbol": symbol, "pnl_pct": pnl, "result": "SL"})
                        in_trade = False
                    elif lows[bar] <= tp_price:
                        pnl = (1 - tp_price / entry_price) * cfg.leverage - 2 * cfg.fee_pct * cfg.leverage
                        all_trades.append({"symbol": symbol, "pnl_pct": pnl, "result": "TP"})
                        in_trade = False
                continue

            # Check entry
            if np.isnan(ema9[bar]) or np.isnan(ema21[bar]) or np.isnan(rsi[bar]):
                continue
            if not is_trend[bar]:
                continue
            if atr_pct[bar] < cfg.min_atr_pct:
                continue

            if ema9[bar] > ema21[bar] and cfg.rsi_long_min <= rsi[bar] <= cfg.rsi_long_max:
                trade_dir = 1
            elif ema9[bar] < ema21[bar] and cfg.rsi_short_min <= rsi[bar] <= cfg.rsi_short_max:
                trade_dir = -1
            else:
                continue

            entry_price = price
            in_trade = True
            if trade_dir == 1:
                tp_price = entry_price * (1 + cfg.tp_pct)
                sl_price = entry_price * (1 - cfg.sl_pct)
            else:
                tp_price = entry_price * (1 - cfg.tp_pct)
                sl_price = entry_price * (1 + cfg.sl_pct)

        # Close open trade
        if in_trade:
            if trade_dir == 1:
                pnl = (closes[-1] / entry_price - 1) * cfg.leverage - 2 * cfg.fee_pct * cfg.leverage
            else:
                pnl = (1 - closes[-1] / entry_price) * cfg.leverage - 2 * cfg.fee_pct * cfg.leverage
            all_trades.append({"symbol": symbol, "pnl_pct": pnl, "result": "OPEN"})

    return _aggregate(all_trades, cfg)


def _aggregate(trades: list[dict], cfg: BacktestConfig) -> dict:
    """Aggregate trade stats."""
    if not trades:
        return {"trades": 0, "wins": 0, "win_rate": 0.0, "total_pnl_pct": 0.0,
                "net_pnl_usd": 0.0, "max_dd_pct": 0.0, "sharpe": 0.0,
                "profit_factor": 0.0, "unique_assets": 0}

    wins = sum(1 for t in trades if t["pnl_pct"] > 0)
    pnls = [t["pnl_pct"] for t in trades]
    total_pnl = sum(pnls)

    # Max drawdown
    cumulative = peak = max_dd = 0.0
    for p in pnls:
        cumulative += p
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    # Sharpe
    if len(pnls) > 1:
        std = float(np.std(pnls))
        avg = total_pnl / len(pnls)
        sharpe = (avg / std) * math.sqrt(96 * 365) if std > 0 else 0.0
    else:
        sharpe = 0.0

    # Profit factor
    gp = sum(p for p in pnls if p > 0)
    gl = abs(sum(p for p in pnls if p < 0))
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)

    trade_size = cfg.account_size * cfg.position_pct * cfg.leverage
    net_usd = sum(t["pnl_pct"] * trade_size for t in trades)

    return {
        "trades": len(trades), "wins": wins,
        "win_rate": wins / len(trades) * 100,
        "total_pnl_pct": total_pnl * 100,
        "net_pnl_usd": net_usd, "max_dd_pct": max_dd * 100,
        "sharpe": sharpe, "profit_factor": pf,
        "unique_assets": len(set(t["symbol"] for t in trades)),
    }


def run(args: argparse.Namespace) -> None:
    days = args.days if args.days is not None else 2
    tf = args.timeframe or "15m"
    cfg = load_config(timeframe=tf, lookback_days=days,
                      account_size=args.account or 86.0)

    tf_scale = {"5m": 3, "15m": 1, "1h": 1}.get(tf, 1)
    warmup = {"5m": 650, "15m": 200, "1h": 200}.get(tf, 200)
    cfg.warmup_bars = warmup

    print("=" * 80)
    print("REGIME DETECTION PARAMETER BACKTEST")
    print("=" * 80)
    print(f"Strategy: EMA9/EMA21 crossover + RSI filter ({tf})")
    print(f"TP: {cfg.tp_pct*100:.1f}%, SL: {cfg.sl_pct*100:.1f}%, "
          f"Fee: {cfg.fee_pct*100:.3f}%/side, Leverage: {cfg.leverage}x")
    print(f"Account: ${cfg.account_size}, Position: {cfg.position_pct*100:.0f}%")
    print()

    now_ms = int(time.time() * 1000)
    extra_days = 4 if tf == "5m" else 3
    start_ms = now_ms - (days + extra_days) * 86_400_000

    assets = get_all_assets()
    asset_candles, _, _ = fetch_all_candles(
        assets, tf, start_ms, now_ms, cfg.exclude_symbols, warmup)

    if not asset_candles:
        print("No assets with enough data.")
        return

    # Compute indicators once
    asset_data: dict[str, dict] = {}
    for asset, candles in asset_candles.items():
        asset_data[asset] = compute_indicators(candles, cfg, tf_scale)

    # Build combos
    combos: list[tuple[str, float, float, int, bool]] = []
    combos.append(("NO_FILTER", 0, 0, 0, False))
    for adx_entry, adx_exit, conf in product(ADX_ENTRY_THRESHOLDS, ADX_EXIT_THRESHOLDS, CONFIRMATION_BARS):
        if adx_exit >= adx_entry:
            continue  # Exit must be lower than entry for hysteresis
        label = f"Entry>={adx_entry} Exit>={adx_exit} Conf={conf}"
        combos.append((label, adx_entry, adx_exit, conf, True))

    print(f"Testing {len(combos)} parameter combinations across "
          f"{len(asset_data)} assets...")
    print()

    results: list[dict] = []
    for i, (label, adx_entry, adx_exit, conf, use_filter) in enumerate(combos):
        stats = _run_regime_backtest(asset_data, cfg, adx_entry, adx_exit, conf, use_filter)
        stats["label"] = label
        results.append(stats)
        if (i + 1) % 10 == 0:
            print(f"  ... tested {i + 1}/{len(combos)} combos")

    results.sort(key=lambda x: x["sharpe"], reverse=True)

    if args.json:
        import json
        print(json.dumps(results, indent=2, default=str))
        return

    # Print table
    print()
    print("=" * 140)
    print(f"{'Rank':<5} {'Parameters':<38} {'Trades':>7} {'Assets':>7} "
          f"{'WinRate':>8} {'TotPnL%':>9} {'PnL$':>8} {'MaxDD%':>8} "
          f"{'Sharpe':>8} {'ProfFact':>9}")
    print("-" * 140)

    for rank, r in enumerate(results, 1):
        pf_str = f"{r['profit_factor']:.2f}" if r['profit_factor'] != float('inf') else "inf"
        print(f"{rank:<5} {r['label']:<38} {r['trades']:>7} "
              f"{r['unique_assets']:>7} {r['win_rate']:>7.1f}% "
              f"{r['total_pnl_pct']:>+8.2f}% {r['net_pnl_usd']:>+7.2f} "
              f"{r['max_dd_pct']:>7.2f}% {r['sharpe']:>8.2f} {pf_str:>9}")

    print("=" * 140)

    if results:
        best = results[0]
        print()
        print("BEST BY SHARPE RATIO:")
        print(f"  Parameters: {best['label']}")
        print(f"  Trades: {best['trades']}, Win Rate: {best['win_rate']:.1f}%")
        print(f"  Total P&L: {best['total_pnl_pct']:+.2f}% (${best['net_pnl_usd']:+.2f})")
        print(f"  Sharpe: {best['sharpe']:.2f}, Profit Factor: {best['profit_factor']:.2f}")

        # Show current config rank
        current_label = (f"Entry>={cfg.trend_adx_entry_min:.0f} "
                         f"Exit>={cfg.trend_adx_exit_min:.0f} "
                         f"Conf={cfg.confirmation_bars}")
        current = [r for r in results if r["label"] == current_label]
        if current:
            curr = current[0]
            curr_rank = results.index(curr) + 1
            print()
            print(f"CURRENT CONFIG (rank #{curr_rank}/{len(results)}):")
            print(f"  Parameters: {curr['label']}")
            print(f"  Trades: {curr['trades']}, Win Rate: {curr['win_rate']:.1f}%")
            print(f"  Sharpe: {curr['sharpe']:.2f}, "
                  f"Profit Factor: {curr['profit_factor']:.2f}")
