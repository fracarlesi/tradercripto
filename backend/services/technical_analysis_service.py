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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from datetime import datetime

import pandas as pd

from factors.momentum import compute_momentum
from factors.support import compute_support
from services.market_data.hyperliquid_market_data import get_kline_data_from_hyperliquid
from services.market_data.websocket_candle_service import get_websocket_candle_service

logger = logging.getLogger(__name__)

# Number of parallel workers for fetching kline data
# CRITICAL: Balanced configuration to optimize speed while avoiding rate limiting
# Testing showed:
#   - 10 workers → massive 429 errors
#   - 3 workers (no delay) → still getting 429 errors (~156 requests per batch)
#   - 1 worker (sequential) → ZERO 429 errors but slow (~7 min for 222 symbols)
#   - 2 workers (250ms delay) → OPTIMIZED for speed (~3.5 min) with low 429 risk
# Trade-off: 2 workers = 2x faster than sequential, minimal rate limit risk
MAX_WORKERS = 2

# Retry configuration for transient API failures
MAX_RETRIES = 2  # Retry failed fetches up to 2 times
RETRY_DELAY = 1.0  # Initial delay between retries (seconds)
RETRY_BACKOFF = 1.5  # Exponential backoff multiplier

# Rate limiting: Add small delay between requests to avoid API throttling
REQUEST_DELAY = 0.50  # 500ms delay between requests (increased to eliminate all 429 errors)


def fetch_historical_data(symbols: list[str], period: str = "1h", count: int = 70) -> dict[str, pd.DataFrame]:
    """
    Fetch historical OHLCV data from WebSocket cache (ZERO API calls).

    This function reads from the local WebSocket candle cache populated by
    the persistent WebSocket connection. Zero rate limiting, sub-second latency.

    Args:
        symbols: List of symbols to fetch (e.g., ["BTC", "ETH", "SOL"])
        period: Timeframe for candles (default: "1h" hourly, ONLY 1h supported by WebSocket)
        count: Number of candles to fetch (default: 70, max available in cache)

    Returns:
        Dictionary mapping symbol to DataFrame with columns:
        - Date: datetime
        - Open, High, Low, Close: float
        - Volume: float
    """
    history = {}

    # Get WebSocket candle service (singleton)
    ws_service = get_websocket_candle_service()

    logger.info(f"Fetching historical data for {len(symbols)} symbols from WebSocket cache (0 API calls)")

    def fetch_single_symbol_from_cache(symbol: str) -> tuple[str, pd.DataFrame | None]:
        """
        Helper function to fetch data for a single symbol from WebSocket cache.

        Reads from local in-memory cache - instant, no API calls, no rate limiting.

        Returns:
            Tuple of (symbol, DataFrame) or (symbol, None) if no data in cache
        """
        try:
            # Read candles from WebSocket cache (INSTANT, no API call!)
            candles = ws_service.get_candles(symbol, limit=count)

            # Check if we got data
            if not candles or len(candles) < 2:
                logger.debug(f"No cached data for {symbol} (<2 candles) - WebSocket warming up?")
                return symbol, None

            # Convert to DataFrame
            # WebSocket candles format: {"t": timestamp_ms, "o": open, "h": high, "l": low, "c": close, "v": volume}
            df_data = []
            for candle in candles:
                df_data.append({
                    'Date': datetime.fromtimestamp(candle['t'] / 1000),  # Convert ms to datetime
                    'Open': float(candle['o']),
                    'High': float(candle['h']),
                    'Low': float(candle['l']),
                    'Close': float(candle['c']),
                    'Volume': float(candle['v'])
                })

            df = pd.DataFrame(df_data)

            # Sort by date (oldest first) - WebSocket returns newest first
            df = df.sort_values('Date', ascending=True).reset_index(drop=True)

            logger.debug(f"✅ Got {len(df)} candles for {symbol} from cache")

            return symbol, df

        except Exception as e:
            logger.error(f"Failed to read {symbol} from WebSocket cache: {e}", exc_info=True)
            return symbol, None

    # Read from cache (instant, no threading needed)
    # WebSocket cache reads are already thread-safe and sub-millisecond
    start_time = time.time()

    for symbol in symbols:
        symbol_name, df = fetch_single_symbol_from_cache(symbol)
        if df is not None:
            history[symbol_name] = df

    elapsed = time.time() - start_time

    # Log detailed summary
    success_rate = (len(history) / len(symbols) * 100) if symbols else 0
    logger.info(
        f"✅ Fetched data for {len(history)}/{len(symbols)} symbols ({success_rate:.1f}%) "
        f"in {elapsed:.2f}s from WebSocket cache"
    )

    # Log sample of successful symbols for debugging
    if history:
        successful_symbols = list(history.keys())[:10]
        logger.info(f"Sample successful symbols: {', '.join(successful_symbols)}")

    # Note: Symbols without cached data means WebSocket is warming up or symbol not subscribed
    if len(history) < len(symbols):
        logger.warning(
            f"⚠️ Missing cache data for {len(symbols) - len(history)} symbols. "
            f"WebSocket may still be warming up or symbols not subscribed."
        )

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
        # Fetch historical data from WebSocket cache (70 candles for RSI/MACD precision)
        history = fetch_historical_data(symbols, period="1h", count=70)

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


def get_technical_analysis_structured(symbols: list[str]) -> dict[str, dict]:
    """
    Calculate technical analysis and return structured dict for JSON builder.

    This is the NEW API that returns data in the format expected by
    MarketDataBuilder for the orchestrator system.

    Args:
        symbols: List of symbols to analyze

    Returns:
        Dictionary mapping symbol to technical analysis data:
        {
            "BTC": {
                "score": 0.75,
                "momentum": 0.72,
                "support": 0.78,
                "signal": "BUY",
                "rank": 1
            },
            "ETH": {
                "score": 0.68,
                "momentum": 0.65,
                "support": 0.71,
                "signal": "BUY",
                "rank": 2
            },
            ...
        }

    Note:
        - Only symbols with valid data (70+ days history) are included
        - Symbols are ranked by combined score (best = rank 1)
        - Signal is determined by score: >0.7=STRONG_BUY, >0.6=BUY, etc.

    Example:
        >>> technical = get_technical_analysis_structured(["BTC", "ETH", "SOL"])
        >>> btc_score = technical["BTC"]["score"]
        >>> btc_signal = technical["BTC"]["signal"]
    """
    logger.info(f"[STRUCTURED API] Calculating technical analysis for {len(symbols)} symbols")

    # Use existing analysis function
    raw_result = calculate_technical_factors(symbols)

    # Convert to structured format
    structured = {}

    for idx, rec in enumerate(raw_result.get("recommendations", []), start=1):
        symbol = rec["symbol"]
        score = rec["score"]

        # Determine signal based on score
        if score >= 0.7:
            signal = "STRONG_BUY"
        elif score >= 0.6:
            signal = "BUY"
        elif score >= 0.4:
            signal = "HOLD"
        elif score >= 0.3:
            signal = "SELL"
        else:
            signal = "STRONG_SELL"

        structured[symbol] = {
            "score": round(score, 4),
            "momentum": round(rec["momentum"], 4),
            "support": round(rec["support"], 4),
            "signal": signal,
            "rank": idx
        }

    logger.info(
        f"[STRUCTURED API] Completed: {len(structured)} symbols with technical data"
    )

    # Log top 3 for debugging
    if structured:
        top_3 = sorted(structured.items(), key=lambda x: x[1]["rank"])[:3]
        logger.info("[STRUCTURED API] Top 3 signals:")
        for symbol, data in top_3:
            logger.info(
                f"  {data['rank']}. {symbol}: {data['signal']} "
                f"(score={data['score']:.3f})"
            )

    return structured


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
