"""Signal computation for the IB scanner.

Calculates technical indicators and produces ScanResult for each symbol.
Uses pandas-native implementations to avoid numpy/pandas alignment issues
with the shared.indicators module (which operates on raw numpy arrays).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Indicator implementations (pandas-native)
# ---------------------------------------------------------------------------

def _ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    """Average True Range."""
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def _adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    """Average Directional Index."""
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    plus_dm = (high - prev_high).clip(lower=0)
    minus_dm = (prev_low - low).clip(lower=0)

    # Zero out when opposite DM is larger
    plus_dm = plus_dm.where(plus_dm > minus_dm, 0.0)
    minus_dm = minus_dm.where(minus_dm > plus_dm, 0.0)

    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)

    atr_smooth = tr.ewm(span=period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(span=period, adjust=False).mean() / atr_smooth
    minus_di = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr_smooth

    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    return dx.ewm(span=period, adjust=False).mean()


# ---------------------------------------------------------------------------
# Squeeze detection (Bollinger Bands inside Keltner Channels)
# ---------------------------------------------------------------------------

def _detect_squeeze(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    bb_period: int = 20,
    bb_mult: float = 2.0,
    kc_period: int = 20,
    kc_mult: float = 1.5,
) -> pd.Series:
    """Detect Bollinger-Keltner squeeze. Returns True when squeeze is active."""
    bb_mid = close.rolling(bb_period).mean()
    bb_std = close.rolling(bb_period).std()
    bb_upper = bb_mid + bb_mult * bb_std
    bb_lower = bb_mid - bb_mult * bb_std

    atr_val = _atr(high, low, close, kc_period)
    kc_mid = _ema(close, kc_period)
    kc_upper = kc_mid + kc_mult * atr_val
    kc_lower = kc_mid - kc_mult * atr_val

    squeeze_on = (bb_lower > kc_lower) & (bb_upper < kc_upper)
    return squeeze_on


# ---------------------------------------------------------------------------
# ScanResult dataclass
# ---------------------------------------------------------------------------

@dataclass
class ScanResult:
    """Result of scanning a single symbol."""

    symbol: str
    squeeze_fired: bool = False
    ema_cross_direction: str | None = None  # "bullish", "bearish", or None
    rsi_value: float = 50.0
    atr_pct: float = 0.0
    adx_value: float = 0.0
    trend: str = "neutral"  # "bullish", "bearish", "neutral"
    score: float = 0.0
    volume_ratio: float = 1.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


# ---------------------------------------------------------------------------
# Main scan function
# ---------------------------------------------------------------------------

def scan_symbol(symbol: str, df: pd.DataFrame) -> ScanResult:
    """Compute all signals and produce a ScanResult for one symbol.

    Args:
        symbol: Ticker symbol.
        df: Daily OHLCV DataFrame with columns [Open, High, Low, Close, Volume].

    Returns:
        ScanResult with all computed signals and composite score.
    """
    result = ScanResult(symbol=symbol)

    if df.empty or len(df) < 30:
        return result

    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    volume = df["Volume"].astype(float)

    # --- EMA crossover (9/21 on close) ---
    ema_fast = _ema(close, 9)
    ema_slow = _ema(close, 21)
    if len(ema_fast) >= 2 and len(ema_slow) >= 2:
        prev_fast = ema_fast.iloc[-2]
        prev_slow = ema_slow.iloc[-2]
        curr_fast = ema_fast.iloc[-1]
        curr_slow = ema_slow.iloc[-1]
        if prev_fast <= prev_slow and curr_fast > curr_slow:
            result.ema_cross_direction = "bullish"
        elif prev_fast >= prev_slow and curr_fast < curr_slow:
            result.ema_cross_direction = "bearish"

    # --- Trend (EMA 50 slope) ---
    ema_50 = _ema(close, 50)
    if len(ema_50) >= 5:
        slope = ema_50.iloc[-1] - ema_50.iloc[-5]
        if slope > 0:
            result.trend = "bullish"
        elif slope < 0:
            result.trend = "bearish"

    # --- RSI ---
    rsi_series = _rsi(close, 14)
    if len(rsi_series) > 0:
        last_rsi = rsi_series.iloc[-1]
        if not np.isnan(last_rsi):
            result.rsi_value = round(float(last_rsi), 2)

    # --- ATR % ---
    atr_series = _atr(high, low, close, 14)
    if len(atr_series) > 0 and close.iloc[-1] != 0:
        last_atr = atr_series.iloc[-1]
        if not np.isnan(last_atr):
            result.atr_pct = round(float(last_atr / close.iloc[-1] * 100), 3)

    # --- ADX ---
    adx_series = _adx(high, low, close, 14)
    if len(adx_series) > 0:
        last_adx = adx_series.iloc[-1]
        if not np.isnan(last_adx):
            result.adx_value = round(float(last_adx), 2)

    # --- Volume ratio (latest vs 20-day average) ---
    if len(volume) >= 20:
        vol_avg = volume.iloc[-20:].mean()
        if vol_avg > 0:
            result.volume_ratio = round(float(volume.iloc[-1] / vol_avg), 2)

    # --- Squeeze detection ---
    squeeze_series = _detect_squeeze(close, high, low)
    if len(squeeze_series) >= 2:
        # "Fired" = squeeze was ON and just turned OFF (expansion)
        prev_squeeze = squeeze_series.iloc[-2]
        curr_squeeze = squeeze_series.iloc[-1]
        if not np.isnan(prev_squeeze) and not np.isnan(curr_squeeze):
            result.squeeze_fired = bool(prev_squeeze) and not bool(curr_squeeze)

    # --- Composite score ---
    score = 0.0
    if result.squeeze_fired:
        score += 3.0
    if result.ema_cross_direction is not None:
        score += 2.0
    if result.rsi_value < 30 or result.rsi_value > 70:
        score += 1.5
    if result.adx_value > 25:
        score += 1.0
    if result.volume_ratio > 1.5:
        score += 0.5
    result.score = round(score, 1)

    return result
