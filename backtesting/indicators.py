"""Canonical indicator implementations (NaN-padded, full-length arrays).

Source of truth: backtest_sizing.py (NaN-seeded EMA with SMA warmup).
All functions return np.ndarray of same length as input closes.
"""

from __future__ import annotations

import numpy as np

from backtesting.config import BacktestConfig


# ── EMA ──────────────────────────────────────────────────────────────────────

def calc_ema(closes: np.ndarray, period: int) -> np.ndarray:
    """EMA with SMA-seeded warmup, NaN for pre-warmup bars."""
    ema = np.full_like(closes, np.nan)
    if len(closes) < period:
        return ema
    ema[period - 1] = np.mean(closes[:period])
    k = 2.0 / (period + 1)
    for i in range(period, len(closes)):
        ema[i] = closes[i] * k + ema[i - 1] * (1 - k)
    return ema


# ── RSI (Wilder's smoothing) ────────────────────────────────────────────────

def calc_rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """RSI using Wilder's smoothing. Returns full-length array, NaN-padded."""
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
        rsi[period] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi[i + 1] = 100.0
        else:
            rsi[i + 1] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return rsi


# ── ADX ──────────────────────────────────────────────────────────────────────

def calc_adx(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
             period: int = 14) -> np.ndarray:
    """Average Directional Index. Returns full-length array, NaN-padded."""
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
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]))
        plus_dm[i] = h_diff if (h_diff > l_diff and h_diff > 0) else 0.0
        minus_dm[i] = l_diff if (l_diff > h_diff and l_diff > 0) else 0.0

    atr_arr = np.zeros(n)
    atr_arr[period] = np.sum(tr[1: period + 1])
    s_plus = np.sum(plus_dm[1: period + 1])
    s_minus = np.sum(minus_dm[1: period + 1])
    plus_di = np.zeros(n)
    minus_di = np.zeros(n)
    if atr_arr[period] > 0:
        plus_di[period] = 100.0 * s_plus / atr_arr[period]
        minus_di[period] = 100.0 * s_minus / atr_arr[period]
    for i in range(period + 1, n):
        atr_arr[i] = atr_arr[i - 1] - atr_arr[i - 1] / period + tr[i]
        s_plus = s_plus - s_plus / period + plus_dm[i]
        s_minus = s_minus - s_minus / period + minus_dm[i]
        if atr_arr[i] > 0:
            plus_di[i] = 100.0 * s_plus / atr_arr[i]
            minus_di[i] = 100.0 * s_minus / atr_arr[i]

    dx = np.zeros(n)
    for i in range(period, n):
        denom = plus_di[i] + minus_di[i]
        if denom > 0:
            dx[i] = 100.0 * abs(plus_di[i] - minus_di[i]) / denom

    start_idx = period * 2
    if start_idx < n:
        adx[start_idx] = np.mean(dx[period + 1: start_idx + 1])
        for i in range(start_idx + 1, n):
            adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period
    return adx


# ── ATR (Wilder's smoothing) ────────────────────────────────────────────────

def calc_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
             period: int = 14) -> np.ndarray:
    """ATR using Wilder's smoothing. Returns full-length array (index 0 = 0)."""
    n = len(closes)
    if n < 2:
        return np.zeros(n)
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(np.abs(highs[1:] - closes[:-1]),
                   np.abs(lows[1:] - closes[:-1])),
    )
    atr_raw = np.zeros(len(tr))
    atr_raw[0] = np.mean(tr[:period]) if len(tr) >= period else tr[0]
    alpha = 1.0 / period
    for i in range(1, len(tr)):
        atr_raw[i] = alpha * tr[i] + (1 - alpha) * atr_raw[i - 1]
    atr = np.zeros(n)
    atr[1:] = atr_raw
    return atr


# ── SMA ──────────────────────────────────────────────────────────────────────

def calc_sma(closes: np.ndarray, period: int) -> np.ndarray:
    """Simple moving average, NaN-padded."""
    sma = np.full_like(closes, np.nan)
    for i in range(period - 1, len(closes)):
        sma[i] = np.mean(closes[i - period + 1: i + 1])
    return sma


# ── Bollinger Bands ──────────────────────────────────────────────────────────

def calc_bollinger(closes: np.ndarray, period: int = 20,
                   num_std: float = 2.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Bollinger Bands: (middle, upper, lower)."""
    mid = calc_sma(closes, period)
    upper = np.full_like(closes, np.nan)
    lower = np.full_like(closes, np.nan)
    for i in range(period - 1, len(closes)):
        std = np.std(closes[i - period + 1: i + 1], ddof=0)
        upper[i] = mid[i] + num_std * std
        lower[i] = mid[i] - num_std * std
    return mid, upper, lower


# ── Donchian Channel ─────────────────────────────────────────────────────────

def calc_donchian(highs: np.ndarray, lows: np.ndarray,
                  period: int = 20) -> tuple[np.ndarray, np.ndarray]:
    """Donchian channel: (upper, lower)."""
    n = len(highs)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    for i in range(period - 1, n):
        upper[i] = np.max(highs[i - period + 1: i + 1])
        lower[i] = np.min(lows[i - period + 1: i + 1])
    return upper, lower


# ── Regime detection with hysteresis ─────────────────────────────────────────

def compute_regime_series(adx: np.ndarray, ema200_slope: np.ndarray,
                          cfg: BacktestConfig) -> np.ndarray:
    """Compute TREND regime per bar with N-bar hysteresis (matches bot).

    Returns boolean array: True = TREND regime.
    """
    n = len(adx)
    is_trend = np.zeros(n, dtype=bool)
    history: list[bool] = []
    confirmation = cfg.confirmation_bars

    for i in range(n):
        a = adx[i]
        s = ema200_slope[i]

        if np.isnan(a) or np.isnan(s):
            is_trend[i] = False
            continue

        raw_trend = (a >= cfg.trend_adx_min) and (abs(s) >= cfg.ema_slope_threshold)
        history.append(raw_trend)

        max_history = confirmation + 1
        if len(history) > max_history:
            history = history[-max_history:]

        if len(history) >= confirmation:
            recent = history[-confirmation:]
            if all(r == raw_trend for r in recent):
                is_trend[i] = raw_trend
            elif len(history) > confirmation:
                is_trend[i] = history[-confirmation - 1]
            else:
                is_trend[i] = raw_trend
        else:
            is_trend[i] = raw_trend

    return is_trend


# ── Orchestrator ─────────────────────────────────────────────────────────────

def compute_indicators(candles: list[dict], cfg: BacktestConfig,
                       timeframe_scale: int = 1) -> dict:
    """Compute all indicators for a list of candles.

    Args:
        candles: List of {t, o, h, l, c, v} dicts.
        cfg: Backtest configuration.
        timeframe_scale: Multiplier for indicator periods (e.g. 3 for 5m candles
            when strategy uses 15m periods: EMA9 -> EMA27).

    Returns:
        Dict of indicator arrays, all same length as candles.
    """
    closes = np.array([c["c"] for c in candles])
    highs = np.array([c["h"] for c in candles])
    lows = np.array([c["l"] for c in candles])

    s = timeframe_scale
    ema9 = calc_ema(closes, 9 * s)
    ema21 = calc_ema(closes, 21 * s)
    ema200 = calc_ema(closes, 200 * s)
    rsi = calc_rsi(closes, 14 * s)
    adx = calc_adx(highs, lows, closes, 14 * s)
    atr = calc_atr(highs, lows, closes, 14 * s)

    # ATR as percentage of close
    atr_pct = np.zeros(len(closes))
    valid = closes > 0
    atr_pct[valid] = (atr[valid] / closes[valid]) * 100

    # EMA200 slope: lookback 4@15m scaled
    slope_lookback = 4 * s
    ema200_slope = np.full(len(closes), np.nan)
    for i in range(slope_lookback, len(closes)):
        if not np.isnan(ema200[i]) and not np.isnan(ema200[i - slope_lookback]) and ema200[i - slope_lookback] > 0:
            ema200_slope[i] = (ema200[i] - ema200[i - slope_lookback]) / ema200[i - slope_lookback]

    # Regime with hysteresis
    is_trend = compute_regime_series(adx, ema200_slope, cfg)

    return {
        "ema9": ema9, "ema21": ema21, "ema200": ema200,
        "ema200_slope": ema200_slope,
        "rsi": rsi, "adx": adx, "atr": atr, "atr_pct": atr_pct,
        "is_trend": is_trend,
        "closes": closes, "highs": highs, "lows": lows,
    }
