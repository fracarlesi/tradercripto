"""
Shared Technical Indicators
============================

Pure, stateless indicator functions with no dependencies beyond numpy.
Extracted from crypto_bot for reuse across crypto_bot and ib_bot.

All functions operate on numpy arrays and return numpy arrays (or dataclasses).

Author: Francesco Carlesi
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)


# =============================================================================
# Basic Indicators
# =============================================================================


def calculate_ema(prices: np.ndarray, period: int) -> np.ndarray:
    """Calculate Exponential Moving Average.

    Uses standard EMA formula: alpha = 2 / (period + 1).
    Seed value is prices[0].

    Parameters
    ----------
    prices : np.ndarray
        1-D array of prices.
    period : int
        EMA lookback period.

    Returns
    -------
    np.ndarray
        EMA array of same length as *prices*.
    """
    alpha = 2.0 / (period + 1)
    ema = np.zeros_like(prices, dtype=float)
    ema[0] = prices[0]
    for i in range(1, len(prices)):
        ema[i] = alpha * prices[i] + (1 - alpha) * ema[i - 1]
    return ema


def calculate_atr(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int = 14,
) -> np.ndarray:
    """Calculate Average True Range (Wilder's smoothing).

    Parameters
    ----------
    high, low, close : np.ndarray
        OHLC arrays of equal length *n*.
    period : int
        ATR lookback period (default 14).

    Returns
    -------
    np.ndarray
        ATR array of length *n - 1* (no TR for bar 0).
    """
    tr = np.maximum(
        high[1:] - low[1:],
        np.maximum(
            np.abs(high[1:] - close[:-1]),
            np.abs(low[1:] - close[:-1]),
        ),
    )
    atr = np.zeros(len(tr), dtype=float)
    atr[0] = np.mean(tr[:period]) if len(tr) >= period else tr[0]

    alpha = 1.0 / period
    for i in range(1, len(tr)):
        atr[i] = alpha * tr[i] + (1 - alpha) * atr[i - 1]

    return atr


def calculate_rsi(prices: np.ndarray, period: int = 14) -> np.ndarray:
    """Calculate Relative Strength Index.

    Uses Wilder's smoothed moving average for gains/losses.

    Parameters
    ----------
    prices : np.ndarray
        1-D array of prices.
    period : int
        RSI lookback period (default 14).

    Returns
    -------
    np.ndarray
        RSI array of length ``len(prices) - 1`` (one diff lost).
        Values before *period* are unreliable (warm-up).
    """
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = np.zeros(len(deltas), dtype=float)
    avg_loss = np.zeros(len(deltas), dtype=float)

    # Initial SMA seed
    avg_gain[period - 1] = np.mean(gains[:period])
    avg_loss[period - 1] = np.mean(losses[:period])

    # Smoothed averages (Wilder)
    for i in range(period, len(deltas)):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gains[i]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + losses[i]) / period

    rs = np.divide(
        avg_gain,
        avg_loss,
        out=np.full_like(avg_gain, 100.0, dtype=float),
        where=avg_loss != 0,
    )
    rsi = 100.0 - (100.0 / (1.0 + rs))

    return rsi


def calculate_adx(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int = 14,
) -> np.ndarray:
    """Calculate Average Directional Index (ADX).

    Algorithm:
    - TR, +DM, -DM computed per bar (starting at index 1)
    - Wilder's smoothing on TR, +DM, -DM (running sums)
    - DI+/DI- = 100 * smoothed_DM / smoothed_TR
    - DX = 100 * |DI+ - DI-| / (DI+ + DI-)
    - ADX = Wilder's smoothing of DX

    Parameters
    ----------
    high, low, close : np.ndarray
        OHLC arrays of equal length *n*.
    period : int
        ADX lookback period (default 14).

    Returns
    -------
    np.ndarray
        ADX array of length *n - 1*.
        Values before ``2 * period`` are 0.0 (warm-up).
    """
    n = len(close)
    if n < 2:
        return np.zeros(max(n - 1, 0))

    tr = np.zeros(n, dtype=float)
    plus_dm = np.zeros(n, dtype=float)
    minus_dm = np.zeros(n, dtype=float)

    for i in range(1, n):
        h_diff = high[i] - high[i - 1]
        l_diff = low[i - 1] - low[i]
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
        plus_dm[i] = h_diff if (h_diff > l_diff and h_diff > 0) else 0.0
        minus_dm[i] = l_diff if (l_diff > h_diff and l_diff > 0) else 0.0

    atr_arr = np.zeros(n, dtype=float)
    atr_arr[period] = np.sum(tr[1 : period + 1])
    s_plus = np.sum(plus_dm[1 : period + 1])
    s_minus = np.sum(minus_dm[1 : period + 1])

    plus_di = np.zeros(n, dtype=float)
    minus_di = np.zeros(n, dtype=float)
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

    dx = np.zeros(n, dtype=float)
    for i in range(period, n):
        denom = plus_di[i] + minus_di[i]
        if denom > 0:
            dx[i] = 100.0 * abs(plus_di[i] - minus_di[i]) / denom

    adx_full = np.zeros(n, dtype=float)
    start_idx = period * 2
    if start_idx < n:
        adx_full[start_idx] = np.mean(dx[period + 1 : start_idx + 1])
        for i in range(start_idx + 1, n):
            adx_full[i] = (adx_full[i - 1] * (period - 1) + dx[i]) / period

    # Return length (n-1) to match original output shape
    return adx_full[1:]


# =============================================================================
# Bollinger Bands
# =============================================================================


def compute_bollinger_bands(
    close: np.ndarray,
    period: int = 20,
    std_mult: float = 2.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute Bollinger Bands using a true SMA.

    Parameters
    ----------
    close : np.ndarray
        1-D array of close prices.
    period : int
        SMA / rolling-std lookback (default 20).
    std_mult : float
        Standard-deviation multiplier (default 2.0).

    Returns
    -------
    tuple[np.ndarray, np.ndarray, np.ndarray]
        ``(lower, mid, upper)`` arrays of length ``len(close) - period + 1``.
    """
    n = len(close)
    if n < period:
        empty = np.array([], dtype=float)
        return empty, empty, empty

    # SMA via convolve -- mode='valid' gives length (n - period + 1)
    mid = np.convolve(close, np.ones(period) / period, mode="valid")

    # Rolling standard deviation (population, ddof=0)
    rolling_std = np.array(
        [np.std(close[i : i + period]) for i in range(n - period + 1)]
    )

    upper = mid + std_mult * rolling_std
    lower = mid - std_mult * rolling_std

    return lower, mid, upper


# =============================================================================
# Keltner Channels
# =============================================================================


def compute_keltner_channels(
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    ema_period: int = 20,
    atr_period: int = 14,
    atr_mult: float = 1.5,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute Keltner Channels.

    Parameters
    ----------
    close, high, low : np.ndarray
        OHLC arrays of equal length.
    ema_period : int
        EMA period for the midline (default 20).
    atr_period : int
        ATR period for the channel width (default 14).
    atr_mult : float
        ATR multiplier (default 1.5).

    Returns
    -------
    tuple[np.ndarray, np.ndarray, np.ndarray]
        ``(lower, mid, upper)`` arrays of length ``len(close) - 1``.
    """
    n = len(close)
    if n < 2:
        empty = np.array([], dtype=float)
        return empty, empty, empty

    ema = calculate_ema(close, ema_period)          # length n
    atr = calculate_atr(high, low, close, atr_period)  # length n-1

    # Align: drop first element of EMA so both have length n-1
    mid = ema[1:]
    upper = mid + atr_mult * atr
    lower = mid - atr_mult * atr

    return lower, mid, upper


# =============================================================================
# Squeeze Detection
# =============================================================================


@dataclass
class SqueezeResult:
    """Result of a single squeeze detection run."""

    symbol: str
    in_squeeze_now: bool        # current bar: BB inside KC
    was_in_squeeze: bool        # at least ``lookback`` prior bars were in squeeze
    fired: bool                 # was_in_squeeze AND NOT in_squeeze_now (edge-triggered)
    bb_width: float             # (upper - lower) / mid -- bandwidth
    kc_width: float             # (upper - lower) / mid -- bandwidth
    squeeze_bars: int           # consecutive bars in squeeze ending at current-1
    timestamp: float            # time.time()


def detect_squeeze_state(
    symbol: str,
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    bb_period: int = 20,
    bb_std_mult: float = 2.0,
    kc_ema_period: int = 20,
    kc_atr_period: int = 14,
    kc_atr_mult: float = 1.5,
    lookback: int = 3,
) -> SqueezeResult:
    """Detect Bollinger-Keltner squeeze state.

    Squeeze = BB_upper <= KC_upper AND BB_lower >= KC_lower (BB fits inside KC).
    Fire   = was_in_squeeze (>= ``lookback`` consecutive bars) AND NOT in_squeeze_now.

    Parameters
    ----------
    symbol : str
        Asset symbol (for labelling the result).
    close, high, low : np.ndarray
        OHLC arrays of equal length.
    bb_period, bb_std_mult : int, float
        Bollinger Band parameters.
    kc_ema_period, kc_atr_period, kc_atr_mult : int, int, float
        Keltner Channel parameters.
    lookback : int
        Minimum consecutive squeeze bars to qualify (default 3).

    Returns
    -------
    SqueezeResult
        If arrays are too short, returns a safe result with ``fired=False``.
    """
    _safe = SqueezeResult(
        symbol=symbol,
        in_squeeze_now=False,
        was_in_squeeze=False,
        fired=False,
        bb_width=0.0,
        kc_width=0.0,
        squeeze_bars=0,
        timestamp=time.time(),
    )

    min_bars = max(bb_period, kc_atr_period) + lookback
    if len(close) < min_bars or len(high) < min_bars or len(low) < min_bars:
        logger.debug(
            "squeeze: not enough bars for %s (%d < %d)",
            symbol, len(close), min_bars,
        )
        return _safe

    # -- Compute indicators ------------------------------------------------
    bb_lower, bb_mid, bb_upper = compute_bollinger_bands(close, bb_period, bb_std_mult)
    kc_lower, kc_mid, kc_upper = compute_keltner_channels(
        close, high, low, kc_ema_period, kc_atr_period, kc_atr_mult,
    )

    if len(bb_mid) == 0 or len(kc_mid) == 0:
        return _safe

    # -- Align BB and KC to the same time axis ----------------------------
    align_len = min(len(bb_mid), len(kc_mid))
    bb_l = bb_lower[-align_len:]
    bb_u = bb_upper[-align_len:]
    bb_m = bb_mid[-align_len:]
    kc_l = kc_lower[-align_len:]
    kc_u = kc_upper[-align_len:]
    kc_m = kc_mid[-align_len:]

    if align_len < lookback + 1:
        return _safe

    # -- Per-bar squeeze flag ---------------------------------------------
    in_squeeze = (bb_u <= kc_u) & (bb_l >= kc_l)

    # Current bar
    in_squeeze_now = bool(in_squeeze[-1])

    # Count consecutive squeeze bars ending at current-1 (prior bars only)
    squeeze_bars = 0
    for i in range(len(in_squeeze) - 2, -1, -1):
        if in_squeeze[i]:
            squeeze_bars += 1
        else:
            break

    was_in_squeeze = squeeze_bars >= lookback
    fired = was_in_squeeze and not in_squeeze_now

    # -- Bandwidth metrics (current bar) ----------------------------------
    last_bb_mid = float(bb_m[-1])
    last_kc_mid = float(kc_m[-1])

    bb_width = float(bb_u[-1] - bb_l[-1]) / last_bb_mid if last_bb_mid != 0.0 else 0.0
    kc_width = float(kc_u[-1] - kc_l[-1]) / last_kc_mid if last_kc_mid != 0.0 else 0.0

    result = SqueezeResult(
        symbol=symbol,
        in_squeeze_now=in_squeeze_now,
        was_in_squeeze=was_in_squeeze,
        fired=fired,
        bb_width=bb_width,
        kc_width=kc_width,
        squeeze_bars=squeeze_bars,
        timestamp=time.time(),
    )

    if fired:
        logger.info(
            "squeeze FIRED for %s -- %d bars in squeeze, bb_width=%.4f, kc_width=%.4f",
            symbol, squeeze_bars, bb_width, kc_width,
        )

    return result


# =============================================================================
# EMA Crossover Signals
# =============================================================================


def compute_ema_high_signal(
    closes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    ema_period: int = 4,
    trend_period: int = 21,
    sma_period: int = 50,
    rising_lookback: int = 5,
) -> tuple[str | None, float, float]:
    """Compute EMA-High breakout signal (LONG only).

    Signal LONG if:
    - close[-1] > EMA(highs, ema_period)[-1]        (breakout)
    - close[-2] < EMA(highs, ema_period)[-2]        (was below yesterday)
    - close[-3] < EMA(highs, ema_period)[-3]        (was below 2 days ago)
    - EMA(closes, trend_period)[-1] > SMA(closes, sma_period)[-1]  (trend filter)
    - SMA(closes, sma_period) rising for ``rising_lookback`` bars

    Parameters
    ----------
    closes, highs, lows : np.ndarray
        Price arrays (must have enough history).
    ema_period : int
        EMA period for the high crossover (default 4).
    trend_period : int
        EMA period for the trend filter (default 21).
    sma_period : int
        SMA period for the trend filter (default 50).
    rising_lookback : int
        Number of bars the SMA must be rising over (default 5).

    Returns
    -------
    tuple[str | None, float, float]
        ``("long", entry_close, signal_low)`` or ``(None, 0.0, 0.0)``.
    """
    n = len(closes)
    min_required = max(sma_period + rising_lookback, ema_period + 3)
    if n < min_required:
        return None, 0.0, 0.0

    ema_high = calculate_ema(highs, ema_period)

    # Breakout: close crosses above EMA(highs)
    if not (closes[-1] > ema_high[-1]):
        return None, 0.0, 0.0
    if not (closes[-2] < ema_high[-2]):
        return None, 0.0, 0.0
    if not (closes[-3] < ema_high[-3]):
        return None, 0.0, 0.0

    # Trend filter: EMA(trend) > SMA(sma)
    ema_trend = calculate_ema(closes, trend_period)
    sma_trend = np.convolve(
        closes, np.ones(sma_period) / sma_period, mode="valid",
    )

    if len(sma_trend) < rising_lookback + 1:
        return None, 0.0, 0.0

    if not (ema_trend[-1] > sma_trend[-1]):
        return None, 0.0, 0.0

    # SMA must be net rising over the lookback window
    if not (sma_trend[-1] > sma_trend[-rising_lookback - 1]):
        return None, 0.0, 0.0

    entry_close = float(closes[-1])
    signal_low = float(lows[-1])

    # Guard: doji or inverted bar
    if signal_low >= entry_close:
        return None, 0.0, 0.0

    return "long", entry_close, signal_low


def compute_ema_low_signal(
    closes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    ema_period: int = 4,
    trend_period: int = 21,
    sma_period: int = 50,
    falling_lookback: int = 5,
) -> tuple[str | None, float, float]:
    """Compute EMA-Low breakdown signal (SHORT only).

    Signal SHORT if:
    - close[-1] < EMA(lows, ema_period)[-1]         (breakdown)
    - close[-2] > EMA(lows, ema_period)[-2]         (was above yesterday)
    - close[-3] > EMA(lows, ema_period)[-3]         (was above 2 days ago)
    - EMA(closes, trend_period)[-1] < SMA(closes, sma_period)[-1]  (downtrend)
    - SMA(closes, sma_period) falling for ``falling_lookback`` bars

    Parameters
    ----------
    closes, highs, lows : np.ndarray
        Price arrays (must have enough history).
    ema_period : int
        EMA period for the low crossover (default 4).
    trend_period : int
        EMA period for the trend filter (default 21).
    sma_period : int
        SMA period for the trend filter (default 50).
    falling_lookback : int
        Number of bars the SMA must be falling over (default 5).

    Returns
    -------
    tuple[str | None, float, float]
        ``("short", entry_close, signal_high)`` or ``(None, 0.0, 0.0)``.
    """
    n = len(closes)
    min_required = max(sma_period + falling_lookback, ema_period + 3)
    if n < min_required:
        return None, 0.0, 0.0

    ema_low = calculate_ema(lows, ema_period)

    # Breakdown: close crosses below EMA(lows)
    if not (closes[-1] < ema_low[-1]):
        return None, 0.0, 0.0
    if not (closes[-2] > ema_low[-2]):
        return None, 0.0, 0.0
    if not (closes[-3] > ema_low[-3]):
        return None, 0.0, 0.0

    # Trend filter: EMA(trend) < SMA(sma)
    ema_trend = calculate_ema(closes, trend_period)
    sma_trend = np.convolve(
        closes, np.ones(sma_period) / sma_period, mode="valid",
    )

    if len(sma_trend) < falling_lookback + 1:
        return None, 0.0, 0.0

    if not (ema_trend[-1] < sma_trend[-1]):
        return None, 0.0, 0.0

    # SMA must be falling
    if not (sma_trend[-1] < sma_trend[-falling_lookback - 1]):
        return None, 0.0, 0.0

    entry_close = float(closes[-1])
    signal_high = float(highs[-1])

    # Guard: inverted bar
    if signal_high <= entry_close:
        return None, 0.0, 0.0

    return "short", entry_close, signal_high
