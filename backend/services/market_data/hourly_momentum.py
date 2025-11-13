"""
Hourly Momentum Calculator - Fast momentum-based trading analysis.

This service calculates hourly price momentum for all available coins
and returns top performers to feed into AI decision-making.

Key Metrics:
- % change in last 1 hour
- Volume (to filter out low-liquidity pumps)
- Price volatility (high/low range)

Philosophy:
Instead of predicting future trends with Prophet/daily analysis,
we surf existing momentum in real-time.
"""

import asyncio
import logging
import time
from typing import Dict, List

from hyperliquid.info import Info
from hyperliquid.utils import constants

logger = logging.getLogger(__name__)


async def calculate_hourly_momentum(
    limit: int = 20,
    min_volume_usd: float = 10000.0,
) -> List[Dict]:
    """
    Calculate hourly momentum for all available coins on Hyperliquid.

    Returns top N coins with highest momentum (% gain in last hour).

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
    logger.info("🚀 Calculating hourly momentum for all coins")
    logger.info("=" * 60)

    start_time = time.time()

    try:
        # Initialize Hyperliquid Info API
        info = Info(constants.MAINNET_API_URL, skip_ws=True)

        # Get all available coins
        meta = info.meta()
        all_coins = [asset["name"] for asset in meta["universe"]]

        logger.info(f"Analyzing {len(all_coins)} coins for hourly momentum...")

        # Calculate end time (now) and start time (2 hours ago)
        end_time_ms = int(time.time() * 1000)
        start_time_ms = end_time_ms - (2 * 60 * 60 * 1000)  # 2 hours of data

        performers = []
        errors = 0

        for i, coin in enumerate(all_coins):
            try:
                # Fetch 2 hours of 1h candles (should give us 2 candles: [-2h, -1h], [-1h, now])
                candles = info.candles_snapshot(
                    name=coin,
                    interval="1h",
                    startTime=start_time_ms,
                    endTime=end_time_ms
                )

                if len(candles) < 2:
                    # Not enough data (new coin or low activity)
                    continue

                # Get last 2 candles
                prev_candle = candles[-2]  # 1 hour ago
                curr_candle = candles[-1]  # Current hour

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

                # Rate limiting: Small delay every 10 requests to avoid 429
                if (i + 1) % 10 == 0:
                    await asyncio.sleep(0.1)

            except Exception as e:
                errors += 1
                if errors <= 3:
                    logger.warning(f"Error analyzing {coin}: {str(e)[:50]}")
                continue

        if errors > 3:
            logger.warning(f"... and {errors - 3} more errors (rate limiting or missing data)")

        # Sort by momentum score (descending)
        performers.sort(key=lambda x: x["momentum_score"], reverse=True)

        # Get top N
        top_performers = performers[:limit]

        elapsed = time.time() - start_time

        logger.info(f"✅ Analyzed {len(performers)}/{len(all_coins)} coins in {elapsed:.1f}s")
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
