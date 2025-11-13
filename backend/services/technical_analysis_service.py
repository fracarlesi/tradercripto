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

import pandas as pd

from factors.momentum import compute_momentum
from factors.support import compute_support
from services.market_data.hyperliquid_market_data import get_kline_data_from_hyperliquid

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


def fetch_historical_data(symbols: list[str], period: str = "1d", count: int = 70) -> dict[str, pd.DataFrame]:
    """
    Fetch historical OHLCV data for multiple symbols in parallel.

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

    def fetch_single_symbol(symbol: str) -> tuple[str, pd.DataFrame | None]:
        """
        Helper function to fetch data for a single symbol with retry logic.

        Implements exponential backoff for transient API failures.
        Distinguishes between "no data available" (skip) vs "API error" (retry).

        Returns:
            Tuple of (symbol, DataFrame) or (symbol, None) if failed/no data
        """
        last_exception = None
        retry_delay = RETRY_DELAY

        for attempt in range(MAX_RETRIES + 1):
            try:
                # Fetch klines from Hyperliquid
                klines = get_kline_data_from_hyperliquid(symbol, period=period, count=count)

                # Check if we got data
                if not klines or len(klines) < 2:
                    # No data available - this is expected for some symbols (newly listed, delisted, etc.)
                    # Don't retry, just log once and skip
                    if attempt == 0:  # Only log on first attempt
                        logger.debug(f"No historical data for {symbol} (0 candles) - skipping")
                    return symbol, None

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

                logger.info(f"✅ Fetched {len(df)} candles for {symbol}")

                # Add delay between requests to avoid rate limiting
                # Applied with all worker configurations to prevent 429 errors
                time.sleep(REQUEST_DELAY)

                return symbol, df

            except Exception as e:
                last_exception = e

                # Check if this is a retryable error (including 429 rate limiting)
                error_str = str(e).lower()
                error_type = str(type(e).__name__).lower()

                is_retryable = (
                    any(err_type in error_type for err_type in ['timeout', 'connection', 'http', 'network'])
                    or '429' in error_str  # Retry on rate limiting errors
                )

                if is_retryable and attempt < MAX_RETRIES:
                    # Retry with exponential backoff
                    logger.warning(
                        f"Retryable error fetching {symbol} (attempt {attempt + 1}/{MAX_RETRIES + 1}): "
                        f"{type(e).__name__} - {str(e)[:100]}. Retrying in {retry_delay:.1f}s..."
                    )
                    time.sleep(retry_delay)
                    retry_delay *= RETRY_BACKOFF
                else:
                    # Non-retryable error or max retries reached
                    logger.error(
                        f"Failed to fetch {symbol} after {attempt + 1} attempts: {e}",
                        exc_info=(attempt == MAX_RETRIES)  # Full traceback only on last attempt
                    )
                    return symbol, None

        # Should never reach here, but just in case
        logger.error(f"Unexpected: Failed to fetch {symbol} after all retries")
        return symbol, None

    # Parallel execution with ThreadPoolExecutor
    logger.info(f"Fetching historical data for {len(symbols)} symbols using {MAX_WORKERS} parallel workers")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Submit all fetch tasks
        future_to_symbol = {executor.submit(fetch_single_symbol, symbol): symbol
                           for symbol in symbols}

        # Collect results as they complete
        for future in as_completed(future_to_symbol):
            symbol, df = future.result()
            if df is not None:
                history[symbol] = df

    # Log detailed summary
    success_rate = (len(history) / len(symbols) * 100) if symbols else 0
    logger.info(
        f"✅ Successfully fetched data for {len(history)}/{len(symbols)} symbols ({success_rate:.1f}%)"
    )

    # Log sample of successful symbols for debugging
    if history:
        successful_symbols = list(history.keys())[:10]
        logger.info(f"Sample successful symbols: {', '.join(successful_symbols)}")

    # Note: Symbols without data are expected (newly listed, delisted, or perpetual contracts like kPEPE)
    # These are automatically filtered out - only symbols with 70+ days of historical data are analyzed
    logger.debug(f"Skipped {len(symbols) - len(history)} symbols (no historical data available)")

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
        # Fetch historical data (24 hours for hourly momentum trading)
        history = fetch_historical_data(symbols, period="1h", count=24)

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
