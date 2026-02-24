"""Signal generators with uniform signature.

All functions: (indicators_dict, bar_idx, cfg) -> int
  1 = LONG, -1 = SHORT, 0 = no signal
"""

from __future__ import annotations

import numpy as np

from backtesting.config import BacktestConfig


def signal_trend_momentum(ind: dict, idx: int, cfg: BacktestConfig) -> int:
    """Live strategy: EMA9 > EMA21 (state-based) + RSI filter + TREND regime."""
    ema9 = ind["ema9"][idx]
    ema21 = ind["ema21"][idx]
    rsi = ind["rsi"][idx]
    atr_pct = ind["atr_pct"][idx]

    if any(np.isnan(v) for v in [ema9, ema21, rsi]):
        return 0
    if not ind["is_trend"][idx]:
        return 0
    if atr_pct < cfg.min_atr_pct:
        return 0
    if ema9 > ema21 and cfg.rsi_long_min <= rsi <= cfg.rsi_long_max:
        return 1
    if ema9 < ema21 and cfg.rsi_short_min <= rsi <= cfg.rsi_short_max:
        return -1
    return 0


def signal_rsi_reversal(ind: dict, idx: int, cfg: BacktestConfig) -> int:
    """RSI cross 30/70 reversal."""
    if idx < 1:
        return 0
    rsi = ind["rsi"]
    if np.isnan(rsi[idx]) or np.isnan(rsi[idx - 1]):
        return 0
    if rsi[idx - 1] < 30 and rsi[idx] >= 30:
        return 1
    if rsi[idx - 1] > 70 and rsi[idx] <= 70:
        return -1
    return 0


def signal_ema_no_regime(ind: dict, idx: int, cfg: BacktestConfig) -> int:
    """EMA9/EMA21 crossover without regime filter."""
    if idx < 1:
        return 0
    ema9 = ind["ema9"]
    ema21 = ind["ema21"]
    rsi_val = ind["rsi"][idx]
    if any(np.isnan(v) for v in [ema9[idx], ema21[idx], ema9[idx - 1], ema21[idx - 1], rsi_val]):
        return 0
    # Bullish crossover
    if ema9[idx - 1] <= ema21[idx - 1] and ema9[idx] > ema21[idx]:
        if cfg.rsi_long_min <= rsi_val <= cfg.rsi_long_max:
            return 1
    # Bearish crossover
    if ema9[idx - 1] >= ema21[idx - 1] and ema9[idx] < ema21[idx]:
        if cfg.rsi_short_min <= rsi_val <= cfg.rsi_short_max:
            return -1
    return 0


def signal_momentum_breakout(ind: dict, idx: int, cfg: BacktestConfig) -> int:
    """Donchian 20-bar breakout + ADX > 20."""
    if idx < 1:
        return 0
    if "don_upper" not in ind or "don_lower" not in ind:
        return 0
    don_upper = ind["don_upper"]
    don_lower = ind["don_lower"]
    adx = ind["adx"]
    closes = ind["closes"]
    if np.isnan(don_upper[idx - 1]) or np.isnan(don_lower[idx - 1]) or np.isnan(adx[idx]):
        return 0
    if adx[idx] < 20:
        return 0
    if closes[idx] > don_upper[idx - 1]:
        return 1
    if closes[idx] < don_lower[idx - 1]:
        return -1
    return 0


def signal_ema_crossover_only(ind: dict, idx: int) -> int:
    """EMA9/EMA21 crossover — no regime, RSI, or ATR filter.

    Returns: 1=LONG, -1=SHORT, 0=no signal (EMA9==EMA21 or NaN)
    """
    ema9 = ind["ema9"][idx]
    ema21 = ind["ema21"][idx]
    if np.isnan(ema9) or np.isnan(ema21):
        return 0
    if ema9 > ema21:
        return 1
    if ema9 < ema21:
        return -1
    return 0


def signal_mean_reversion(ind: dict, idx: int, cfg: BacktestConfig) -> int:
    """RSI < 25 + below BB lower for LONG, RSI > 75 + above BB upper for SHORT."""
    if "bb_upper" not in ind or "bb_lower" not in ind:
        return 0
    rsi_val = ind["rsi"][idx]
    bb_upper = ind["bb_upper"][idx]
    bb_lower = ind["bb_lower"][idx]
    closes = ind["closes"]
    if np.isnan(rsi_val) or np.isnan(bb_upper) or np.isnan(bb_lower):
        return 0
    if rsi_val < 25 and closes[idx] < bb_lower:
        return 1
    if rsi_val > 75 and closes[idx] > bb_upper:
        return -1
    return 0
