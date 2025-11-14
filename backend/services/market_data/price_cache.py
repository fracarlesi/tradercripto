"""
Price caching service to reduce API calls and improve performance

Now integrated with WebSocket service for real-time prices from allMids subscription.
"""

import logging
import time
from threading import Lock
from typing import Any, Optional

logger = logging.getLogger(__name__)


class PriceCache:
    """Simple in-memory price cache with TTL"""

    def __init__(self, ttl_seconds: int = 30) -> None:
        self.cache: dict[
            tuple[str, str], tuple[float, float]
        ] = {}  # key: (symbol, market), value: (price, timestamp)
        self.ttl_seconds = ttl_seconds
        self.lock = Lock()

    def get(self, symbol: str, market: str) -> float | None:
        """Get cached price if still valid"""
        key = (symbol, market)
        current_time = time.time()

        with self.lock:
            if key in self.cache:
                price, timestamp = self.cache[key]
                if current_time - timestamp < self.ttl_seconds:
                    logger.debug(f"Cache hit for {symbol}.{market}: {price}")
                    return price
                else:
                    # Remove expired entry
                    del self.cache[key]
                    logger.debug(f"Cache expired for {symbol}.{market}")

        return None

    def set(self, symbol: str, market: str, price: float) -> None:
        """Cache a price with current timestamp"""
        key = (symbol, market)
        current_time = time.time()

        with self.lock:
            self.cache[key] = (price, current_time)
            logger.debug(f"Cached price for {symbol}.{market}: {price}")

    def clear_expired(self) -> None:
        """Remove all expired entries"""
        current_time = time.time()
        expired_keys = []

        with self.lock:
            for key, (price, timestamp) in self.cache.items():
                if current_time - timestamp >= self.ttl_seconds:
                    expired_keys.append(key)

            for key in expired_keys:
                del self.cache[key]

        if expired_keys:
            logger.debug(f"Cleared {len(expired_keys)} expired cache entries")

    def get_cache_stats(self) -> dict[str, Any]:
        """Get cache statistics"""
        current_time = time.time()
        total_entries = 0
        valid_entries = 0

        with self.lock:
            total_entries = len(self.cache)
            for price, timestamp in self.cache.values():
                if current_time - timestamp < self.ttl_seconds:
                    valid_entries += 1

        return {
            "total_entries": total_entries,
            "valid_entries": valid_entries,
            "ttl_seconds": self.ttl_seconds,
        }


# Global price cache instance
price_cache = PriceCache(ttl_seconds=30)  # Cache prices for 30 seconds


def get_cached_price(symbol: str, market: str = "CRYPTO") -> Optional[float]:
    """
    Get price from WebSocket cache (real-time) or fallback to local cache.

    Priority:
    1. WebSocket allMids cache (real-time, zero API calls)
    2. Local TTL cache (fallback for non-WebSocket scenarios)

    Args:
        symbol: Symbol to get price for (e.g., "BTC")
        market: Market identifier (default "CRYPTO", kept for backward compatibility)

    Returns:
        Price as float or None if not available
    """
    # Try WebSocket cache first (real-time prices from allMids)
    try:
        from services.market_data.websocket_candle_service import get_websocket_candle_service

        ws_service = get_websocket_candle_service()
        ws_price = ws_service.get_price(symbol)

        if ws_price is not None:
            logger.debug(f"WebSocket price hit for {symbol}: ${ws_price:.2f}")
            return ws_price
        else:
            logger.debug(f"No WebSocket price for {symbol}, trying local cache")
    except Exception as e:
        logger.warning(f"WebSocket price lookup failed for {symbol}: {e}")

    # Fallback to local TTL cache
    local_price = price_cache.get(symbol, market)
    if local_price is not None:
        logger.debug(f"Local cache hit for {symbol}.{market}: ${local_price:.2f}")

    return local_price


def cache_price(symbol: str, market: str, price: float) -> None:
    """Cache a price"""
    price_cache.set(symbol, market, price)


def clear_expired_prices() -> None:
    """Clear expired price entries"""
    price_cache.clear_expired()


def get_price_cache_stats() -> dict[str, Any]:
    """Get cache statistics"""
    return price_cache.get_cache_stats()
