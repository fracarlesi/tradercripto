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

    NOTE: This is STATE-BASED (fires every bar in trend). Use
    signal_ema_crossover_entry() for ML training to avoid data leakage.
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


def signal_ema_crossover_entry(ind: dict, idx: int) -> int:
    """Only fires on the actual EMA9/EMA21 crossover bar.

    Unlike signal_ema_crossover_only (state-based), this detects only the
    MOMENT of crossover — preventing data leakage where a 200-bar trend
    would generate 200 identical labels.

    Returns: 1=bullish crossover, -1=bearish crossover, 0=no crossover
    """
    if idx < 1:
        return 0
    prev_ema9 = ind["ema9"][idx - 1]
    prev_ema21 = ind["ema21"][idx - 1]
    curr_ema9 = ind["ema9"][idx]
    curr_ema21 = ind["ema21"][idx]
    if np.isnan(prev_ema9) or np.isnan(curr_ema9) or np.isnan(prev_ema21) or np.isnan(curr_ema21):
        return 0
    if prev_ema9 <= prev_ema21 and curr_ema9 > curr_ema21:
        return 1   # Bullish crossover
    if prev_ema9 >= prev_ema21 and curr_ema9 < curr_ema21:
        return -1  # Bearish crossover
    return 0


def signal_volume_breakout_entry(ind: dict, idx: int,
                                 min_volume_ratio: float = 2.0,
                                 min_candle_body_pct: float = 0.3,
                                 min_atr_pct: float = 0.15,
                                 rsi_min: float = 25.0,
                                 rsi_max: float = 80.0) -> int:
    """Volume breakout signal: fires when volume spike + price momentum align.

    Detects pump/dump starts from anomalous volume with directional conviction.
    Designed to work in CHAOS regime (low ADX) where EMA crossover is too slow.

    Conditions (all must be true on the same bar):
    - volume_ratio >= min_volume_ratio (volume spike vs SMA20)
    - |close - open| / open >= min_candle_body_pct% (volume moved the price)
    - atr_pct >= min_atr_pct% (market is alive)
    - rsi_min <= RSI <= rsi_max (not in extremes)

    Direction (price momentum, NOT EMA):
    - LONG: close > open AND close > prev_close
    - SHORT: close < open AND close < prev_close

    Returns: 1=LONG breakout, -1=SHORT breakout, 0=no signal
    """
    if idx < 1:
        return 0

    # Required arrays
    for key in ("closes", "opens", "volumes", "vol_sma20", "rsi", "atr_pct"):
        if key not in ind:
            return 0

    close = ind["closes"][idx]
    open_price = ind["opens"][idx]
    prev_close = ind["closes"][idx - 1]
    volume = ind["volumes"][idx]
    vol_sma = ind["vol_sma20"][idx]
    rsi = ind["rsi"][idx]
    atr_pct = ind["atr_pct"][idx]

    # NaN checks
    if any(np.isnan(v) for v in [close, open_price, prev_close, rsi, atr_pct]):
        return 0
    if np.isnan(vol_sma) or vol_sma <= 0:
        return 0
    if open_price <= 0:
        return 0

    # Condition 1: Volume spike
    volume_ratio = volume / vol_sma
    if volume_ratio < min_volume_ratio:
        return 0

    # Condition 2: Candle body (price moved meaningfully)
    candle_body_pct = abs(close - open_price) / open_price * 100
    if candle_body_pct < min_candle_body_pct:
        return 0

    # Condition 3: Minimum volatility
    if atr_pct < min_atr_pct:
        return 0

    # Condition 4: RSI not in extremes
    if not (rsi_min <= rsi <= rsi_max):
        return 0

    # Direction: price momentum
    if close > open_price and close > prev_close:
        return 1   # LONG breakout
    if close < open_price and close < prev_close:
        return -1  # SHORT breakout

    return 0


def signal_momentum_burst_entry(ind: dict, idx: int,
                                 min_rsi_slope: float = 8.0,
                                 min_candle_body_pct: float = 0.3,
                                 max_rsi_entry: float = 75.0,
                                 min_volume_ratio: float = 1.2) -> int:
    """Momentum burst signal: fires when RSI accelerates before overbought.

    Captures the inflection point where price is accelerating but hasn't
    gone parabolic yet. Designed for CHAOS+TREND regimes.

    Conditions (all must be true):
    - RSI slope (RSI[i] - RSI[i-2]) >= min_rsi_slope (RSI accelerating)
    - Price > EMA9 (above fast moving average)
    - |close - open| / open >= min_candle_body_pct% (conviction candle)
    - RSI <= max_rsi_entry for LONG (enter BEFORE overbought)
    - RSI >= (100 - max_rsi_entry) for SHORT (enter BEFORE oversold)
    - volume_ratio >= min_volume_ratio (some volume confirmation)

    Direction:
    - LONG: RSI rising + close > open (bullish candle)
    - SHORT: RSI falling + close < open (bearish candle)

    Returns: 1=LONG burst, -1=SHORT burst, 0=no signal
    """
    if idx < 2:
        return 0

    # Required arrays
    for key in ("closes", "opens", "rsi", "ema9", "volumes", "vol_sma20"):
        if key not in ind:
            return 0

    close = ind["closes"][idx]
    open_price = ind["opens"][idx]
    rsi_curr = ind["rsi"][idx]
    rsi_2ago = ind["rsi"][idx - 2]
    ema9 = ind["ema9"][idx]

    # NaN checks
    if any(np.isnan(v) for v in [close, open_price, rsi_curr, rsi_2ago, ema9]):
        return 0
    if open_price <= 0:
        return 0

    # RSI slope
    rsi_slope = rsi_curr - rsi_2ago

    # Volume ratio
    vol_sma = ind["vol_sma20"][idx]
    if np.isnan(vol_sma) or vol_sma <= 0:
        return 0
    volume_ratio = ind["volumes"][idx] / vol_sma
    if volume_ratio < min_volume_ratio:
        return 0

    # Candle body
    candle_body_pct = abs(close - open_price) / open_price * 100
    if candle_body_pct < min_candle_body_pct:
        return 0

    # LONG: RSI accelerating up, price above EMA9, bullish candle, not overbought
    if (rsi_slope >= min_rsi_slope
            and close > ema9
            and close > open_price
            and rsi_curr <= max_rsi_entry):
        return 1

    # SHORT: RSI accelerating down, price below EMA9, bearish candle, not oversold
    if (rsi_slope <= -min_rsi_slope
            and close < ema9
            and close < open_price
            and rsi_curr >= (100 - max_rsi_entry)):
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
