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
DEFAULT_MAX_FORWARD_BARS = 24  # 6 hours on 15m, matching live max_hold
DEFAULT_SLIPPAGE_PCT = 0.0005  # 0.05% adverse slippage on entry


def _label_signal(
    candles: list[dict],
    entry_idx: int,
    direction: int,
    tp_pct: float,
    sl_pct: float,
    max_forward_bars: int = DEFAULT_MAX_FORWARD_BARS,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
) -> int | None:
    """Simulate forward from entry bar to determine TP/SL outcome.

    Args:
        candles: Full candle list.
        entry_idx: Index of the signal bar (entry at close price).
        direction: 1 for LONG, -1 for SHORT.
        tp_pct: Take-profit distance as fraction (e.g. 0.025 for 2.5%).
        sl_pct: Stop-loss distance as fraction (e.g. 0.01 for 1.0%).
        max_forward_bars: Max bars to look ahead (default 24 = 6h on 15m).
        slippage_pct: Adverse slippage on entry (default 0.05%).

    Returns:
        1 if TP hit first, 0 if SL hit first, None if neither within max_forward_bars.
    """
    raw_price = candles[entry_idx]["c"]
    if raw_price <= 0:
        return None

    # Apply slippage to entry price
    if direction == 1:  # LONG: buy at slightly higher price
        entry_price = raw_price * (1 + slippage_pct)
    else:  # SHORT: sell at slightly lower price
        entry_price = raw_price * (1 - slippage_pct)

    # Fee-adjusted TP/SL levels: subtract round-trip fees (maker+taker ~0.09%)
    fee_per_side = 0.00045  # 0.045% per side
    fee_roundtrip = fee_per_side * 2

    if direction == 1:  # LONG
        tp_price = entry_price * (1 + tp_pct - fee_roundtrip)
        sl_price = entry_price * (1 - sl_pct + fee_roundtrip)
    else:  # SHORT
        tp_price = entry_price * (1 - tp_pct + fee_roundtrip)
        sl_price = entry_price * (1 + sl_pct - fee_roundtrip)

    end_idx = min(entry_idx + max_forward_bars + 1, len(candles))

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

    # Timeout: dead trade = loss (neither TP nor SL hit within max hold period)
    return 0


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
    btc_ind: dict | None = None,
    btc_time_idx: dict[int, int] | None = None,
    ind_1h: dict | None = None,
) -> dict:
    """Extract feature dict for a single signal bar.

    Args:
        btc_ind: BTC indicator dict (for BTC context features). None = defaults.
        btc_time_idx: BTC {timestamp: bar_idx} mapping for time alignment.
        ind_1h: 1h-equivalent indicators (computed with timeframe_scale=4).
    """
    close = candles[idx]["c"]
    open_price = candles[idx]["o"]
    ema9 = ind["ema9"][idx]
    ema21 = ind["ema21"][idx]
    ema200 = ind["ema200"][idx]

    # Signed EMA spread (directional)
    signed_ema_spread = (ema9 - ema21) / ema21 * 100 if ema21 != 0 else 0.0

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

    # Session bin: 0=Asia(00-08), 1=London(08-13), 2=US(13-21), 3=LateNight(21-00)
    ts_ms = candles[idx]["t"]
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    h = dt.hour
    session = 0 if h < 8 else (1 if h < 13 else (2 if h < 21 else 3))

    # Is weekend (Saturday=5, Sunday=6)
    is_weekend = 1 if dt.weekday() >= 5 else 0

    # Candle body percentage
    candle_body_pct = abs(close - open_price) / open_price * 100 if open_price > 0 else 0.0

    # RSI slope (2-bar lookback)
    if idx >= 2 and not np.isnan(ind["rsi"][idx - 2]):
        rsi_slope = ind["rsi"][idx] - ind["rsi"][idx - 2]
    else:
        rsi_slope = 0.0

    # ATR percentile: rank of current ATR in last 100 bars [0,1]
    atr_vals = ind["atr_pct"]
    lookback = min(100, idx + 1)
    if lookback > 1 and not np.isnan(atr_vals[idx]):
        window = atr_vals[idx - lookback + 1: idx + 1]
        valid = window[~np.isnan(window)]
        if len(valid) > 1:
            atr_percentile = float(np.searchsorted(np.sort(valid), atr_vals[idx])) / len(valid)
        else:
            atr_percentile = 0.5
    else:
        atr_percentile = 0.5

    # --- BTC context features ---
    btc_trend = 0.0
    btc_rsi = 50.0
    btc_ema9_slope = 0.0
    if btc_ind is not None and btc_time_idx is not None:
        btc_idx = btc_time_idx.get(ts_ms)
        if btc_idx is not None:
            b_ema9 = btc_ind["ema9"][btc_idx]
            b_ema21 = btc_ind["ema21"][btc_idx]
            if not np.isnan(b_ema9) and not np.isnan(b_ema21):
                if b_ema9 > b_ema21:
                    btc_trend = 1.0
                elif b_ema9 < b_ema21:
                    btc_trend = -1.0
            if not np.isnan(btc_ind["rsi"][btc_idx]):
                btc_rsi = float(btc_ind["rsi"][btc_idx])
            if btc_idx >= 4 and not np.isnan(btc_ind["ema9"][btc_idx - 4]) and btc_ind["ema9"][btc_idx - 4] > 0:
                btc_ema9_slope = (btc_ind["ema9"][btc_idx] - btc_ind["ema9"][btc_idx - 4]) / btc_ind["ema9"][btc_idx - 4]

    # --- Multi-TF alignment features ---
    rsi_1h = float(ind["rsi"][idx])  # default to 15m RSI
    adx_1h = float(adx_val) if not np.isnan(adx_val) else 0.0
    tf_alignment = 0.0
    if ind_1h is not None:
        if not np.isnan(ind_1h["rsi"][idx]):
            rsi_1h = float(ind_1h["rsi"][idx])
        if not np.isnan(ind_1h["adx"][idx]):
            adx_1h = float(ind_1h["adx"][idx])
        # Compare 15m vs 1h EMA direction
        if (not np.isnan(ind_1h["ema9"][idx]) and not np.isnan(ind_1h["ema21"][idx])
                and not np.isnan(ema9) and not np.isnan(ema21)):
            dir_15m = 1.0 if ema9 > ema21 else -1.0
            dir_1h = 1.0 if ind_1h["ema9"][idx] > ind_1h["ema21"][idx] else -1.0
            tf_alignment = 1.0 if dir_15m == dir_1h else -1.0

    # --- Tier 3: log_volume_24h, bb_width, adx_slope, funding_rate ---
    import math

    # log_volume_24h: log10(sum of last 96 bars volume * close price)
    lookback_vol = min(96, idx + 1)
    vol_slice = volumes[idx - lookback_vol + 1: idx + 1]
    close_slice = ind["closes"][idx - lookback_vol + 1: idx + 1]
    vol_usd_24h = float(np.nansum(vol_slice * close_slice))
    log_volume_24h = math.log10(max(vol_usd_24h, 1.0))

    # bb_width: (bb_upper - bb_lower) / bb_mid * 100
    bb_width = 0.0
    if not np.isnan(bu) and not np.isnan(bl):
        bb_mid = (bu + bl) / 2.0
        if bb_mid > 0:
            bb_width = (bu - bl) / bb_mid * 100

    # adx_slope: (adx[i] - adx[i-4]) / max(adx[i-4], 1.0)
    if idx >= 4 and not np.isnan(ind["adx"][idx - 4]):
        adx_slope = (adx_val - ind["adx"][idx - 4]) / max(ind["adx"][idx - 4], 1.0)
    else:
        adx_slope = 0.0

    # funding_rate: not available in historical candle data
    funding_rate = 0.0

    return {
        "adx": float(adx_val) if not np.isnan(adx_val) else 0.0,
        "rsi": float(ind["rsi"][idx]),
        "atr_pct": float(ind["atr_pct"][idx]),
        "volume_ratio": volume_ratio,
        "bb_position": bb_position,
        "ema9_slope": ema9_slope,
        "ema21_slope": ema21_slope,
        "close_vs_ema200": close_vs_ema200,
        "regime_encoded": regime_encoded,
        "session": session,
        "signal_type": signal_type,
        "candle_body_pct": candle_body_pct,
        "rsi_slope": rsi_slope,
        # Tier 1
        "is_weekend": is_weekend,
        "atr_percentile": atr_percentile,
        "signed_ema_spread": signed_ema_spread,
        "direction": float(direction),
        # Tier 2
        "btc_trend": btc_trend,
        "btc_rsi": btc_rsi,
        "btc_ema9_slope": btc_ema9_slope,
        "tf_alignment": tf_alignment,
        "rsi_1h": rsi_1h,
        "adx_1h": adx_1h,
        # Tier 3
        "log_volume_24h": log_volume_24h,
        "bb_width": bb_width,
        "adx_slope": adx_slope,
        "funding_rate": funding_rate,
    }


def generate_dataset(
    symbols: list[str],
    days: int = 90,
    cfg: BacktestConfig | None = None,
    asset_volumes: dict[str, float] | None = None,
    min_volume_24h: float = 500_000,
    label_tp_pct: float | None = None,
    label_sl_pct: float | None = None,
    label_max_forward_bars: int | None = None,
    label_slippage_pct: float | None = None,
) -> pd.DataFrame:
    """Generate labeled ML dataset from historical candle data.

    Args:
        symbols: List of asset symbols to process.
        days: Number of days of history to fetch.
        cfg: Backtest config (loaded from trading.yaml if None).
        asset_volumes: Dict of asset name -> 24h USD volume (for filtering).
        min_volume_24h: Minimum 24h volume in USD to include asset (default 500K).
        label_tp_pct: Override TP for labeling (fraction, e.g. 0.025 for 2.5%).
        label_sl_pct: Override SL for labeling (fraction, e.g. 0.01 for 1.0%).
        label_max_forward_bars: Override max forward bars (default 24).
        label_slippage_pct: Override slippage (fraction, default 0.0005).

    Returns:
        DataFrame with feature columns, 'label', 'symbol', 'timestamp'.
    """
    if cfg is None:
        cfg = load_config()

    # Resolve labeling parameters: explicit overrides > cfg > defaults
    eff_tp_pct = label_tp_pct if label_tp_pct is not None else cfg.tp_pct
    eff_sl_pct = label_sl_pct if label_sl_pct is not None else cfg.sl_pct
    eff_max_bars = label_max_forward_bars if label_max_forward_bars is not None else DEFAULT_MAX_FORWARD_BARS
    eff_slippage = label_slippage_pct if label_slippage_pct is not None else DEFAULT_SLIPPAGE_PCT
    logger.info(
        "Labeling params: TP=%.2f%% SL=%.2f%% max_bars=%d slippage=%.3f%%",
        eff_tp_pct * 100, eff_sl_pct * 100, eff_max_bars, eff_slippage * 100,
    )

    # --- Volume filter: skip low-volume assets ---
    if asset_volumes is not None:
        original_count = len(symbols)
        symbols = [
            s for s in symbols
            if asset_volumes.get(s, 0) >= min_volume_24h
        ]
        filtered_out = original_count - len(symbols)
        logger.info(
            "Volume filter: %d/%d assets pass >= $%.0f 24h volume (%d filtered out)",
            len(symbols), original_count, min_volume_24h, filtered_out,
        )

    end_ms = int(time.time() * 1000)
    start_ms = int((time.time() - days * 86400) * 1000)

    all_rows: list[dict] = []

    # --- Pre-compute BTC indicators ONCE for BTC context features ---
    btc_ind: dict | None = None
    btc_time_idx: dict[int, int] | None = None
    logger.info("Fetching BTC candles for context features...")
    try:
        btc_candles = get_candles("BTC", cfg.timeframe, start_ms, end_ms)
        if len(btc_candles) >= cfg.warmup_bars:
            btc_ind = compute_indicators(btc_candles, cfg)
            btc_time_idx = {c["t"]: i for i, c in enumerate(btc_candles)}
            logger.info("BTC context: %d candles loaded", len(btc_candles))
    except Exception:
        logger.warning("Failed to fetch BTC candles for context, using defaults")
    time.sleep(RATE_LIMIT_SLEEP)

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

        # Compute 1h-equiv indicators (timeframe_scale=4: EMA(36), RSI(56), ADX(56))
        ind_1h = compute_indicators(candles, cfg, timeframe_scale=4)

        # Additional indicators for features
        closes = ind["closes"]
        volumes = ind["volumes"]
        vol_sma20 = ind["vol_sma20"]
        _, bb_upper, bb_lower = calc_bollinger(closes)

        # For BTC itself, pass None to get defaults (btc_trend=0, rsi=50, slope=0)
        sym_btc_ind = btc_ind if symbol != "BTC" else None
        sym_btc_time_idx = btc_time_idx if symbol != "BTC" else None

        # Dedup: track (symbol, bar_idx) to avoid duplicate labels from both signals
        seen: set[int] = set()

        # --- PATH 1: EMA crossover signals (signal_type=0.0) ---
        crossover_count = 0
        for idx in range(cfg.warmup_bars, len(candles)):
            sig = signal_ema_crossover_entry(ind, idx)
            if sig == 0:
                continue

            label = _label_signal(candles, idx, sig, eff_tp_pct, eff_sl_pct, eff_max_bars, eff_slippage)
            if label is None:
                continue

            features = _extract_features(
                candles, ind, idx, sig,
                volumes, vol_sma20, bb_upper, bb_lower,
                signal_type=0.0,
                btc_ind=sym_btc_ind,
                btc_time_idx=sym_btc_time_idx,
                ind_1h=ind_1h,
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

            label = _label_signal(candles, idx, sig, eff_tp_pct, eff_sl_pct, eff_max_bars, eff_slippage)
            if label is None:
                continue

            features = _extract_features(
                candles, ind, idx, sig,
                volumes, vol_sma20, bb_upper, bb_lower,
                signal_type=1.0,
                btc_ind=sym_btc_ind,
                btc_time_idx=sym_btc_time_idx,
                ind_1h=ind_1h,
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

            label = _label_signal(candles, idx, sig, eff_tp_pct, eff_sl_pct, eff_max_bars, eff_slippage)
            if label is None:
                continue

            features = _extract_features(
                candles, ind, idx, sig,
                volumes, vol_sma20, bb_upper, bb_lower,
                signal_type=2.0,
                btc_ind=sym_btc_ind,
                btc_time_idx=sym_btc_time_idx,
                ind_1h=ind_1h,
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
