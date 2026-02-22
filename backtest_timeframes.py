#!/usr/bin/env python3
"""
Timeframe Backtest: 5m vs 15m vs 1h across all Hyperliquid assets
=================================================================

Tests the EMA9/EMA21 crossover + RSI filter strategy on multiple timeframes
using 7 days of historical data from the Hyperliquid public API.

Strategy: EMA9 crosses EMA21 + RSI filter (30-65 long, 35-70 short)
Regime filter: ADX >= 25 AND |EMA200_slope| >= 0.001
TP: 0.8%, SL: 0.4%
Fees: 0.07% per side (0.14% round trip)
Max 8 trades per day
"""

import json
import math
import time
import sys
import requests
import numpy as np
from datetime import datetime, timezone, timedelta


# ============================================================================
# Configuration
# ============================================================================

TIMEFRAMES = ["5m", "15m", "1h"]
TP_PCT = 0.008       # 0.8%
SL_PCT = 0.004       # 0.4%
FEE_PER_SIDE = 0.0007  # 0.07%
MAX_TRADES_PER_DAY = 8
LEVERAGE = 10
POSITION_SIZE_PCT = 0.05  # 5% of account
ACCOUNT_BALANCE = 86.0    # USD

# RSI filters
RSI_LONG_MIN, RSI_LONG_MAX = 30, 65
RSI_SHORT_MIN, RSI_SHORT_MAX = 35, 70

# Regime filter
ADX_TREND_MIN = 25.0
EMA200_SLOPE_MIN = 0.001

# API
API_URL = "https://api.hyperliquid.xyz/info"
RATE_LIMIT_SLEEP = 0.08

# Timeframe configs: how many days to fetch for 200-bar warmup + 7 days test
TF_CONFIG = {
    "5m":  {"interval_ms": 5 * 60 * 1000,   "fetch_days": 9},
    "15m": {"interval_ms": 15 * 60 * 1000,  "fetch_days": 12},
    "1h":  {"interval_ms": 60 * 60 * 1000,  "fetch_days": 17},
}


# ============================================================================
# Technical Indicators (matching market_state.py exactly)
# ============================================================================

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
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
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
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    atr = calculate_atr(high, low, close, period)
    plus_dm_smooth = np.zeros(len(plus_dm))
    minus_dm_smooth = np.zeros(len(minus_dm))
    if period - 1 < len(plus_dm):
        plus_dm_smooth[period - 1] = np.sum(plus_dm[:period])
        minus_dm_smooth[period - 1] = np.sum(minus_dm[:period])
    for i in range(period, len(plus_dm)):
        plus_dm_smooth[i] = plus_dm_smooth[i - 1] - (plus_dm_smooth[i - 1] / period) + plus_dm[i]
        minus_dm_smooth[i] = minus_dm_smooth[i - 1] - (minus_dm_smooth[i - 1] / period) + minus_dm[i]
    plus_di = np.where(atr != 0, 100 * plus_dm_smooth / (atr * period), 0.0)
    minus_di = np.where(atr != 0, 100 * minus_dm_smooth / (atr * period), 0.0)
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


# ============================================================================
# API Functions
# ============================================================================

def fetch_assets():
    """Get list of all tradeable assets from Hyperliquid."""
    resp = requests.post(API_URL, json={"type": "meta"}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    assets = []
    for item in data.get("universe", []):
        assets.append(item["name"])
    return assets


def fetch_candles(symbol, interval, start_ms, end_ms):
    """Fetch candle data from Hyperliquid API."""
    resp = requests.post(API_URL, json={
        "type": "candleSnapshot",
        "req": {
            "coin": symbol,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
        }
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ============================================================================
# Backtest Engine
# ============================================================================

def backtest_asset_timeframe(symbol, candles, interval, interval_ms, test_start_ms):
    """
    Run backtest for one asset on one timeframe.
    Returns dict with trade stats or None if insufficient data.
    """
    if len(candles) < 210:
        return None

    # Parse OHLCV
    timestamps = []
    opens = []
    highs = []
    lows = []
    closes = []
    for c in candles:
        timestamps.append(int(c.get("t", 0)))
        opens.append(float(c.get("o", 0)))
        highs.append(float(c.get("h", 0)))
        lows.append(float(c.get("l", 0)))
        closes.append(float(c.get("c", 0)))

    ts = np.array(timestamps)
    o = np.array(opens)
    h = np.array(highs)
    l = np.array(lows)
    c = np.array(closes)

    if len(c) < 210:
        return None

    # Calculate indicators on full dataset
    ema9 = calculate_ema(c, 9)
    ema21 = calculate_ema(c, 21)
    ema200 = calculate_ema(c, 200)
    rsi = calculate_rsi(c, 14)
    adx = calculate_adx(h, l, c, 14)
    atr = calculate_atr(h, l, c, 14)

    # Note: RSI has len(c)-1 elements, ADX has len(c)-1 elements, ATR has len(c)-1 elements
    # We need to align indices carefully
    # RSI[i] corresponds to close[i+1] (since it uses diff)
    # ADX[i] corresponds to the i-th element after the first bar (same as ATR)
    # For bar index `b` in the original arrays:
    #   ema9[b], ema21[b], ema200[b] -> direct
    #   rsi[b-1] -> RSI value at bar b (rsi is len c-1, rsi[0] = diff between c[0] and c[1])
    #   adx[b-1] -> ADX value at bar b
    #   atr[b-1] -> ATR value at bar b

    # Find start of test window (first bar at or after test_start_ms)
    test_start_idx = None
    for i in range(len(ts)):
        if ts[i] >= test_start_ms:
            test_start_idx = i
            break

    if test_start_idx is None or test_start_idx < 201:
        # Not enough warmup before test window
        test_start_idx = max(201, test_start_idx or 201)

    if test_start_idx >= len(c):
        return None

    # Track trades
    trades = []
    in_position = False
    position_dir = None  # "long" or "short"
    entry_price = 0.0
    entry_bar = 0
    entry_ts = 0
    daily_trade_count = {}  # date_str -> count

    for b in range(test_start_idx, len(c)):
        # Check if we can resolve open position with this bar's high/low
        if in_position:
            if position_dir == "long":
                tp_price = entry_price * (1 + TP_PCT)
                sl_price = entry_price * (1 - SL_PCT)
                # Check SL first (conservative)
                if l[b] <= sl_price:
                    pnl = -SL_PCT - 2 * FEE_PER_SIDE
                    duration_bars = b - entry_bar
                    trades.append({
                        "dir": "long", "entry": entry_price,
                        "exit": sl_price, "pnl_pct": pnl,
                        "result": "SL", "duration": duration_bars,
                        "entry_ts": entry_ts, "exit_ts": ts[b],
                    })
                    in_position = False
                elif h[b] >= tp_price:
                    pnl = TP_PCT - 2 * FEE_PER_SIDE
                    duration_bars = b - entry_bar
                    trades.append({
                        "dir": "long", "entry": entry_price,
                        "exit": tp_price, "pnl_pct": pnl,
                        "result": "TP", "duration": duration_bars,
                        "entry_ts": entry_ts, "exit_ts": ts[b],
                    })
                    in_position = False
            else:  # short
                tp_price = entry_price * (1 - TP_PCT)
                sl_price = entry_price * (1 + SL_PCT)
                if h[b] >= sl_price:
                    pnl = -SL_PCT - 2 * FEE_PER_SIDE
                    duration_bars = b - entry_bar
                    trades.append({
                        "dir": "short", "entry": entry_price,
                        "exit": sl_price, "pnl_pct": pnl,
                        "result": "SL", "duration": duration_bars,
                        "entry_ts": entry_ts, "exit_ts": ts[b],
                    })
                    in_position = False
                elif l[b] <= tp_price:
                    pnl = TP_PCT - 2 * FEE_PER_SIDE
                    duration_bars = b - entry_bar
                    trades.append({
                        "dir": "short", "entry": entry_price,
                        "exit": tp_price, "pnl_pct": pnl,
                        "result": "TP", "duration": duration_bars,
                        "entry_ts": entry_ts, "exit_ts": ts[b],
                    })
                    in_position = False
            continue  # Don't open new position on same bar we're managing one

        if in_position:
            continue

        # Check daily trade limit
        bar_date = datetime.fromtimestamp(ts[b] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if daily_trade_count.get(bar_date, 0) >= MAX_TRADES_PER_DAY:
            continue

        # Indicator indices (shifted by 1 due to diff-based calculations)
        ri = b - 1  # index into rsi/adx/atr arrays
        if ri < 0 or ri >= len(rsi) or ri >= len(adx) or ri >= len(atr):
            continue

        # Get indicator values
        cur_ema9 = ema9[b]
        cur_ema21 = ema21[b]
        cur_ema200 = ema200[b]
        cur_rsi = rsi[ri]
        cur_adx = max(0, min(100, adx[ri]))
        cur_atr = atr[ri]
        cur_price = c[b]

        # Skip if price is zero or very small
        if cur_price <= 0:
            continue

        # ATR % filter (min 0.1%)
        atr_pct = (cur_atr / cur_price) * 100 if cur_price > 0 else 0
        if atr_pct < 0.1:
            continue

        # EMA200 slope (use 5-bar lookback)
        if b >= 5:
            ema200_slope = (ema200[b] - ema200[b - 5]) / ema200[b - 5] if ema200[b - 5] != 0 else 0
        else:
            ema200_slope = 0

        # Regime filter: ADX >= 25 AND |EMA200_slope| >= 0.001
        # Note: The actual bot strategy (momentum_scalper) does NOT use regime filter
        # (can_trade returns True always). But the task specifies regime filter.
        # We include it as specified in the task.
        if cur_adx < ADX_TREND_MIN or abs(ema200_slope) < EMA200_SLOPE_MIN:
            continue

        # Direction from EMA crossover
        if cur_ema9 > cur_ema21:
            direction = "long"
        elif cur_ema9 < cur_ema21:
            direction = "short"
        else:
            continue

        # RSI filter
        if direction == "long":
            if not (RSI_LONG_MIN <= cur_rsi <= RSI_LONG_MAX):
                continue
        else:
            if not (RSI_SHORT_MIN <= cur_rsi <= RSI_SHORT_MAX):
                continue

        # ENTRY
        entry_price = c[b]
        entry_bar = b
        entry_ts = ts[b]
        in_position = True
        position_dir = direction
        daily_trade_count[bar_date] = daily_trade_count.get(bar_date, 0) + 1

    # Close any remaining open position at last close
    if in_position:
        if position_dir == "long":
            pnl = (c[-1] - entry_price) / entry_price - 2 * FEE_PER_SIDE
        else:
            pnl = (entry_price - c[-1]) / entry_price - 2 * FEE_PER_SIDE
        duration_bars = len(c) - 1 - entry_bar
        trades.append({
            "dir": position_dir, "entry": entry_price,
            "exit": c[-1], "pnl_pct": pnl,
            "result": "OPEN", "duration": duration_bars,
            "entry_ts": entry_ts, "exit_ts": ts[-1],
        })

    if not trades:
        return {
            "symbol": symbol,
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "net_pnl_pct": 0.0,
            "net_pnl_usd": 0.0,
            "max_dd_pct": 0.0,
            "avg_duration": 0.0,
            "long_trades": 0,
            "short_trades": 0,
        }

    # Calculate stats
    wins = sum(1 for t in trades if t["pnl_pct"] > 0)
    losses = sum(1 for t in trades if t["pnl_pct"] <= 0)
    long_trades = sum(1 for t in trades if t["dir"] == "long")
    short_trades = sum(1 for t in trades if t["dir"] == "short")

    # Net PnL (each trade uses POSITION_SIZE_PCT of account with LEVERAGE)
    position_value = ACCOUNT_BALANCE * POSITION_SIZE_PCT * LEVERAGE
    net_pnl_usd = sum(t["pnl_pct"] * position_value for t in trades)
    net_pnl_pct = sum(t["pnl_pct"] for t in trades) * 100  # total % return

    # Max drawdown (cumulative)
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        cumulative += t["pnl_pct"] * position_value
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    max_dd_pct = (max_dd / ACCOUNT_BALANCE) * 100 if ACCOUNT_BALANCE > 0 else 0

    # Average duration in bars
    avg_duration = np.mean([t["duration"] for t in trades])

    return {
        "symbol": symbol,
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / len(trades)) * 100 if trades else 0,
        "net_pnl_pct": net_pnl_pct,
        "net_pnl_usd": net_pnl_usd,
        "max_dd_pct": max_dd_pct,
        "avg_duration": avg_duration,
        "long_trades": long_trades,
        "short_trades": short_trades,
    }


# ============================================================================
# Main
# ============================================================================

def main():
    print("=" * 80)
    print("TIMEFRAME BACKTEST: 5m vs 15m vs 1h")
    print("Strategy: EMA9/EMA21 crossover + RSI filter + Regime (ADX>=25)")
    print(f"TP: {TP_PCT*100}% | SL: {SL_PCT*100}% | Fees: {FEE_PER_SIDE*100}%/side")
    print(f"Account: ${ACCOUNT_BALANCE} | Leverage: {LEVERAGE}x | Size: {POSITION_SIZE_PCT*100}%")
    print(f"Max trades/day: {MAX_TRADES_PER_DAY}")
    print("=" * 80)
    print()

    # Fetch all assets
    print("Fetching asset list...")
    try:
        all_assets = fetch_assets()
    except Exception as e:
        print(f"ERROR: Failed to fetch assets: {e}")
        sys.exit(1)

    print(f"Found {len(all_assets)} assets")
    print()

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    test_start = datetime.now(timezone.utc) - timedelta(days=7)
    test_start_ms = int(test_start.timestamp() * 1000)

    # Results storage: timeframe -> list of asset results
    results = {tf: [] for tf in TIMEFRAMES}

    # Process each timeframe
    for tf in TIMEFRAMES:
        cfg = TF_CONFIG[tf]
        fetch_days = cfg["fetch_days"]
        start_ms = now_ms - (fetch_days * 24 * 60 * 60 * 1000)

        print(f"--- Timeframe: {tf} (fetching {fetch_days} days of data) ---")
        fetched = 0
        skipped = 0
        errors = 0

        for i, symbol in enumerate(all_assets):
            try:
                candles = fetch_candles(symbol, tf, start_ms, now_ms)
                time.sleep(RATE_LIMIT_SLEEP)

                if not candles or len(candles) < 210:
                    skipped += 1
                    continue

                result = backtest_asset_timeframe(
                    symbol, candles, tf, cfg["interval_ms"], test_start_ms
                )
                if result is not None:
                    results[tf].append(result)
                    fetched += 1
                else:
                    skipped += 1

            except Exception as e:
                errors += 1
                # Don't spam output for individual errors
                if errors <= 3:
                    print(f"  Error on {symbol}: {e}")

            # Progress every 50 assets
            if (i + 1) % 50 == 0:
                print(f"  Progress: {i+1}/{len(all_assets)} assets "
                      f"({fetched} ok, {skipped} skipped, {errors} errors)")

        print(f"  Done: {fetched} assets backtested, {skipped} skipped, {errors} errors")
        print()

    # ========================================================================
    # Aggregate Results
    # ========================================================================

    print()
    print("=" * 100)
    print("AGGREGATE RESULTS BY TIMEFRAME")
    print("=" * 100)
    print()

    header = f"{'Timeframe':<10} {'Assets':<8} {'Trades':<8} {'Wins':<7} {'Losses':<8} "
    header += f"{'WinRate%':<9} {'NetPnL$':<10} {'NetPnL%':<10} {'MaxDD%':<9} {'AvgDur':<8} {'L/S':<10}"
    print(header)
    print("-" * 100)

    tf_summary = {}

    for tf in TIMEFRAMES:
        asset_results = results[tf]
        if not asset_results:
            print(f"{tf:<10} {'No data'}")
            continue

        total_trades = sum(r["trades"] for r in asset_results)
        total_wins = sum(r["wins"] for r in asset_results)
        total_losses = sum(r["losses"] for r in asset_results)
        total_pnl_usd = sum(r["net_pnl_usd"] for r in asset_results)
        total_pnl_pct = sum(r["net_pnl_pct"] for r in asset_results)
        max_dd = max((r["max_dd_pct"] for r in asset_results), default=0)
        total_long = sum(r["long_trades"] for r in asset_results)
        total_short = sum(r["short_trades"] for r in asset_results)

        assets_with_trades = sum(1 for r in asset_results if r["trades"] > 0)
        win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0

        # Average duration across all assets that had trades
        durations = [r["avg_duration"] for r in asset_results if r["trades"] > 0 and r["avg_duration"] > 0]
        avg_dur = np.mean(durations) if durations else 0

        tf_summary[tf] = {
            "assets": len(asset_results),
            "assets_with_trades": assets_with_trades,
            "total_trades": total_trades,
            "total_wins": total_wins,
            "total_losses": total_losses,
            "win_rate": win_rate,
            "total_pnl_usd": total_pnl_usd,
            "total_pnl_pct": total_pnl_pct,
            "max_dd": max_dd,
            "avg_dur": avg_dur,
            "total_long": total_long,
            "total_short": total_short,
        }

        print(f"{tf:<10} {assets_with_trades:<8} {total_trades:<8} {total_wins:<7} {total_losses:<8} "
              f"{win_rate:<9.1f} {total_pnl_usd:<10.2f} {total_pnl_pct:<10.2f} {max_dd:<9.2f} "
              f"{avg_dur:<8.1f} {total_long}/{total_short}")

    print()
    print()

    # ========================================================================
    # Top 10 assets per timeframe
    # ========================================================================

    for tf in TIMEFRAMES:
        asset_results = results[tf]
        # Sort by net PnL USD descending
        with_trades = [r for r in asset_results if r["trades"] > 0]
        with_trades.sort(key=lambda x: x["net_pnl_usd"], reverse=True)

        print(f"--- TOP 10 ASSETS ({tf}) ---")
        top_header = f"{'Rank':<6} {'Symbol':<10} {'Trades':<8} {'Wins':<6} {'WR%':<8} {'PnL$':<10} {'PnL%':<10} {'MaxDD%':<9} {'AvgDur':<8} {'L/S':<8}"
        print(top_header)
        print("-" * 90)

        for rank, r in enumerate(with_trades[:10], 1):
            wr = (r["wins"] / r["trades"] * 100) if r["trades"] > 0 else 0
            print(f"{rank:<6} {r['symbol']:<10} {r['trades']:<8} {r['wins']:<6} {wr:<8.1f} "
                  f"{r['net_pnl_usd']:<10.2f} {r['net_pnl_pct']:<10.2f} {r['max_dd_pct']:<9.2f} "
                  f"{r['avg_duration']:<8.1f} {r['long_trades']}/{r['short_trades']}")

        if with_trades:
            print()
            # Bottom 5
            print(f"--- BOTTOM 5 ASSETS ({tf}) ---")
            for rank, r in enumerate(with_trades[-5:], 1):
                wr = (r["wins"] / r["trades"] * 100) if r["trades"] > 0 else 0
                print(f"{rank:<6} {r['symbol']:<10} {r['trades']:<8} {r['wins']:<6} {wr:<8.1f} "
                      f"{r['net_pnl_usd']:<10.2f} {r['net_pnl_pct']:<10.2f} {r['max_dd_pct']:<9.2f} "
                      f"{r['avg_duration']:<8.1f} {r['long_trades']}/{r['short_trades']}")

        print()

    # ========================================================================
    # Final Recommendation
    # ========================================================================

    print("=" * 80)
    print("RECOMMENDATION")
    print("=" * 80)

    if tf_summary:
        best_tf = max(tf_summary.items(), key=lambda x: x[1]["total_pnl_usd"])
        best_wr = max(tf_summary.items(), key=lambda x: x[1]["win_rate"])
        most_trades = max(tf_summary.items(), key=lambda x: x[1]["total_trades"])

        print(f"Best PnL:      {best_tf[0]} (${best_tf[1]['total_pnl_usd']:.2f})")
        print(f"Best Win Rate: {best_wr[0]} ({best_wr[1]['win_rate']:.1f}%)")
        print(f"Most Trades:   {most_trades[0]} ({most_trades[1]['total_trades']} trades)")
        print()

        for tf in TIMEFRAMES:
            if tf in tf_summary:
                s = tf_summary[tf]
                profit_factor = "N/A"
                if s["total_losses"] > 0 and s["total_wins"] > 0:
                    avg_win = s["total_pnl_usd"] / s["total_wins"] if s["total_pnl_usd"] > 0 else 0
                    # Rough profit factor from wins/losses count * TP/SL ratio
                    gross_wins = s["total_wins"] * (TP_PCT - 2 * FEE_PER_SIDE)
                    gross_losses = s["total_losses"] * (SL_PCT + 2 * FEE_PER_SIDE)
                    if gross_losses > 0:
                        profit_factor = f"{gross_wins / gross_losses:.2f}"

                print(f"{tf}: PnL=${s['total_pnl_usd']:.2f} | WR={s['win_rate']:.1f}% | "
                      f"Trades={s['total_trades']} | MaxDD={s['max_dd']:.2f}% | PF={profit_factor}")

    print()
    print("Done.")


if __name__ == "__main__":
    main()
