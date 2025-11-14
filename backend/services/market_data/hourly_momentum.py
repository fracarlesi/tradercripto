"""
Hourly Momentum Calculator - WebSocket-based real-time momentum analysis.

This service calculates hourly price momentum using LOCAL CACHE from WebSocket
instead of HTTP API calls. This eliminates rate limiting and provides sub-second latency.

Key Metrics:
- % change in last 1 hour
- Volume (to filter out low-liquidity pumps)
- Price volatility (high/low range)

Architecture:
- Reads from WebSocket local cache (in-memory)
- Zero API calls during calculation (cache pre-populated via WebSocket stream)
- Event-driven: Can be triggered on candle close events

Performance:
- Old: 220 API calls × 20 weight = 4400 weight (~15s, rate limited)
- New: 0 API calls, reads from local cache (~0.5s)
"""

import asyncio
import logging
import time
from typing import Dict, List

from services.market_data.websocket_candle_service import get_websocket_candle_service

logger = logging.getLogger(__name__)


async def calculate_hourly_momentum(
    limit: int = 20,
    min_volume_usd: float = 10000.0,
) -> List[Dict]:
    """
    Calculate hourly momentum for all coins using WebSocket cache.

    This function reads from LOCAL CACHE (in-memory) populated by WebSocket stream.
    Zero API calls = zero rate limiting.

    Args:
        limit: Number of top coins to return (default: 20 for AI analysis)
        min_volume_usd: Minimum hourly volume to filter out illiquid coins

    Returns:
        List of dicts with keys:
        - symbol: Coin symbol (e.g., "BTC", "ETH")
        - momentum_pct: % change in last 1h
        - volume_usd: Hourly volume in USD
        - current_price: Current close price
        - volatility_pct: (high - low) / open * 100 (intraday volatility)
        - momentum_score: Composite score (momentum * volume_weight)

    Example:
        [
            {
                "symbol": "POPCAT",
                "momentum_pct": +7.88,
                "volume_usd": 1160000.0,
                "current_price": 0.0847,
                "volatility_pct": 9.2,
                "momentum_score": 8.12
            },
            ...
        ]
    """

    logger.info("=" * 60)
    logger.info("🚀 Calculating hourly momentum from WebSocket cache")
    logger.info("=" * 60)

    start_time = time.time()

    try:
        # Get WebSocket service (singleton)
        ws_service = get_websocket_candle_service()

        # Get cache stats
        cache_stats = ws_service.get_cache_stats()
        logger.info(
            f"Cache: {cache_stats['symbols_cached']} symbols, "
            f"{cache_stats['total_candles']} candles, "
            f"{cache_stats['memory_mb']} MB"
        )

        if not cache_stats["connected"]:
            logger.warning("⚠️  WebSocket not connected - cache may be stale")

        # Get all subscribed symbols
        all_coins = ws_service.subscribed_symbols

        if not all_coins:
            logger.error("No symbols in WebSocket cache - is service running?")
            return []

        logger.info(f"Analyzing {len(all_coins)} coins from cache...")

        performers = []
        missing_data = 0

        for i, coin in enumerate(all_coins):
            try:
                # Read from local cache (instant, no API call)
                candles = ws_service.get_candles(coin, limit=2)

                if len(candles) < 2:
                    # Not enough data (new coin or WebSocket just started)
                    missing_data += 1
                    continue

                # Get last 2 candles (cache returns most recent first)
                curr_candle = candles[0]  # Most recent (current hour)
                prev_candle = candles[1]  # 1 hour ago

                # Extract OHLCV data
                open_1h_ago = float(prev_candle.get("o", 0))
                current_close = float(curr_candle.get("c", 0))
                current_high = float(curr_candle.get("h", 0))
                current_low = float(curr_candle.get("l", 0))
                current_volume = float(curr_candle.get("v", 0))

                # Calculate momentum (% change from 1h ago to now)
                if open_1h_ago == 0:
                    continue

                momentum_pct = ((current_close - open_1h_ago) / open_1h_ago) * 100

                # Calculate volume in USD
                volume_usd = current_volume * current_close

                # Skip low-volume coins (illiquid pumps)
                if volume_usd < min_volume_usd:
                    continue

                # Calculate volatility (intraday range)
                volatility_pct = ((current_high - current_low) / open_1h_ago * 100) if open_1h_ago > 0 else 0

                # Calculate composite momentum score
                # Higher volume = more reliable momentum
                volume_weight = min(volume_usd / 100000.0, 10.0)  # Cap at 10x weight
                momentum_score = momentum_pct * (1 + volume_weight / 10)

                performers.append({
                    "symbol": coin,
                    "momentum_pct": momentum_pct,
                    "volume_usd": volume_usd,
                    "current_price": current_close,
                    "volatility_pct": volatility_pct,
                    "momentum_score": momentum_score,
                })

                # Progress logging (every 50 coins)
                if (i + 1) % 50 == 0:
                    logger.debug(f"Progress: {i+1}/{len(all_coins)} coins analyzed...")

            except Exception as e:
                logger.warning(f"Error analyzing {coin}: {str(e)[:50]}")
                continue

        if missing_data > 0:
            logger.warning(f"Missing cache data for {missing_data}/{len(all_coins)} coins (WebSocket warming up?)")

        # Sort by momentum score (descending)
        performers.sort(key=lambda x: x["momentum_score"], reverse=True)

        # Get top N
        top_performers = performers[:limit]

        elapsed = time.time() - start_time

        logger.info(f"✅ Analyzed {len(performers)}/{len(all_coins)} coins in {elapsed:.1f}s (from cache)")
        logger.info(f"📊 Top {len(top_performers)} performers by momentum:")

        for i, p in enumerate(top_performers[:5], 1):
            logger.info(
                f"  {i}. {p['symbol']}: {p['momentum_pct']:+.2f}% "
                f"(vol: ${p['volume_usd']:,.0f}, score: {p['momentum_score']:.2f})"
            )

        if len(top_performers) > 5:
            logger.info(f"  ... and {len(top_performers) - 5} more")

        return top_performers

    except Exception as e:
        logger.error(
            f"Failed to calculate hourly momentum: {e}",
            exc_info=True
        )
        return []


async def get_top_momentum_symbols(limit: int = 20) -> List[str]:
    """
    Convenience function to get just the symbol names of top performers.

    This is used for pre-filtering before technical analysis.

    Args:
        limit: Number of symbols to return

    Returns:
        List of symbol names (e.g., ["BTC", "ETH", "POPCAT", ...])
    """

    performers = await calculate_hourly_momentum(limit=limit)
    return [p["symbol"] for p in performers]


# Sync wrapper for non-async contexts
def calculate_hourly_momentum_sync(limit: int = 20) -> List[Dict]:
    """Synchronous wrapper for calculate_hourly_momentum()."""
    return asyncio.run(calculate_hourly_momentum(limit=limit))
