"""
Prepare training episodes from downloaded equity daily data.

Reads Parquet files, computes technical indicators, detects setups
(squeeze fired, EMA crossovers), and labels forward returns.

Usage:
    python3 -m ib_bot.scripts.prepare_training_data
    python3 -m ib_bot.scripts.prepare_training_data --input-dir ib_bot/data/training/equity_daily --output ib_bot/data/training/equity_episodes.parquet
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from tqdm import tqdm

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Indicator calculations (pure numpy, no external dependency)
# ---------------------------------------------------------------------------

def ema(series: np.ndarray, period: int) -> np.ndarray:
    """Exponential moving average."""
    alpha = 2.0 / (period + 1)
    result = np.empty_like(series, dtype=np.float64)
    result[0] = series[0]
    for i in range(1, len(series)):
        result[i] = alpha * series[i] + (1 - alpha) * result[i - 1]
    return result


def sma(series: np.ndarray, period: int) -> np.ndarray:
    """Simple moving average with NaN fill for initial period."""
    result = np.full_like(series, np.nan, dtype=np.float64)
    if len(series) < period:
        return result
    cumsum = np.cumsum(series)
    result[period - 1 :] = (cumsum[period - 1 :] - np.concatenate([[0], cumsum[: -period]])) / period
    return result


def rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    """Relative Strength Index."""
    result = np.full_like(close, np.nan, dtype=np.float64)
    if len(close) < period + 1:
        return result

    delta = np.diff(close)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)

    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    for i in range(period, len(delta)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            result[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i + 1] = 100.0 - (100.0 / (1.0 + rs))

    # First valid RSI at index=period
    if avg_loss == 0:
        result[period] = 100.0
    else:
        first_gain = np.mean(gains[:period])
        first_loss = np.mean(losses[:period])
        if first_loss == 0:
            result[period] = 100.0
        else:
            result[period] = 100.0 - (100.0 / (1.0 + first_gain / first_loss))

    return result


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """Average True Range."""
    result = np.full(len(high), np.nan, dtype=np.float64)
    if len(high) < period + 1:
        return result

    tr = np.empty(len(high), dtype=np.float64)
    tr[0] = high[0] - low[0]
    for i in range(1, len(high)):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))

    # Wilder smoothing
    result[period] = np.mean(tr[1 : period + 1])
    for i in range(period + 1, len(high)):
        result[i] = (result[i - 1] * (period - 1) + tr[i]) / period

    return result


def adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """Average Directional Index."""
    n = len(high)
    result = np.full(n, np.nan, dtype=np.float64)
    if n < 2 * period + 1:
        return result

    tr = np.empty(n, dtype=np.float64)
    plus_dm = np.empty(n, dtype=np.float64)
    minus_dm = np.empty(n, dtype=np.float64)
    tr[0] = high[0] - low[0]
    plus_dm[0] = 0.0
    minus_dm[0] = 0.0

    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
        up = high[i] - high[i - 1]
        down = low[i - 1] - low[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0

    # Wilder smoothing for TR, +DM, -DM
    atr_s = np.mean(tr[1 : period + 1])
    pdm_s = np.mean(plus_dm[1 : period + 1])
    mdm_s = np.mean(minus_dm[1 : period + 1])

    dx_values: list[float] = []

    for i in range(period, n):
        if i > period:
            atr_s = (atr_s * (period - 1) + tr[i]) / period
            pdm_s = (pdm_s * (period - 1) + plus_dm[i]) / period
            mdm_s = (mdm_s * (period - 1) + minus_dm[i]) / period

        if atr_s == 0:
            dx_values.append(0.0)
        else:
            pdi = 100.0 * pdm_s / atr_s
            mdi = 100.0 * mdm_s / atr_s
            di_sum = pdi + mdi
            dx = 100.0 * abs(pdi - mdi) / di_sum if di_sum != 0 else 0.0
            dx_values.append(dx)

        if len(dx_values) >= period:
            if len(dx_values) == period:
                adx_val = np.mean(dx_values[-period:])
            else:
                adx_val = (result[i - 1] * (period - 1) + dx_values[-1]) / period
            result[i] = adx_val

    return result


# ---------------------------------------------------------------------------
# Bollinger / Keltner squeeze detection
# ---------------------------------------------------------------------------

def detect_squeeze(
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    bb_period: int = 20,
    bb_mult: float = 2.0,
    kc_period: int = 20,
    kc_mult: float = 1.5,
) -> np.ndarray:
    """
    Detect squeeze states.
    Returns array: 0=no squeeze, 1=squeeze on, 2=squeeze just fired (first bar off).
    """
    n = len(close)
    result = np.zeros(n, dtype=np.int8)
    if n < max(bb_period, kc_period) + 1:
        return result

    # BB
    bb_sma = sma(close, bb_period)
    bb_std = np.full(n, np.nan, dtype=np.float64)
    for i in range(bb_period - 1, n):
        bb_std[i] = np.std(close[i - bb_period + 1 : i + 1], ddof=0)

    bb_upper = bb_sma + bb_mult * bb_std
    bb_lower = bb_sma - bb_mult * bb_std

    # KC
    kc_ema = ema(close, kc_period)
    atr_vals = atr(high, low, close, kc_period)
    kc_upper = kc_ema + kc_mult * atr_vals
    kc_lower = kc_ema - kc_mult * atr_vals

    # Squeeze: BB inside KC
    squeeze_on = np.zeros(n, dtype=bool)
    for i in range(max(bb_period, kc_period), n):
        if not np.isnan(bb_lower[i]) and not np.isnan(kc_lower[i]):
            squeeze_on[i] = (bb_lower[i] > kc_lower[i]) and (bb_upper[i] < kc_upper[i])

    for i in range(1, n):
        if squeeze_on[i]:
            result[i] = 1  # squeeze on
        elif squeeze_on[i - 1] and not squeeze_on[i]:
            result[i] = 2  # squeeze fired

    return result


# ---------------------------------------------------------------------------
# Setup detection
# ---------------------------------------------------------------------------

def detect_setups(df: pd.DataFrame) -> pd.DataFrame:
    """Detect trading setups and compute forward returns.

    Expects columns: date, open, high, low, close, volume.
    Returns DataFrame with setup rows and all indicators.
    """
    close = df["close"].values.astype(np.float64)
    high = df["high"].values.astype(np.float64)
    low = df["low"].values.astype(np.float64)
    n = len(close)

    if n < 60:
        return pd.DataFrame()

    # Indicators
    ema4 = ema(close, 4)
    ema21 = ema(close, 21)
    sma50 = sma(close, 50)
    rsi14 = rsi(close, 14)
    atr14 = atr(high, low, close, 14)
    adx14 = adx(high, low, close, 14)
    squeeze = detect_squeeze(close, high, low)

    # EMA crossovers (using EMA of high/low for the cross signals)
    ema_high_4 = ema(high, 4)
    ema_low_4 = ema(low, 4)

    # Signals
    setups: list[dict] = []

    for i in range(1, n):
        entry_price = close[i]
        if entry_price <= 0 or np.isnan(entry_price):
            continue

        # Forward returns (percentage)
        fwd = {}
        for days, label in [(1, "result_1d"), (3, "result_3d"), (5, "result_5d"), (8, "result_8d")]:
            if i + days < n:
                fwd[label] = ((close[i + days] - entry_price) / entry_price) * 100
            else:
                fwd[label] = np.nan

        base = {
            "date": df["date"].iloc[i],
            "entry_price": entry_price,
            "ema4": ema4[i],
            "ema21": ema21[i],
            "sma50": sma50[i],
            "rsi14": rsi14[i],
            "atr14": atr14[i],
            "adx14": adx14[i],
            **fwd,
        }

        # Setup 1: Squeeze fired
        if squeeze[i] == 2:
            direction = "long" if ema4[i] > ema21[i] else "short"
            setups.append({**base, "signal_type": f"squeeze_{direction}"})

        # Setup 2: EMA High crossover (bullish: ema4 of close crosses above ema21)
        if i >= 2 and ema4[i - 1] <= ema21[i - 1] and ema4[i] > ema21[i]:
            setups.append({**base, "signal_type": "long"})

        # Setup 3: EMA Low crossover (bearish: ema4 of close crosses below ema21)
        if i >= 2 and ema4[i - 1] >= ema21[i - 1] and ema4[i] < ema21[i]:
            setups.append({**base, "signal_type": "short"})

    return pd.DataFrame(setups) if setups else pd.DataFrame()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Prepare training episodes from downloaded equity daily data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 -m ib_bot.scripts.prepare_training_data
  python3 -m ib_bot.scripts.prepare_training_data --input-dir ib_bot/data/training/equity_daily --output ib_bot/data/training/equity_episodes.parquet
        """,
    )
    parser.add_argument(
        "--input-dir",
        type=str,
        default="ib_bot/data/training/equity_daily",
        help="Input directory with per-symbol Parquet files (default: ib_bot/data/training/equity_daily)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="ib_bot/data/training/equity_episodes.parquet",
        help="Output Parquet file (default: ib_bot/data/training/equity_episodes.parquet)",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    input_dir = Path(args.input_dir)
    if not input_dir.exists():
        logger.error("Input directory does not exist: %s", input_dir)
        sys.exit(1)

    parquet_files = sorted(input_dir.glob("*.parquet"))
    if not parquet_files:
        logger.error("No Parquet files found in %s", input_dir)
        sys.exit(1)

    logger.info("Processing %d symbol files from %s", len(parquet_files), input_dir)

    all_episodes: list[pd.DataFrame] = []

    for pf in tqdm(parquet_files, desc="Processing", unit="symbol"):
        symbol = pf.stem
        try:
            df = pd.read_parquet(pf)
            df = df.sort_values("date").reset_index(drop=True)
            episodes = detect_setups(df)
            if not episodes.empty:
                episodes.insert(0, "symbol", symbol)
                all_episodes.append(episodes)
        except Exception as e:
            logger.error("Error processing %s: %s", symbol, e)

    if not all_episodes:
        logger.error("No episodes generated from any symbol")
        sys.exit(1)

    result = pd.concat(all_episodes, ignore_index=True)

    # Save
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_parquet(output_path, index=False)

    # Statistics
    logger.info("=" * 60)
    logger.info("Total episodes: %d", len(result))
    logger.info("Symbols with setups: %d", result["symbol"].nunique())
    logger.info("")
    logger.info("Distribution by signal type:")
    for sig_type, count in result["signal_type"].value_counts().items():
        logger.info("  %-20s %6d", sig_type, count)

    logger.info("")

    # Win rate at 5 days (positive return = win)
    valid_5d = result.dropna(subset=["result_5d"])
    if not valid_5d.empty:
        logger.info("Win rates at 5 days (by signal type):")
        for sig_type in sorted(valid_5d["signal_type"].unique()):
            subset = valid_5d[valid_5d["signal_type"] == sig_type]
            if sig_type.endswith("short") or sig_type == "short":
                # For shorts, negative return = win
                wr = (subset["result_5d"] < 0).mean() * 100
            else:
                wr = (subset["result_5d"] > 0).mean() * 100
            avg_ret = subset["result_5d"].mean()
            logger.info("  %-20s WR=%.1f%%  avg_return=%.2f%%  n=%d", sig_type, wr, avg_ret, len(subset))

    logger.info("")
    logger.info("Output saved to: %s", output_path.resolve())


if __name__ == "__main__":
    main()
