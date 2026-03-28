"""
Bollinger-Keltner Squeeze Indicator
====================================

Pure, stateless functions for detecting Bollinger Band / Keltner Channel
squeeze-to-expansion transitions.  No I/O, no asyncio — input is numpy arrays,
output is a plain dataclass.

A "squeeze" occurs when the Bollinger Bands contract inside the Keltner Channels,
indicating a period of low volatility.  When the bands expand back outside the
channels ("fire"), a directional move is likely.

Author: Francesco Carlesi
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import numpy as np

from .market_state import calculate_atr, calculate_ema

logger = logging.getLogger(__name__)


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class SqueezeResult:
    """Result of a single squeeze detection run."""
    symbol: str
    in_squeeze_now: bool        # current bar: BB inside KC
    was_in_squeeze: bool        # at least `lookback` prior bars were in squeeze
    fired: bool                 # was_in_squeeze AND NOT in_squeeze_now (edge-triggered)
    bb_width: float             # (upper - lower) / mid — bandwidth
    kc_width: float             # (upper - lower) / mid — bandwidth
    squeeze_bars: int           # consecutive bars in squeeze ending at current-1
    timestamp: float            # time.time()


# =============================================================================
# Bollinger Bands
# =============================================================================

def compute_bollinger_bands(
    close: np.ndarray,
    period: int = 20,
    std_mult: float = 2.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute Bollinger Bands using a true SMA (not EMA).

    Returns (lower, mid, upper) arrays of length ``len(close) - period + 1``.
    Uses ``np.convolve`` for the SMA and a rolling window for std.
    """
    n = len(close)
    if n < period:
        empty = np.array([], dtype=float)
        return empty, empty, empty

    # SMA via convolve — mode='valid' gives length (n - period + 1)
    mid = np.convolve(close, np.ones(period) / period, mode="valid")

    # Rolling standard deviation (population, ddof=0 to match typical BB)
    rolling_std = np.array([
        np.std(close[i : i + period]) for i in range(n - period + 1)
    ])

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

    Returns (lower, mid, upper) where:
        - mid  = EMA(close, ema_period)      — length ``len(close)``
        - width = ATR(atr_period) * atr_mult  — length ``len(close) - 1`` (no TR for bar 0)

    The returned arrays are trimmed to the shorter length so they align.
    Final length: ``len(close) - 1``.
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

    If the arrays are too short, returns a safe ``SqueezeResult`` with ``fired=False``.
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
        logger.debug("squeeze: not enough bars for %s (%d < %d)", symbol, len(close), min_bars)
        return _safe

    # -- Compute indicators ------------------------------------------------
    bb_lower, bb_mid, bb_upper = compute_bollinger_bands(close, bb_period, bb_std_mult)
    kc_lower, kc_mid, kc_upper = compute_keltner_channels(close, high, low, kc_ema_period, kc_atr_period, kc_atr_mult)

    if len(bb_mid) == 0 or len(kc_mid) == 0:
        return _safe

    # -- Align BB and KC to the same time axis ----------------------------
    # BB has length (n - bb_period + 1), starting at index bb_period-1
    # KC has length (n - 1), starting at index 1
    # We take the overlap from the right (most recent bars).
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
    for i in range(len(in_squeeze) - 2, -1, -1):  # walk backwards from second-to-last
        if in_squeeze[i]:
            squeeze_bars += 1
        else:
            break

    was_in_squeeze = squeeze_bars >= lookback

    fired = was_in_squeeze and not in_squeeze_now

    # -- Bandwidth metrics (current bar) ----------------------------------
    last_bb_mid = float(bb_m[-1])
    last_kc_mid = float(kc_m[-1])

    if last_bb_mid != 0.0:
        bb_width = float(bb_u[-1] - bb_l[-1]) / last_bb_mid
    else:
        bb_width = 0.0

    if last_kc_mid != 0.0:
        kc_width = float(kc_u[-1] - kc_l[-1]) / last_kc_mid
    else:
        kc_width = 0.0

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
            "squeeze FIRED for %s — %d bars in squeeze, bb_width=%.4f, kc_width=%.4f",
            symbol, squeeze_bars, bb_width, kc_width,
        )

    return result
