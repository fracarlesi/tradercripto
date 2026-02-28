"""ML dataset generator: extracts labeled features from historical candles.

Downloads candle data via the backtesting API, computes indicators, finds
EMA crossover signals, and labels each signal by whether TP or SL was hit
first in forward simulation.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from backtesting.api import get_candles
from backtesting.config import BacktestConfig, load_config
from backtesting.indicators import (
    calc_bollinger,
    compute_indicators,
)
from backtesting.signals import signal_ema_crossover_entry, signal_volume_breakout_entry, signal_momentum_burst_entry

__all__ = ["generate_dataset"]

logger = logging.getLogger(__name__)

RATE_LIMIT_SLEEP = 0.25
MAX_FORWARD_BARS = 100


def _label_signal(
    candles: list[dict],
    entry_idx: int,
    direction: int,
    tp_pct: float,
    sl_pct: float,
) -> int | None:
    """Simulate forward from entry bar to determine TP/SL outcome.

    Args:
        candles: Full candle list.
        entry_idx: Index of the signal bar (entry at close price).
        direction: 1 for LONG, -1 for SHORT.
        tp_pct: Take-profit distance as fraction (e.g. 0.016 for 1.6%).
        sl_pct: Stop-loss distance as fraction (e.g. 0.008 for 0.8%).

    Returns:
        1 if TP hit first, 0 if SL hit first, None if neither within MAX_FORWARD_BARS.
    """
    entry_price = candles[entry_idx]["c"]
    if entry_price <= 0:
        return None

    if direction == 1:  # LONG
        tp_price = entry_price * (1 + tp_pct)
        sl_price = entry_price * (1 - sl_pct)
    else:  # SHORT
        tp_price = entry_price * (1 - tp_pct)
        sl_price = entry_price * (1 + sl_pct)

    end_idx = min(entry_idx + MAX_FORWARD_BARS + 1, len(candles))

    for i in range(entry_idx + 1, end_idx):
        high = candles[i]["h"]
        low = candles[i]["l"]

        if direction == 1:
            if low <= sl_price:
                return 0
            if high >= tp_price:
                return 1
        else:
            if high >= sl_price:
                return 0
            if low <= tp_price:
                return 1

    return None  # Neither hit


def _extract_features(
    candles: list[dict],
    ind: dict,
    idx: int,
    direction: int,
    volumes: np.ndarray,
    vol_sma20: np.ndarray,
    bb_upper: np.ndarray,
    bb_lower: np.ndarray,
    signal_type: float = 0.0,
) -> dict:
    """Extract feature dict for a single signal bar."""
    close = candles[idx]["c"]
    open_price = candles[idx]["o"]
    ema9 = ind["ema9"][idx]
    ema21 = ind["ema21"][idx]
    ema200 = ind["ema200"][idx]

    # EMA spread
    ema_spread_pct = abs(ema9 - ema21) / ema21 * 100 if ema21 != 0 else 0.0

    # Volume ratio
    if not np.isnan(vol_sma20[idx]) and vol_sma20[idx] > 0:
        volume_ratio = volumes[idx] / vol_sma20[idx]
    else:
        volume_ratio = 1.0

    # Bollinger position
    bu = bb_upper[idx]
    bl = bb_lower[idx]
    if not np.isnan(bu) and not np.isnan(bl) and (bu - bl) > 0:
        bb_position = (close - bl) / (bu - bl)
    else:
        bb_position = 0.5

    # EMA slopes (4-bar lookback)
    if idx >= 4 and not np.isnan(ind["ema9"][idx - 4]) and ind["ema9"][idx - 4] > 0:
        ema9_slope = (ind["ema9"][idx] - ind["ema9"][idx - 4]) / ind["ema9"][idx - 4]
    else:
        ema9_slope = 0.0

    if idx >= 4 and not np.isnan(ind["ema21"][idx - 4]) and ind["ema21"][idx - 4] > 0:
        ema21_slope = (ind["ema21"][idx] - ind["ema21"][idx - 4]) / ind["ema21"][idx - 4]
    else:
        ema21_slope = 0.0

    # Close vs EMA200
    if not np.isnan(ema200) and ema200 > 0:
        close_vs_ema200 = (close - ema200) / ema200 * 100
    else:
        close_vs_ema200 = 0.0

    # Regime encoding: TREND=2.0, RANGE=0.0, CHAOS=1.0
    adx_val = ind["adx"][idx]
    is_trend = ind["is_trend"][idx]
    if not np.isnan(adx_val) and is_trend:
        regime_encoded = 2.0  # TREND
    elif not np.isnan(adx_val) and adx_val <= 20:
        regime_encoded = 0.0  # RANGE
    else:
        regime_encoded = 1.0  # CHAOS

    # Hour of day (UTC), normalized to [0, 1)
    ts_ms = candles[idx]["t"]
    hour_of_day = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).hour / 24.0

    # Candle body percentage
    candle_body_pct = abs(close - open_price) / open_price * 100 if open_price > 0 else 0.0

    # RSI slope (2-bar lookback)
    if idx >= 2 and not np.isnan(ind["rsi"][idx - 2]):
        rsi_slope = ind["rsi"][idx] - ind["rsi"][idx - 2]
    else:
        rsi_slope = 0.0

    return {
        "adx": ind["adx"][idx],
        "rsi": ind["rsi"][idx],
        "atr_pct": ind["atr_pct"][idx],
        "ema_spread_pct": ema_spread_pct,
        "volume_ratio": volume_ratio,
        "bb_position": bb_position,
        "ema9_slope": ema9_slope,
        "ema21_slope": ema21_slope,
        "close_vs_ema200": close_vs_ema200,
        "regime_encoded": regime_encoded,
        "hour_of_day": hour_of_day,
        "signal_type": signal_type,
        "candle_body_pct": candle_body_pct,
        "rsi_slope": rsi_slope,
    }


def generate_dataset(
    symbols: list[str],
    days: int = 90,
    cfg: BacktestConfig | None = None,
) -> pd.DataFrame:
    """Generate labeled ML dataset from historical candle data.

    For each symbol, downloads candles, computes indicators, finds EMA
    crossover signals, and labels each by forward TP/SL simulation.

    Args:
        symbols: List of asset symbols to process.
        days: Number of days of history to fetch.
        cfg: Backtest config (loaded from trading.yaml if None).

    Returns:
        DataFrame with feature columns, 'label', 'symbol', 'timestamp'.
    """
    if cfg is None:
        cfg = load_config()

    end_ms = int(time.time() * 1000)
    start_ms = int((time.time() - days * 86400) * 1000)

    all_rows: list[dict] = []

    for sym_idx, symbol in enumerate(symbols):
        logger.info(
            "[%d/%d] Processing %s ...", sym_idx + 1, len(symbols), symbol
        )
        try:
            candles = get_candles(symbol, cfg.timeframe, start_ms, end_ms)
        except Exception:
            logger.warning("Failed to fetch candles for %s, skipping", symbol)
            time.sleep(RATE_LIMIT_SLEEP)
            continue
        time.sleep(RATE_LIMIT_SLEEP)

        if len(candles) < cfg.warmup_bars:
            logger.debug(
                "%s: only %d candles (need %d), skipping",
                symbol, len(candles), cfg.warmup_bars,
            )
            continue

        # Compute standard indicators (now includes opens, volumes, vol_sma20)
        ind = compute_indicators(candles, cfg)

        # Additional indicators for features
        closes = ind["closes"]
        volumes = ind["volumes"]
        vol_sma20 = ind["vol_sma20"]
        _, bb_upper, bb_lower = calc_bollinger(closes)

        # Dedup: track (symbol, bar_idx) to avoid duplicate labels from both signals
        seen: set[int] = set()

        # --- PATH 1: EMA crossover signals (signal_type=0.0) ---
        crossover_count = 0
        for idx in range(cfg.warmup_bars, len(candles)):
            sig = signal_ema_crossover_entry(ind, idx)
            if sig == 0:
                continue

            label = _label_signal(candles, idx, sig, cfg.tp_pct, cfg.sl_pct)
            if label is None:
                continue  # Neither TP nor SL hit within 100 bars

            features = _extract_features(
                candles, ind, idx, sig,
                volumes, vol_sma20, bb_upper, bb_lower,
                signal_type=0.0,
            )
            features["label"] = label
            features["symbol"] = symbol
            features["timestamp"] = candles[idx]["t"]
            all_rows.append(features)
            seen.add(idx)
            crossover_count += 1

        # --- PATH 2: Volume breakout signals (signal_type=1.0) ---
        breakout_count = 0
        for idx in range(cfg.warmup_bars, len(candles)):
            if idx in seen:
                continue  # Already labeled by crossover on this bar

            sig = signal_volume_breakout_entry(ind, idx)
            if sig == 0:
                continue

            label = _label_signal(candles, idx, sig, cfg.tp_pct, cfg.sl_pct)
            if label is None:
                continue

            features = _extract_features(
                candles, ind, idx, sig,
                volumes, vol_sma20, bb_upper, bb_lower,
                signal_type=1.0,
            )
            features["label"] = label
            features["symbol"] = symbol
            features["timestamp"] = candles[idx]["t"]
            all_rows.append(features)
            breakout_count += 1

        # --- PATH 3: Momentum burst signals (signal_type=2.0) ---
        burst_count = 0
        for idx in range(cfg.warmup_bars, len(candles)):
            if idx in seen:
                continue  # Already labeled by crossover or breakout on this bar

            sig = signal_momentum_burst_entry(ind, idx)
            if sig == 0:
                continue

            label = _label_signal(candles, idx, sig, cfg.tp_pct, cfg.sl_pct)
            if label is None:
                continue

            features = _extract_features(
                candles, ind, idx, sig,
                volumes, vol_sma20, bb_upper, bb_lower,
                signal_type=2.0,
            )
            features["label"] = label
            features["symbol"] = symbol
            features["timestamp"] = candles[idx]["t"]
            all_rows.append(features)
            seen.add(idx)
            burst_count += 1

        logger.info(
            "%s: %d crossover + %d breakout + %d burst = %d labeled signals from %d candles",
            symbol, crossover_count, breakout_count, burst_count,
            crossover_count + breakout_count + burst_count, len(candles),
        )

    df = pd.DataFrame(all_rows)
    if not df.empty:
        logger.info(
            "Dataset: %d samples, %.1f%% wins",
            len(df),
            df["label"].mean() * 100,
        )
    else:
        logger.warning("Dataset is empty - no signals found")

    return df
