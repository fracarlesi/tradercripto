"""
Market Context — Longer-term indicators for prompt enrichment
=============================================================

Computes 24h/3d/7d price changes, volume ratio, RSI(4h), and trend
direction from 15m candle data.
"""

from __future__ import annotations


def compute_market_context(candles: list[dict], symbol: str = "") -> dict:
    """Compute longer-term market context from 15m candle data.

    Args:
        candles: Full list of 15m candles (ideally 672+ for 7d coverage).
                 Each dict has: open, high, low, close, volume.
        symbol: Asset symbol for labeling.

    Returns:
        Dict with keys: symbol, pct_24h, pct_3d, pct_7d, vol_ratio, rsi_4h, trend.
    """
    if len(candles) < 2:
        return {"symbol": symbol}

    closes = [c["close"] for c in candles]
    current_close = closes[-1]
    ctx: dict = {"symbol": symbol}

    # Percentage changes (96 bars = 24h, 288 = 3d, 672 = 7d for 15m candles)
    for label, bars in [("pct_24h", 96), ("pct_3d", 288), ("pct_7d", 672)]:
        if len(closes) > bars and closes[-bars - 1] != 0:
            ctx[label] = (current_close / closes[-bars - 1] - 1.0) * 100

    # Volume ratio: last bar vs 20-bar average
    volumes = [c["volume"] for c in candles]
    if len(volumes) >= 20:
        avg_vol = sum(volumes[-20:]) / 20
        if avg_vol > 0:
            ctx["vol_ratio"] = volumes[-1] / avg_vol

    # RSI on 4h resampled candles (every 16 bars of 15m)
    rsi_4h = _compute_rsi_4h(closes)
    if rsi_4h is not None:
        ctx["rsi_4h"] = rsi_4h

    # Trend: price vs EMA50 direction
    ctx["trend"] = _compute_trend(closes)

    return ctx


def _compute_rsi_4h(closes_15m: list[float], period: int = 14) -> float | None:
    """Compute RSI(14) on 4h-resampled closes (every 16 bars of 15m)."""
    # Resample: take every 16th close
    closes_4h = closes_15m[::16]
    if len(closes_4h) < period + 1:
        return None

    # Wilder's smoothed RSI
    deltas = [closes_4h[i] - closes_4h[i - 1] for i in range(1, len(closes_4h))]

    # Seed with SMA of first `period` deltas
    gains = [max(d, 0) for d in deltas[:period]]
    losses = [max(-d, 0) for d in deltas[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    # Smooth
    for d in deltas[period:]:
        avg_gain = (avg_gain * (period - 1) + max(d, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0)) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _compute_trend(closes: list[float], ema_period: int = 50) -> str:
    """Determine trend: UP, DOWN, or RANGE based on EMA50 on 15m candles."""
    if len(closes) < ema_period + 5:
        return "RANGE"

    # Compute EMA
    multiplier = 2.0 / (ema_period + 1)
    ema = sum(closes[:ema_period]) / ema_period
    ema_prev = ema
    for price in closes[ema_period:]:
        ema_prev = ema
        ema = (price - ema) * multiplier + ema

    ema_rising = ema > ema_prev
    price_above = closes[-1] > ema

    if price_above and ema_rising:
        return "UP"
    elif not price_above and not ema_rising:
        return "DOWN"
    return "RANGE"
