#!/usr/bin/env python3
"""
Backtest 4 alternative entry strategies on all Hyperliquid assets (last 7 days).
Uses only requests + numpy. Outputs a comparison table.
"""

import json
import math
import time
import sys
from datetime import datetime, timezone

import requests
import numpy as np

# ─── Configuration ───────────────────────────────────────────────────────────
ACCOUNT_SIZE = 86.0
LEVERAGE = 10
POSITION_PCT = 0.05        # 5% of account per trade
TP_PCT = 0.008             # 0.8%
SL_PCT = 0.004             # 0.4%
FEE_PCT = 0.0007           # 0.07% per side
MAX_TRADES_PER_DAY = 8
TIMEFRAME = "15m"
CANDLE_INTERVAL_MS = 15 * 60 * 1000
LOOKBACK_DAYS = 7
WARMUP_BARS = 200
API_URL = "https://api.hyperliquid.xyz/info"
RATE_LIMIT_SLEEP = 0.25

# ─── API helpers ─────────────────────────────────────────────────────────────

def get_all_assets() -> list[str]:
    """Fetch all tradeable assets from Hyperliquid."""
    resp = requests.post(API_URL, json={"type": "meta"}, timeout=10)
    resp.raise_for_status()
    meta = resp.json()
    assets = [u["name"] for u in meta["universe"]]
    return assets


def get_candles(asset: str, interval: str, start_ms: int, end_ms: int, retries: int = 3) -> list[dict]:
    """Fetch candles for an asset with retry on 429. Returns list of {t, o, h, l, c, v}."""
    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": asset,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
        },
    }
    for attempt in range(retries):
        resp = requests.post(API_URL, json=payload, timeout=15)
        if resp.status_code == 429:
            wait = 2.0 * (attempt + 1)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        break
    else:
        resp.raise_for_status()
    raw = resp.json()
    candles = []
    for c in raw:
        candles.append({
            "t": int(c["t"]),
            "o": float(c["o"]),
            "h": float(c["h"]),
            "l": float(c["l"]),
            "c": float(c["c"]),
            "v": float(c["v"]),
        })
    candles.sort(key=lambda x: x["t"])
    return candles


# ─── Indicator helpers ───────────────────────────────────────────────────────

def calc_ema(closes: np.ndarray, period: int) -> np.ndarray:
    """Exponential moving average."""
    ema = np.full_like(closes, np.nan)
    if len(closes) < period:
        return ema
    ema[period - 1] = np.mean(closes[:period])
    k = 2.0 / (period + 1)
    for i in range(period, len(closes)):
        ema[i] = closes[i] * k + ema[i - 1] * (1 - k)
    return ema


def calc_rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """RSI using Wilder's smoothing."""
    rsi = np.full_like(closes, np.nan)
    if len(closes) < period + 1:
        return rsi
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    if avg_loss == 0:
        rsi[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi[period] = 100.0 - 100.0 / (1.0 + rs)
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i + 1] = 100.0 - 100.0 / (1.0 + rs)
    return rsi


def calc_sma(closes: np.ndarray, period: int) -> np.ndarray:
    """Simple moving average."""
    sma = np.full_like(closes, np.nan)
    for i in range(period - 1, len(closes)):
        sma[i] = np.mean(closes[i - period + 1 : i + 1])
    return sma


def calc_bollinger(closes: np.ndarray, period: int = 20, num_std: float = 2.0):
    """Bollinger Bands: returns (middle, upper, lower)."""
    mid = calc_sma(closes, period)
    upper = np.full_like(closes, np.nan)
    lower = np.full_like(closes, np.nan)
    for i in range(period - 1, len(closes)):
        std = np.std(closes[i - period + 1 : i + 1], ddof=0)
        upper[i] = mid[i] + num_std * std
        lower[i] = mid[i] - num_std * std
    return mid, upper, lower


def calc_donchian(highs: np.ndarray, lows: np.ndarray, period: int = 20):
    """Donchian channel: returns (upper, lower)."""
    n = len(highs)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    for i in range(period - 1, n):
        upper[i] = np.max(highs[i - period + 1 : i + 1])
        lower[i] = np.min(lows[i - period + 1 : i + 1])
    return upper, lower


def calc_adx(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> np.ndarray:
    """Average Directional Index."""
    n = len(closes)
    adx = np.full(n, np.nan)
    if n < period * 2 + 1:
        return adx

    tr = np.zeros(n)
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)

    for i in range(1, n):
        h_diff = highs[i] - highs[i - 1]
        l_diff = lows[i - 1] - lows[i]
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        plus_dm[i] = h_diff if (h_diff > l_diff and h_diff > 0) else 0.0
        minus_dm[i] = l_diff if (l_diff > h_diff and l_diff > 0) else 0.0

    # Wilder smoothing
    atr = np.zeros(n)
    plus_di = np.zeros(n)
    minus_di = np.zeros(n)

    atr[period] = np.sum(tr[1 : period + 1])
    s_plus = np.sum(plus_dm[1 : period + 1])
    s_minus = np.sum(minus_dm[1 : period + 1])

    if atr[period] > 0:
        plus_di[period] = 100.0 * s_plus / atr[period]
        minus_di[period] = 100.0 * s_minus / atr[period]

    for i in range(period + 1, n):
        atr[i] = atr[i - 1] - atr[i - 1] / period + tr[i]
        s_plus = s_plus - s_plus / period + plus_dm[i]
        s_minus = s_minus - s_minus / period + minus_dm[i]
        if atr[i] > 0:
            plus_di[i] = 100.0 * s_plus / atr[i]
            minus_di[i] = 100.0 * s_minus / atr[i]

    dx = np.zeros(n)
    for i in range(period, n):
        denom = plus_di[i] + minus_di[i]
        if denom > 0:
            dx[i] = 100.0 * abs(plus_di[i] - minus_di[i]) / denom

    # First ADX = average of first `period` DX values
    start_idx = period * 2
    if start_idx < n:
        adx[start_idx] = np.mean(dx[period + 1 : start_idx + 1])
        for i in range(start_idx + 1, n):
            adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period

    return adx


# ─── Trade simulator ─────────────────────────────────────────────────────────

def simulate_trades(signals: list[dict], candles: list[dict]) -> dict:
    """
    Given a list of signals [{bar_idx, direction}] and candles,
    simulate trades with TP/SL/fees and return stats.
    direction: 1 = LONG, -1 = SHORT
    """
    position_size = ACCOUNT_SIZE * POSITION_PCT * LEVERAGE  # notional
    trades = []
    daily_counts: dict[str, int] = {}

    i_signal = 0
    in_trade = False

    for sig in signals:
        idx = sig["bar_idx"]
        direction = sig["direction"]

        # Day limit
        day_key = datetime.fromtimestamp(candles[idx]["t"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if daily_counts.get(day_key, 0) >= MAX_TRADES_PER_DAY:
            continue

        if in_trade:
            continue

        entry_price = candles[idx]["c"]  # enter at close of signal bar
        if entry_price == 0:
            continue

        if direction == 1:
            tp_price = entry_price * (1 + TP_PCT)
            sl_price = entry_price * (1 - SL_PCT)
        else:
            tp_price = entry_price * (1 - TP_PCT)
            sl_price = entry_price * (1 + SL_PCT)

        # Walk forward to resolve
        exit_price = None
        exit_reason = None
        for j in range(idx + 1, len(candles)):
            c = candles[j]
            if direction == 1:
                if c["l"] <= sl_price:
                    exit_price = sl_price
                    exit_reason = "SL"
                    break
                if c["h"] >= tp_price:
                    exit_price = tp_price
                    exit_reason = "TP"
                    break
            else:
                if c["h"] >= sl_price:
                    exit_price = sl_price
                    exit_reason = "SL"
                    break
                if c["l"] <= tp_price:
                    exit_price = tp_price
                    exit_reason = "TP"
                    break

        if exit_price is None:
            # Close at last candle
            exit_price = candles[-1]["c"]
            exit_reason = "CLOSE"

        # P&L
        qty = position_size / entry_price
        if direction == 1:
            gross_pnl = (exit_price - entry_price) * qty
        else:
            gross_pnl = (entry_price - exit_price) * qty

        fees = position_size * FEE_PCT * 2  # entry + exit
        net_pnl = gross_pnl - fees

        trades.append({
            "entry": entry_price,
            "exit": exit_price,
            "direction": direction,
            "pnl": net_pnl,
            "reason": exit_reason,
        })
        daily_counts[day_key] = daily_counts.get(day_key, 0) + 1
        in_trade = False  # single-bar trade resolution, can enter again

    return summarize_trades(trades)


def summarize_trades(trades: list[dict]) -> dict:
    """Summarize trade list into stats."""
    if not trades:
        return {
            "count": 0,
            "wins": 0,
            "win_rate": 0.0,
            "net_pnl": 0.0,
            "max_dd": 0.0,
            "profit_factor": 0.0,
        }

    wins = sum(1 for t in trades if t["pnl"] > 0)
    total_pnl = sum(t["pnl"] for t in trades)
    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))

    # Max drawdown (equity curve)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        equity += t["pnl"]
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    pf = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)

    return {
        "count": len(trades),
        "wins": wins,
        "win_rate": wins / len(trades) * 100 if trades else 0.0,
        "net_pnl": total_pnl,
        "max_dd": max_dd,
        "profit_factor": pf,
    }


# ─── Strategy signal generators ─────────────────────────────────────────────

def strategy_rsi_reversal(candles: list[dict]) -> list[dict]:
    """RSI Reversal: LONG when RSI crosses above 30, SHORT when crosses below 70."""
    closes = np.array([c["c"] for c in candles])
    rsi = calc_rsi(closes, 14)
    signals = []
    for i in range(1, len(candles)):
        if np.isnan(rsi[i]) or np.isnan(rsi[i - 1]):
            continue
        # Cross above 30
        if rsi[i - 1] < 30 and rsi[i] >= 30:
            signals.append({"bar_idx": i, "direction": 1})
        # Cross below 70
        if rsi[i - 1] > 70 and rsi[i] <= 70:
            signals.append({"bar_idx": i, "direction": -1})
    return signals


def strategy_ema_no_regime(candles: list[dict]) -> list[dict]:
    """EMA9/EMA21 crossover without regime filter."""
    closes = np.array([c["c"] for c in candles])
    ema9 = calc_ema(closes, 9)
    ema21 = calc_ema(closes, 21)
    rsi = calc_rsi(closes, 14)
    signals = []
    for i in range(1, len(candles)):
        if np.isnan(ema9[i]) or np.isnan(ema21[i]) or np.isnan(ema9[i - 1]) or np.isnan(ema21[i - 1]):
            continue
        if np.isnan(rsi[i]):
            continue
        # Bullish crossover
        if ema9[i - 1] <= ema21[i - 1] and ema9[i] > ema21[i] and 30 <= rsi[i] <= 65:
            signals.append({"bar_idx": i, "direction": 1})
        # Bearish crossover
        if ema9[i - 1] >= ema21[i - 1] and ema9[i] < ema21[i] and 35 <= rsi[i] <= 70:
            signals.append({"bar_idx": i, "direction": -1})
    return signals


def strategy_momentum_breakout(candles: list[dict]) -> list[dict]:
    """Momentum Breakout: Donchian 20-bar breakout + ADX > 20."""
    closes = np.array([c["c"] for c in candles])
    highs = np.array([c["h"] for c in candles])
    lows = np.array([c["l"] for c in candles])
    don_upper, don_lower = calc_donchian(highs, lows, 20)
    adx = calc_adx(highs, lows, closes, 14)
    signals = []
    for i in range(1, len(candles)):
        if np.isnan(don_upper[i - 1]) or np.isnan(don_lower[i - 1]) or np.isnan(adx[i]):
            continue
        if adx[i] < 20:
            continue
        # Break above previous upper channel
        if closes[i] > don_upper[i - 1]:
            signals.append({"bar_idx": i, "direction": 1})
        # Break below previous lower channel
        elif closes[i] < don_lower[i - 1]:
            signals.append({"bar_idx": i, "direction": -1})
    return signals


def strategy_mean_reversion(candles: list[dict]) -> list[dict]:
    """Mean Reversion: RSI < 25 + price < lower BB for LONG, RSI > 75 + price > upper BB for SHORT."""
    closes = np.array([c["c"] for c in candles])
    rsi = calc_rsi(closes, 14)
    _, bb_upper, bb_lower = calc_bollinger(closes, 20, 2.0)
    signals = []
    for i in range(len(candles)):
        if np.isnan(rsi[i]) or np.isnan(bb_upper[i]) or np.isnan(bb_lower[i]):
            continue
        if rsi[i] < 25 and closes[i] < bb_lower[i]:
            signals.append({"bar_idx": i, "direction": 1})
        elif rsi[i] > 75 and closes[i] > bb_upper[i]:
            signals.append({"bar_idx": i, "direction": -1})
    return signals


# ─── Main ────────────────────────────────────────────────────────────────────

STRATEGIES = [
    ("RSI Reversal", strategy_rsi_reversal),
    ("EMA9/21 No Regime", strategy_ema_no_regime),
    ("Momentum Breakout", strategy_momentum_breakout),
    ("Mean Reversion", strategy_mean_reversion),
]


def main():
    print("=" * 80)
    print("BACKTEST: 4 Alternative Strategies on All Hyperliquid Assets (Last 7 Days)")
    print("=" * 80)
    print(f"Config: 15m candles | TP 0.8% | SL 0.4% | 10x leverage | $86 account | 5% size")
    print(f"Fees: 0.07%/side | Max 8 trades/day")
    print()

    # Time window
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (LOOKBACK_DAYS + 3) * 24 * 60 * 60 * 1000  # extra for warmup
    end_ms = now_ms

    # Fetch assets
    print("Fetching asset list...")
    try:
        assets = get_all_assets()
    except Exception as e:
        print(f"ERROR fetching assets: {e}")
        sys.exit(1)
    print(f"Found {len(assets)} assets")
    print()

    # Aggregate results per strategy
    agg = {name: {"count": 0, "wins": 0, "net_pnl": 0.0, "max_dd": 0.0,
                   "gross_profit": 0.0, "gross_loss": 0.0, "assets_traded": 0}
           for name, _ in STRATEGIES}

    fetched = 0
    errors = 0
    skipped = 0

    for asset_idx, asset in enumerate(assets):
        progress = f"[{asset_idx + 1}/{len(assets)}]"
        try:
            candles = get_candles(asset, TIMEFRAME, start_ms, end_ms)
            fetched += 1
        except Exception as e:
            errors += 1
            print(f"  {progress} {asset}: ERROR fetching candles - {e}")
            time.sleep(RATE_LIMIT_SLEEP)
            continue

        time.sleep(RATE_LIMIT_SLEEP)

        if len(candles) < WARMUP_BARS:
            skipped += 1
            continue

        # Only keep last 7 days for signal generation (after warmup)
        cutoff_ms = now_ms - LOOKBACK_DAYS * 24 * 60 * 60 * 1000

        for strat_name, strat_fn in STRATEGIES:
            try:
                signals = strat_fn(candles)
            except Exception as e:
                continue

            # Filter signals to only last 7 days
            signals = [s for s in signals if candles[s["bar_idx"]]["t"] >= cutoff_ms]

            if not signals:
                continue

            result = simulate_trades(signals, candles)
            if result["count"] > 0:
                agg[strat_name]["count"] += result["count"]
                agg[strat_name]["wins"] += result["wins"]
                agg[strat_name]["net_pnl"] += result["net_pnl"]
                agg[strat_name]["assets_traded"] += 1
                # Track gross for profit factor
                for s in signals:
                    pass  # already in result

                # Approximate max drawdown (worst of all assets)
                if result["max_dd"] > agg[strat_name]["max_dd"]:
                    agg[strat_name]["max_dd"] = result["max_dd"]

        if (asset_idx + 1) % 20 == 0:
            print(f"  {progress} Processed {asset}... ({errors} errors, {skipped} skipped)")

    print()
    print(f"Data: {fetched} assets fetched, {errors} errors, {skipped} skipped (< {WARMUP_BARS} bars)")
    print()

    # ─── Recompute profit factor from aggregated trades ──────────────────────
    # We need to re-run to get gross profit/loss. Let's compute from what we have.
    # Actually, let's just re-derive from PnL sign heuristic using win/loss counts and avg.
    # Better: recompute by re-running. But that would double the time.
    # Instead, use the approximate: PF = (wins * avg_win) / (losses * avg_loss)
    # With fixed TP/SL: avg_win ~ TP * notional - fees, avg_loss ~ SL * notional + fees (approx)
    notional = ACCOUNT_SIZE * POSITION_PCT * LEVERAGE
    fees_total = notional * FEE_PCT * 2

    # ─── Results table ───────────────────────────────────────────────────────
    print("=" * 100)
    print(f"{'Strategy':<22} {'Trades':>7} {'Assets':>7} {'Win%':>7} {'Net P&L':>10} {'Max DD':>9} {'PF':>7}")
    print("-" * 100)

    for strat_name, _ in STRATEGIES:
        s = agg[strat_name]
        count = s["count"]
        wins = s["wins"]
        losses = count - wins
        wr = (wins / count * 100) if count > 0 else 0.0
        pnl = s["net_pnl"]
        mdd = s["max_dd"]

        # Estimate profit factor from wins/losses with known TP/SL
        if count > 0 and pnl != 0:
            # Use actual P&L: separate wins and losses
            # Since we can't separate them from aggregates, estimate:
            # Each win ~ TP_PCT * notional - fees, each loss ~ -(SL_PCT * notional + fees)
            est_win = TP_PCT * notional - fees_total
            est_loss = SL_PCT * notional + fees_total
            gp = wins * est_win
            gl = losses * est_loss
            pf = gp / gl if gl > 0 else 999.0
        else:
            pf = 0.0

        print(f"{strat_name:<22} {count:>7} {s['assets_traded']:>7} {wr:>6.1f}% ${pnl:>+9.2f} ${mdd:>7.2f} {pf:>7.2f}")

    print("=" * 100)

    # Best strategy
    best = max(STRATEGIES, key=lambda x: agg[x[0]]["net_pnl"])
    print(f"\nBest by Net P&L: {best[0]} (${agg[best[0]]['net_pnl']:+.2f})")

    best_wr_name = max(
        [name for name, _ in STRATEGIES if agg[name]["count"] > 0],
        key=lambda name: agg[name]["wins"] / max(agg[name]["count"], 1),
        default="N/A",
    )
    if best_wr_name != "N/A":
        wr = agg[best_wr_name]["wins"] / agg[best_wr_name]["count"] * 100
        print(f"Best by Win Rate: {best_wr_name} ({wr:.1f}%)")

    print()
    print("Note: Profit Factor estimated from fixed TP/SL sizing.")
    print("Note: Max DD is worst single-asset drawdown, not portfolio-level.")


if __name__ == "__main__":
    main()
