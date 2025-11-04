"""
Technical Analysis Service - Calculate momentum and support factors

This service:
1. Fetches historical OHLCV data from Hyperliquid
2. Calculates momentum factor (price trend strength)
3. Calculates support factor (support level strength)
4. Returns quantitative signals for AI decision-making

Based on open-alpha-arena methodology.
"""

import logging
from typing import Any

import pandas as pd

from factors.momentum import compute_momentum
from factors.support import compute_support
from services.market_data.hyperliquid_market_data import get_kline_data_from_hyperliquid

logger = logging.getLogger(__name__)


def fetch_historical_data(symbols: list[str], period: str = "1d", count: int = 70) -> dict[str, pd.DataFrame]:
    """
    Fetch historical OHLCV data for multiple symbols.

    Args:
        symbols: List of symbols to fetch (e.g., ["BTC", "ETH", "SOL"])
        period: Timeframe for candles (default: "1d" daily)
        count: Number of candles to fetch (default: 70 days, min 61 for support calc)

    Returns:
        Dictionary mapping symbol to DataFrame with columns:
        - Date: datetime
        - Open, High, Low, Close: float
        - Volume: float
    """
    history = {}

    for symbol in symbols:
        try:
            # Fetch klines from Hyperliquid
            klines = get_kline_data_from_hyperliquid(symbol, period=period, count=count)

            if not klines or len(klines) < 2:
                logger.warning(f"Insufficient data for {symbol}: {len(klines) if klines else 0} candles")
                continue

            # Convert to DataFrame
            df = pd.DataFrame(klines)

            # Rename columns to match factor calculation format
            df = df.rename(columns={
                'datetime_str': 'Date',
                'open': 'Open',
                'high': 'High',
                'low': 'Low',
                'close': 'Close',
                'volume': 'Volume'
            })

            # Convert Date to datetime
            df['Date'] = pd.to_datetime(df['Date'])

            # Sort by date (oldest first)
            df = df.sort_values('Date', ascending=True).reset_index(drop=True)

            # Keep only required columns
            df = df[['Date', 'Open', 'High', 'Low', 'Close', 'Volume']]

            history[symbol] = df
            logger.info(f"Fetched {len(df)} candles for {symbol}")

        except Exception as e:
            logger.error(f"Failed to fetch historical data for {symbol}: {e}")
            continue

    return history


def calculate_technical_factors(symbols: list[str]) -> dict[str, Any]:
    """
    Calculate momentum and support factors for given symbols.

    Args:
        symbols: List of symbols to analyze (e.g., ["BTC", "ETH", "SOL"])

    Returns:
        Dictionary with structure:
        {
            "momentum": {
                "BTC": {"score": 0.75, "raw": 1.2, "rank": 1},
                "ETH": {"score": 0.62, "raw": 0.8, "rank": 2},
                ...
            },
            "support": {
                "BTC": {"score": 0.68, "raw": 0.5, "rank": 1},
                "ETH": {"score": 0.55, "raw": 0.3, "rank": 2},
                ...
            },
            "recommendations": [
                {"symbol": "BTC", "score": 0.715, "momentum": 0.75, "support": 0.68},
                ...
            ]
        }
    """
    logger.info(f"Calculating technical factors for {len(symbols)} symbols")

    try:
        # Fetch historical data (70 days to satisfy support requirement of window_size+1)
        history = fetch_historical_data(symbols, period="1d", count=70)

        if not history:
            logger.error("No historical data available for analysis")
            return _empty_technical_factors()

        # Calculate momentum factor
        momentum_df = compute_momentum(history)

        # Calculate support factor
        support_df = compute_support(history)

        # Combine results
        result = {
            "momentum": {},
            "support": {},
            "recommendations": []
        }

        # Process momentum results
        if not momentum_df.empty:
            for idx, row in momentum_df.iterrows():
                symbol = row['Symbol']
                result["momentum"][symbol] = {
                    "score": float(row['Momentum Score']),
                    "raw": float(row['Momentum']),
                    "rank": int(idx + 1)
                }

        # Process support results
        if not support_df.empty:
            for idx, row in support_df.iterrows():
                symbol = row['Symbol']
                result["support"][symbol] = {
                    "score": float(row['Support Score']),
                    "raw": float(row['Support']),
                    "rank": int(idx + 1)
                }

        # Generate combined recommendations (average of momentum + support)
        recommendations = []
        for symbol in symbols:
            if symbol in result["momentum"] and symbol in result["support"]:
                momentum_score = result["momentum"][symbol]["score"]
                support_score = result["support"][symbol]["score"]
                combined_score = (momentum_score + support_score) / 2

                recommendations.append({
                    "symbol": symbol,
                    "score": combined_score,
                    "momentum": momentum_score,
                    "support": support_score
                })

        # Sort recommendations by combined score (descending)
        recommendations.sort(key=lambda x: x["score"], reverse=True)
        result["recommendations"] = recommendations

        logger.info(f"Technical analysis completed: {len(recommendations)} symbols ranked")

        # Log top 3 recommendations
        if recommendations:
            logger.info("Top 3 technical signals:")
            for i, rec in enumerate(recommendations[:3], 1):
                logger.info(
                    f"  {i}. {rec['symbol']}: score={rec['score']:.3f} "
                    f"(momentum={rec['momentum']:.3f}, support={rec['support']:.3f})"
                )

        return result

    except Exception as e:
        logger.error(f"Technical analysis failed: {e}", exc_info=True)
        return _empty_technical_factors()


def _empty_technical_factors() -> dict[str, Any]:
    """Return empty technical factors (fallback)"""
    return {
        "momentum": {},
        "support": {},
        "recommendations": []
    }


def format_technical_analysis_for_ai(technical_factors: dict[str, Any]) -> str:
    """
    Format technical analysis results for AI prompt.

    Args:
        technical_factors: Output from calculate_technical_factors()

    Returns:
        Formatted string for AI prompt
    """
    if not technical_factors.get("recommendations"):
        return "No technical analysis available (insufficient historical data)"

    recommendations = technical_factors["recommendations"]

    # Build formatted output
    lines = []
    lines.append("Technical Analysis (Momentum + Support):")
    lines.append("")

    # Show top 5 recommendations
    for i, rec in enumerate(recommendations[:5], 1):
        symbol = rec["symbol"]
        score = rec["score"]
        momentum = rec["momentum"]
        support = rec["support"]

        # Interpret signal strength
        if score >= 0.7:
            signal = "STRONG BUY"
        elif score >= 0.6:
            signal = "BUY"
        elif score >= 0.4:
            signal = "HOLD"
        elif score >= 0.3:
            signal = "SELL"
        else:
            signal = "STRONG SELL"

        lines.append(
            f"{i}. {symbol}: {signal} (score: {score:.2f}, "
            f"momentum: {momentum:.2f}, support: {support:.2f})"
        )

    lines.append("")
    lines.append("Interpretation:")
    lines.append("- Momentum: Measures price trend strength (higher = upward trend)")
    lines.append("- Support: Measures support level strength (higher = strong support)")
    lines.append("- Combined Score > 0.6: Strong buy signal")
    lines.append("- Combined Score < 0.4: Weak signal, consider selling")

    return "\n".join(lines)


def get_top_technical_pick(technical_factors: dict[str, Any]) -> str | None:
    """
    Get the symbol with the best technical signals.

    Args:
        technical_factors: Output from calculate_technical_factors()

    Returns:
        Symbol with highest combined score, or None if no data
    """
    recommendations = technical_factors.get("recommendations", [])

    if not recommendations:
        return None

    # Return top recommendation
    top_pick = recommendations[0]
    logger.info(
        f"Top technical pick: {top_pick['symbol']} "
        f"(score: {top_pick['score']:.3f})"
    )

    return top_pick["symbol"]
