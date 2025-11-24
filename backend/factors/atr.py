"""
ATR (Average True Range) Factor - Volatility Indicator

ATR measures market volatility by calculating the average of true ranges over a period.
True Range = max(High - Low, |High - Previous Close|, |Low - Previous Close|)

Used for:
- Position sizing (higher ATR = smaller position)
- Stop loss placement (e.g., 2x ATR)
- Volatility filtering (avoid low-volatility sideways markets)

Based on Rizzo's trading agent methodology.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .models import Factor


def calculate_true_range(df: pd.DataFrame) -> pd.Series:
    """Calculate True Range for each candle.

    True Range = max(
        High - Low,
        |High - Previous Close|,
        |Low - Previous Close|
    )

    Args:
        df: DataFrame with High, Low, Close columns

    Returns:
        Series of True Range values
    """
    high = df['High']
    low = df['Low']
    close = df['Close']
    prev_close = close.shift(1)

    # Three components of True Range
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()

    # True Range is the maximum of the three
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    return true_range


def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Calculate Average True Range (ATR) for a given period.

    Args:
        df: DataFrame with OHLC data
        period: Number of periods for averaging (default: 14)

    Returns:
        ATR value (average of last 'period' true ranges)
    """
    if len(df) < period + 1:
        return 0.0

    true_range = calculate_true_range(df)

    # Use simple moving average of True Range
    atr = true_range.rolling(window=period).mean().iloc[-1]

    return float(atr) if not pd.isna(atr) else 0.0


def calculate_atr_percentage(df: pd.DataFrame, period: int = 14) -> float:
    """Calculate ATR as percentage of current price.

    This is more useful for comparing volatility across different assets.

    Args:
        df: DataFrame with OHLC data
        period: Number of periods for averaging

    Returns:
        ATR as percentage of current close price (e.g., 2.5 for 2.5%)
    """
    atr = calculate_atr(df, period)
    current_price = float(df['Close'].iloc[-1])

    if current_price == 0 or atr == 0:
        return 0.0

    return (atr / current_price) * 100


def compute_atr(
    history: dict[str, pd.DataFrame],
    period: int = 14,
    top_spot: pd.DataFrame | None = None
) -> pd.DataFrame:
    """Calculate ATR factor for all symbols.

    Args:
        history: Dict mapping symbol to OHLC DataFrame
        period: ATR period (default: 14)
        top_spot: Optional spot data (unused)

    Returns:
        DataFrame with columns:
        - Symbol: Asset symbol
        - ATR: Absolute ATR value
        - ATR_Pct: ATR as percentage of price
        - ATR_Score: Normalized 0-1 score (lower volatility = higher score for safety)
    """
    rows: list[dict] = []

    for symbol, df in history.items():
        if df is None or df.empty or len(df) < period + 1:
            continue

        # Ensure proper sorting
        df_copy = df.copy()
        if not pd.api.types.is_datetime64_any_dtype(df_copy["Date"]):
            df_copy["Date"] = pd.to_datetime(df_copy["Date"])
        df_sorted = df_copy.sort_values("Date", ascending=True).reset_index(drop=True)

        atr = calculate_atr(df_sorted, period)
        atr_pct = calculate_atr_percentage(df_sorted, period)

        # Also calculate short-term ATR (3 periods) for comparison
        atr_short = calculate_atr(df_sorted, period=3)
        atr_short_pct = calculate_atr_percentage(df_sorted, period=3)

        rows.append({
            "Symbol": symbol,
            "ATR": atr,
            "ATR_Pct": atr_pct,
            "ATR_Short": atr_short,
            "ATR_Short_Pct": atr_short_pct,
        })

    df_result = pd.DataFrame(rows)

    if not df_result.empty:
        # Calculate ATR Score (normalized)
        # Higher ATR % = higher volatility = lower score (riskier)
        # We want score where 0 = very volatile, 1 = low volatility
        max_atr_pct = df_result['ATR_Pct'].max()
        if max_atr_pct > 0:
            # Invert and normalize: 1 - (atr / max_atr)
            df_result['ATR_Score'] = 1 - (df_result['ATR_Pct'] / max_atr_pct)
        else:
            df_result['ATR_Score'] = 0.5

        # Sort by ATR_Pct descending (most volatile first)
        df_result = df_result.sort_values("ATR_Pct", ascending=False)

    return df_result


def get_atr_structured(history: dict[str, pd.DataFrame], period: int = 14) -> dict[str, dict]:
    """Get ATR data in structured format for JSON builder.

    Args:
        history: Dict mapping symbol to OHLC DataFrame
        period: ATR period

    Returns:
        Dict mapping symbol to ATR data:
        {
            "BTC": {
                "atr": 1500.0,
                "atr_pct": 1.72,
                "atr_short": 1200.0,
                "atr_short_pct": 1.38,
                "volatility": "medium"  # low/medium/high based on percentile
            },
            ...
        }
    """
    df_result = compute_atr(history, period)

    if df_result.empty:
        return {}

    # Calculate percentile thresholds for volatility classification
    p33 = df_result['ATR_Pct'].quantile(0.33)
    p66 = df_result['ATR_Pct'].quantile(0.66)

    structured = {}
    for _, row in df_result.iterrows():
        symbol = row['Symbol']
        atr_pct = row['ATR_Pct']

        # Classify volatility
        if atr_pct <= p33:
            volatility = "low"
        elif atr_pct <= p66:
            volatility = "medium"
        else:
            volatility = "high"

        structured[symbol] = {
            "atr": round(row['ATR'], 4),
            "atr_pct": round(atr_pct, 4),
            "atr_short": round(row['ATR_Short'], 4),
            "atr_short_pct": round(row['ATR_Short_Pct'], 4),
            "score": round(row['ATR_Score'], 4),
            "volatility": volatility,
        }

    return structured


ATR_FACTOR = Factor(
    id="atr",
    name="ATR (Average True Range)",
    description="Volatility indicator measuring average price range over 14 periods",
    columns=[
        {"key": "ATR", "label": "ATR", "type": "number", "sortable": True},
        {"key": "ATR_Pct", "label": "ATR %", "type": "percent", "sortable": True},
        {"key": "ATR_Score", "label": "Volatility Score", "type": "score", "sortable": True},
    ],
    compute=lambda history, top_spot=None: compute_atr(history, period=14, top_spot=top_spot),
)

MODULE_FACTORS = [ATR_FACTOR]
