#!/usr/bin/env python3
"""
Backtest regime detection parameter combinations on Hyperliquid assets.
Tests EMA9/EMA21 crossover + RSI filter strategy with different regime thresholds.

Uses only requests and numpy. No pandas.
"""

import json
import math
import sys
import time
from itertools import product

import numpy as np
import requests

# ===========================================================================
# Configuration
# ===========================================================================

API_URL = "https://api.hyperliquid.xyz/info"
TIMEFRAME = "15m"
INTERVAL_MS = 15 * 60 * 1000
# ~55 hours of 15m data = 220 bars (need 200 for EMA200 warmup)
BARS_NEEDED = 220
LOOKBACK_MS = BARS_NEEDED * INTERVAL_MS

# Strategy parameters (fixed)
RSI_LONG_MIN, RSI_LONG_MAX = 30, 65
RSI_SHORT_MIN, RSI_SHORT_MAX = 35, 70
TP_PCT = 0.8 / 100   # 0.8%
SL_PCT = 0.4 / 100   # 0.4%
FEE_PCT = 0.07 / 100  # 0.07% per side (0.14% round-trip)
MIN_ATR_PCT = 0.1 / 100  # 0.1%
LEVERAGE = 10
POSITION_SIZE_PCT = 0.05  # 5% of account per trade
ACCOUNT_SIZE = 86.0  # $86

# Regime parameter combinations to test
ADX_THRESHOLDS = [18, 20, 22, 25]
EMA200_SLOPE_THRESHOLDS = [0.0003, 0.0005, 0.001]
# Also test "no regime filter" as a baseline
# Choppiness threshold for RANGE detection
CHOPPINESS_THRESHOLDS = [50, 55, 60]

# We test: (adx_threshold, ema_slope_threshold, choppiness_threshold)
# Plus a "NO_FILTER" baseline that trades regardless of regime


# ===========================================================================
# Technical Indicator Functions (matching market_state.py exactly)
# ===========================================================================

def calculate_ema(prices, period):
    alpha = 2.0 / (period + 1)
    ema = np.zeros_like(prices, dtype=float)
    ema[0] = prices[0]
    for i in range(1, len(prices)):
        ema[i] = alpha * prices[i] + (1 - alpha) * ema[i - 1]
    return ema


def calculate_atr(high, low, close, period=14):
    tr = np.maximum(
        high[1:] - low[1:],
        np.maximum(
            np.abs(high[1:] - close[:-1]),
            np.abs(low[1:] - close[:-1])
        )
    )
    atr = np.zeros(len(tr))
    atr[0] = np.mean(tr[:period]) if len(tr) >= period else tr[0]
    alpha = 1.0 / period
    for i in range(1, len(tr)):
        atr[i] = alpha * tr[i] + (1 - alpha) * atr[i - 1]
    return atr


def calculate_rsi(prices, period=14):
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.zeros(len(deltas))
    avg_loss = np.zeros(len(deltas))
    avg_gain[period - 1] = np.mean(gains[:period])
    avg_loss[period - 1] = np.mean(losses[:period])
    for i in range(period, len(deltas)):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gains[i]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + losses[i]) / period
    rs = np.divide(avg_gain, avg_loss, out=np.full_like(avg_gain, 100.0), where=avg_loss != 0)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calculate_adx(high, low, close, period=14):
    up_move = high[1:] - high[:-1]
    down_move = low[:-1] - low[1:]
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)
    atr = calculate_atr(high, low, close, period)
    plus_dm_smooth = np.zeros(len(plus_dm))
    minus_dm_smooth = np.zeros(len(minus_dm))
    plus_dm_smooth[period - 1] = np.sum(plus_dm[:period])
    minus_dm_smooth[period - 1] = np.sum(minus_dm[:period])
    for i in range(period, len(plus_dm)):
        plus_dm_smooth[i] = plus_dm_smooth[i - 1] - (plus_dm_smooth[i - 1] / period) + plus_dm[i]
        minus_dm_smooth[i] = minus_dm_smooth[i - 1] - (minus_dm_smooth[i - 1] / period) + minus_dm[i]
    plus_di = np.where(atr != 0, 100 * plus_dm_smooth / (atr * period), 0)
    minus_di = np.where(atr != 0, 100 * minus_dm_smooth / (atr * period), 0)
    di_sum = plus_di + minus_di
    di_diff = 100 * np.abs(plus_di - minus_di)
    dx = np.divide(di_diff, di_sum, out=np.zeros_like(di_sum), where=di_sum != 0)
    adx = np.zeros(len(dx))
    start_idx = 2 * period - 1
    if start_idx < len(dx):
        adx[start_idx] = np.mean(dx[period:start_idx + 1])
        for i in range(start_idx + 1, len(dx)):
            adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period
    return adx


def calculate_choppiness(high, low, close, period=14):
    atr = calculate_atr(high, low, close, period)
    ci = np.zeros(len(close))
    for i in range(period, len(close)):
        atr_sum = np.sum(atr[i - period + 1:i + 1])
        hl_range = np.max(high[i - period + 1:i + 1]) - np.min(low[i - period + 1:i + 1])
        if hl_range > 0 and atr_sum > 0:
            ci[i] = 100 * math.log10(atr_sum / hl_range) / math.log10(period)
    return ci


# ===========================================================================
# Regime detection
# ===========================================================================

def detect_regime(adx_val, ema200_slope, choppiness_val,
                  adx_thresh, slope_thresh, chop_thresh):
    """Detect regime: TREND / RANGE / CHAOS."""
    if adx_val >= adx_thresh and abs(ema200_slope) >= slope_thresh:
        return "TREND"
    elif adx_val <= (adx_thresh - 5) and choppiness_val >= chop_thresh:
        return "RANGE"
    else:
        return "CHAOS"


# ===========================================================================
# API calls
# ===========================================================================

def get_all_assets():
    """Get all tradable assets from Hyperliquid."""
    resp = requests.post(API_URL, json={"type": "meta"}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return [u["name"] for u in data.get("universe", [])]


def get_candles(symbol, start_ms, end_ms):
    """Fetch candle data for a symbol."""
    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": symbol,
            "interval": TIMEFRAME,
            "startTime": start_ms,
            "endTime": end_ms,
        }
    }
    resp = requests.post(API_URL, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ===========================================================================
# Backtest logic
# ===========================================================================

def compute_indicators(ohlcv):
    """Compute all indicators for one asset. Returns dict of arrays."""
    o, h, l, c = ohlcv["open"], ohlcv["high"], ohlcv["low"], ohlcv["close"]
    n = len(c)
    if n < 200:
        return None

    ema9 = calculate_ema(c, 9)
    ema21 = calculate_ema(c, 21)
    ema200 = calculate_ema(c, 200)
    rsi = calculate_rsi(c, 14)  # length = n-1
    atr = calculate_atr(h, l, c, 14)  # length = n-1
    adx = calculate_adx(h, l, c, 14)  # length = n-1
    chop = calculate_choppiness(h, l, c, 14)

    return {
        "open": o, "high": h, "low": l, "close": c,
        "ema9": ema9, "ema21": ema21, "ema200": ema200,
        "rsi": rsi, "atr": atr, "adx": adx, "choppiness": chop,
    }


def run_backtest_single(indicators, symbol, adx_thresh, slope_thresh, chop_thresh, use_regime_filter):
    """
    Run backtest for one asset with one parameter combo.
    Returns list of trade dicts.
    """
    c = indicators["close"]
    h = indicators["high"]
    l = indicators["low"]
    n = len(c)

    ema9 = indicators["ema9"]
    ema21 = indicators["ema21"]
    ema200 = indicators["ema200"]
    rsi = indicators["rsi"]      # length n-1, index i corresponds to diff at position i
    atr = indicators["atr"]      # length n-1
    adx = indicators["adx"]      # length n-1
    chop = indicators["choppiness"]

    trades = []
    in_trade = False
    trade_dir = None
    entry_price = 0.0
    tp_price = 0.0
    sl_price = 0.0

    # Start from bar 200 to ensure all indicators are warmed up
    # RSI/ATR/ADX arrays are 1 shorter than close, so index i in those = close index i+1
    # Actually: rsi has length n-1, rsi[i] corresponds to the change from close[i] to close[i+1]
    # But the standard RSI at bar k uses rsi array index k-1
    start_bar = 200

    for bar in range(start_bar, n):
        price = c[bar]

        # Check if current trade hits TP/SL using this bar's high/low
        if in_trade:
            if trade_dir == "LONG":
                if h[bar] >= tp_price:
                    pnl = (tp_price / entry_price - 1) * LEVERAGE - 2 * FEE_PCT * LEVERAGE
                    trades.append({"symbol": symbol, "dir": "LONG", "entry": entry_price,
                                   "exit": tp_price, "pnl_pct": pnl, "result": "TP"})
                    in_trade = False
                elif l[bar] <= sl_price:
                    pnl = (sl_price / entry_price - 1) * LEVERAGE - 2 * FEE_PCT * LEVERAGE
                    trades.append({"symbol": symbol, "dir": "LONG", "entry": entry_price,
                                   "exit": sl_price, "pnl_pct": pnl, "result": "SL"})
                    in_trade = False
            else:  # SHORT
                if l[bar] <= tp_price:
                    pnl = (1 - tp_price / entry_price) * LEVERAGE - 2 * FEE_PCT * LEVERAGE
                    trades.append({"symbol": symbol, "dir": "SHORT", "entry": entry_price,
                                   "exit": tp_price, "pnl_pct": pnl, "result": "TP"})
                    in_trade = False
                elif h[bar] >= sl_price:
                    pnl = (1 - sl_price / entry_price) * LEVERAGE - 2 * FEE_PCT * LEVERAGE
                    trades.append({"symbol": symbol, "dir": "SHORT", "entry": entry_price,
                                   "exit": sl_price, "pnl_pct": pnl, "result": "SL"})
                    in_trade = False

        # If not in trade, check for entry
        if not in_trade:
            # Indicator indices: rsi/atr/adx are offset by 1 (length n-1)
            # For bar k in close[], use index k-1 in rsi/atr/adx
            idx = bar - 1
            if idx < 0 or idx >= len(rsi):
                continue

            rsi_val = rsi[idx]
            atr_val = atr[idx]
            adx_val = adx[idx]
            chop_val = chop[bar]

            # ATR filter
            atr_pct = atr_val / price if price > 0 else 0
            if atr_pct < MIN_ATR_PCT:
                continue

            # EMA200 slope (last 5 bars)
            if bar >= 5:
                ema200_slope = (ema200[bar] - ema200[bar - 5]) / ema200[bar - 5] if ema200[bar - 5] != 0 else 0
            else:
                ema200_slope = 0

            # Regime filter
            if use_regime_filter:
                regime = detect_regime(adx_val, ema200_slope, chop_val,
                                       adx_thresh, slope_thresh, chop_thresh)
                if regime != "TREND":
                    continue

            # EMA crossover direction
            if ema9[bar] > ema21[bar]:
                direction = "LONG"
            elif ema9[bar] < ema21[bar]:
                direction = "SHORT"
            else:
                continue

            # RSI filter
            if direction == "LONG":
                if not (RSI_LONG_MIN <= rsi_val <= RSI_LONG_MAX):
                    continue
            else:
                if not (RSI_SHORT_MIN <= rsi_val <= RSI_SHORT_MAX):
                    continue

            # Enter trade at close of this bar
            entry_price = price
            in_trade = True
            trade_dir = direction

            if direction == "LONG":
                tp_price = entry_price * (1 + TP_PCT)
                sl_price = entry_price * (1 - SL_PCT)
            else:
                tp_price = entry_price * (1 - TP_PCT)
                sl_price = entry_price * (1 + SL_PCT)

    # Close any open trade at last price
    if in_trade:
        if trade_dir == "LONG":
            pnl = (c[-1] / entry_price - 1) * LEVERAGE - 2 * FEE_PCT * LEVERAGE
        else:
            pnl = (1 - c[-1] / entry_price) * LEVERAGE - 2 * FEE_PCT * LEVERAGE
        trades.append({"symbol": symbol, "dir": trade_dir, "entry": entry_price,
                       "exit": c[-1], "pnl_pct": pnl, "result": "OPEN"})

    return trades


def aggregate_trades(all_trades):
    """Compute aggregate stats from a list of trades."""
    if not all_trades:
        return {
            "trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "total_pnl_pct": 0.0, "avg_pnl_pct": 0.0, "max_dd_pct": 0.0,
            "net_pnl_usd": 0.0, "sharpe": 0.0, "profit_factor": 0.0,
            "unique_assets": 0,
        }

    n = len(all_trades)
    wins = sum(1 for t in all_trades if t["pnl_pct"] > 0)
    losses = n - wins
    pnls = [t["pnl_pct"] for t in all_trades]
    total_pnl = sum(pnls)
    avg_pnl = total_pnl / n if n > 0 else 0

    # Max drawdown on cumulative curve
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cumulative += p
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    # Sharpe (annualized, assuming ~96 trades per day at 15m)
    if len(pnls) > 1:
        std = float(np.std(pnls))
        sharpe = (avg_pnl / std) * math.sqrt(96 * 365) if std > 0 else 0
    else:
        sharpe = 0

    # Profit factor
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf') if gross_profit > 0 else 0

    # Approximate net P&L in USD
    # Each trade uses 5% of account * leverage
    trade_size = ACCOUNT_SIZE * POSITION_SIZE_PCT * LEVERAGE
    net_pnl_usd = sum(t["pnl_pct"] * trade_size for t in all_trades)

    unique_assets = len(set(t["symbol"] for t in all_trades))

    return {
        "trades": n, "wins": wins, "losses": losses,
        "win_rate": wins / n * 100 if n > 0 else 0,
        "total_pnl_pct": total_pnl * 100,
        "avg_pnl_pct": avg_pnl * 100,
        "max_dd_pct": max_dd * 100,
        "net_pnl_usd": net_pnl_usd,
        "sharpe": sharpe,
        "profit_factor": profit_factor,
        "unique_assets": unique_assets,
    }


# ===========================================================================
# Main
# ===========================================================================

def main():
    print("=" * 80)
    print("REGIME DETECTION PARAMETER BACKTEST")
    print("=" * 80)
    print(f"Strategy: EMA9/EMA21 crossover + RSI filter")
    print(f"Timeframe: {TIMEFRAME}, Lookback: ~{LOOKBACK_MS / 3600000:.0f}h ({BARS_NEEDED} bars)")
    print(f"TP: {TP_PCT*100:.1f}%, SL: {SL_PCT*100:.1f}%, Fee: {FEE_PCT*100:.3f}%/side, Leverage: {LEVERAGE}x")
    print(f"Account: ${ACCOUNT_SIZE}, Position: {POSITION_SIZE_PCT*100:.0f}%")
    print()

    # 1) Get all assets
    print("Fetching asset list...")
    assets = get_all_assets()
    print(f"Found {len(assets)} assets")

    # 2) Fetch candle data for all assets
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - LOOKBACK_MS

    asset_data = {}
    skipped = 0
    print(f"Fetching candle data for {len(assets)} assets...")
    for i, symbol in enumerate(assets):
        try:
            candles = get_candles(symbol, start_ms, now_ms)
            if not candles or len(candles) < 200:
                skipped += 1
                continue
            opens = np.array([float(c["o"]) for c in candles])
            highs = np.array([float(c["h"]) for c in candles])
            lows = np.array([float(c["l"]) for c in candles])
            closes = np.array([float(c["c"]) for c in candles])
            asset_data[symbol] = {"open": opens, "high": highs, "low": lows, "close": closes}
        except Exception as e:
            skipped += 1
        time.sleep(0.08)  # rate limit
        if (i + 1) % 50 == 0:
            print(f"  ... fetched {i+1}/{len(assets)} assets ({len(asset_data)} valid)")

    print(f"Fetched data for {len(asset_data)} assets (skipped {skipped})")
    print()

    # 3) Compute indicators for all assets
    print("Computing indicators...")
    asset_indicators = {}
    for symbol, ohlcv in asset_data.items():
        ind = compute_indicators(ohlcv)
        if ind is not None:
            asset_indicators[symbol] = ind
    print(f"Indicators computed for {len(asset_indicators)} assets")
    print()

    # 4) Run backtests for each parameter combo
    combos = []

    # Add "NO FILTER" baseline
    combos.append(("NO_FILTER", None, None, None, False))

    # Add all regime filter combos
    for adx_t, slope_t, chop_t in product(ADX_THRESHOLDS, EMA200_SLOPE_THRESHOLDS, CHOPPINESS_THRESHOLDS):
        label = f"ADX>={adx_t} Slope>={slope_t} Chop>={chop_t}"
        combos.append((label, adx_t, slope_t, chop_t, True))

    print(f"Testing {len(combos)} parameter combinations across {len(asset_indicators)} assets...")
    print()

    results = []
    for combo_idx, (label, adx_t, slope_t, chop_t, use_filter) in enumerate(combos):
        all_trades = []
        for symbol, ind in asset_indicators.items():
            trades = run_backtest_single(ind, symbol, adx_t, slope_t, chop_t, use_filter)
            all_trades.extend(trades)

        stats = aggregate_trades(all_trades)
        stats["label"] = label
        results.append(stats)

        if (combo_idx + 1) % 10 == 0:
            print(f"  ... tested {combo_idx+1}/{len(combos)} combos")

    # 5) Sort by Sharpe ratio (risk-adjusted returns)
    results.sort(key=lambda x: x["sharpe"], reverse=True)

    # 6) Print results table
    print()
    print("=" * 140)
    print(f"{'Rank':<5} {'Parameters':<38} {'Trades':>7} {'Assets':>7} {'WinRate':>8} {'TotPnL%':>9} {'AvgPnL%':>9} "
          f"{'MaxDD%':>8} {'PnL$':>8} {'Sharpe':>8} {'ProfFact':>9}")
    print("-" * 140)

    for rank, r in enumerate(results, 1):
        pf_str = f"{r['profit_factor']:.2f}" if r['profit_factor'] != float('inf') else "inf"
        print(f"{rank:<5} {r['label']:<38} {r['trades']:>7} {r['unique_assets']:>7} "
              f"{r['win_rate']:>7.1f}% {r['total_pnl_pct']:>+8.2f}% {r['avg_pnl_pct']:>+8.3f}% "
              f"{r['max_dd_pct']:>7.2f}% {r['net_pnl_usd']:>+7.2f} {r['sharpe']:>8.2f} {pf_str:>9}")

    print("=" * 140)

    # 7) Highlight best
    if results:
        best = results[0]
        print()
        print("BEST BY SHARPE RATIO:")
        print(f"  Parameters: {best['label']}")
        print(f"  Trades: {best['trades']}, Win Rate: {best['win_rate']:.1f}%")
        print(f"  Total P&L: {best['total_pnl_pct']:+.2f}% (${best['net_pnl_usd']:+.2f})")
        print(f"  Max Drawdown: {best['max_dd_pct']:.2f}%")
        print(f"  Sharpe: {best['sharpe']:.2f}, Profit Factor: {best['profit_factor']:.2f}")

        # Find best by net P&L
        best_pnl = max(results, key=lambda x: x["net_pnl_usd"])
        if best_pnl["label"] != best["label"]:
            print()
            print("BEST BY NET P&L:")
            print(f"  Parameters: {best_pnl['label']}")
            print(f"  Trades: {best_pnl['trades']}, Win Rate: {best_pnl['win_rate']:.1f}%")
            print(f"  Total P&L: {best_pnl['total_pnl_pct']:+.2f}% (${best_pnl['net_pnl_usd']:+.2f})")
            print(f"  Max Drawdown: {best_pnl['max_dd_pct']:.2f}%")
            print(f"  Sharpe: {best_pnl['sharpe']:.2f}, Profit Factor: {best_pnl['profit_factor']:.2f}")

        # Show current config for reference
        current = [r for r in results if r["label"] == "ADX>=25 Slope>=0.001 Chop>=60"]
        if current:
            curr = current[0]
            curr_rank = results.index(curr) + 1
            print()
            print(f"CURRENT CONFIG (rank #{curr_rank}/{len(results)}):")
            print(f"  Parameters: {curr['label']}")
            print(f"  Trades: {curr['trades']}, Win Rate: {curr['win_rate']:.1f}%")
            print(f"  Total P&L: {curr['total_pnl_pct']:+.2f}% (${curr['net_pnl_usd']:+.2f})")
            print(f"  Sharpe: {curr['sharpe']:.2f}, Profit Factor: {curr['profit_factor']:.2f}")

    print()
    print("Done.")


if __name__ == "__main__":
    main()
