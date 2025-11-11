"""
News Feed Cache (T090)

Implements caching for news feed data to reduce API calls from every 3 minutes
to every 60 minutes, achieving a 20x reduction in external API requests.

Features:
- TTL-based caching (default: 1 hour, configurable)
- Timestamp validation for cache freshness
- Thread-safe cache access
- Cache hit/miss tracking for metrics
"""

import logging
import time
from collections.abc import Callable
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)


class NewsFeedCache:
    """
    Cache for news feed data with TTL-based expiration.

    Reduces external API calls by caching news content for a configurable
    time period (default: 1 hour).

    Example:
        >>> cache = NewsFeedCache(ttl_seconds=3600)  # 1 hour TTL
        >>> news = cache.get_news(fetch_func=fetch_latest_news)
        >>> # First call fetches from API, subsequent calls return cached data
        >>> news = cache.get_news(fetch_func=fetch_latest_news)  # Cache hit
    """

    def __init__(self, ttl_seconds: int = 3600):
        """
        Initialize news feed cache.

        Args:
            ttl_seconds: Time-to-live in seconds (default: 3600 = 1 hour)
        """
        self.ttl_seconds = ttl_seconds
        self._cached_news: str | None = None
        self._cache_timestamp: float | None = None
        self._lock = Lock()

        # Metrics
        self._cache_hits = 0
        self._cache_misses = 0

        logger.info(
            f"NewsFeedCache initialized with TTL={ttl_seconds}s ({ttl_seconds / 60:.1f} minutes)"
        )

    def get_news(self, fetch_func: Callable[[], str]) -> str:
        """
        Get news from cache or fetch fresh data if cache is expired.

        This method checks if cached news is still valid based on TTL.
        If valid, returns cached data (cache hit). If expired or missing,
        calls fetch_func to get fresh data (cache miss).

        Args:
            fetch_func: Function to call when cache miss occurs (no arguments)
                       Note: Configure max_chars in the lambda when calling,
                       e.g., lambda: fetch_latest_news(max_chars=5000)

        Returns:
            News content as string (cached or fresh)

        Example:
            >>> from services.news_feed import fetch_latest_news
            >>> cache = NewsFeedCache()
            >>> news = cache.get_news(fetch_func=lambda: fetch_latest_news(max_chars=5000))
        """
        with self._lock:
            # Check if cache is valid
            if self._is_cache_valid():
                self._cache_hits += 1
                # Type narrowing: if _is_cache_valid() is True, timestamp is not None
                assert self._cache_timestamp is not None
                assert self._cached_news is not None
                cache_age_seconds = time.time() - self._cache_timestamp
                logger.debug(
                    f"News cache HIT (age: {cache_age_seconds:.0f}s, "
                    f"hits: {self._cache_hits}, misses: {self._cache_misses})"
                )
                return self._cached_news

            # Cache miss - fetch fresh data
            self._cache_misses += 1
            logger.info(f"News cache MISS (hits: {self._cache_hits}, misses: {self._cache_misses})")

            # Fetch fresh news
            try:
                # fetch_func is already configured with max_chars (via lambda in news_feed.py)
                fresh_news = fetch_func() if callable(fetch_func) else fetch_func

                # Update cache
                self._cached_news = fresh_news
                self._cache_timestamp = time.time()

                logger.info(
                    f"News cache updated: {len(fresh_news)} chars, "
                    f"next refresh in {self.ttl_seconds}s"
                )

                return fresh_news

            except Exception as err:
                logger.error(f"Failed to fetch fresh news: {err}", exc_info=True)

                # Return stale cache if available, or empty string
                if self._cached_news is not None:
                    logger.warning("Returning stale cached news due to fetch failure")
                    return self._cached_news

                return ""

    def _is_cache_valid(self) -> bool:
        """
        Check if cached news is still valid based on TTL.

        Returns:
            True if cache is valid (not expired), False otherwise
        """
        if self._cached_news is None or self._cache_timestamp is None:
            return False

        cache_age = time.time() - self._cache_timestamp
        return cache_age < self.ttl_seconds

    def invalidate(self) -> None:
        """
        Manually invalidate the cache, forcing next get_news() to fetch fresh data.

        Useful for testing or when you know news should be refreshed immediately.
        """
        with self._lock:
            logger.info("News cache manually invalidated")
            self._cached_news = None
            self._cache_timestamp = None

    def get_cache_age_seconds(self) -> float | None:
        """
        Get the age of cached data in seconds.

        Returns:
            Age in seconds, or None if no cached data
        """
        if self._cache_timestamp is None:
            return None

        return time.time() - self._cache_timestamp

    def get_cache_stats(self) -> dict[str, Any]:
        """
        Get cache statistics for monitoring.

        Returns:
            Dictionary with cache metrics:
            - hits: Number of cache hits
            - misses: Number of cache misses
            - hit_rate: Percentage of cache hits (0-100)
            - age_seconds: Age of cached data in seconds (or None)
            - ttl_seconds: Configured TTL
        """
        total_requests = self._cache_hits + self._cache_misses
        hit_rate = (self._cache_hits / total_requests * 100) if total_requests > 0 else 0.0

        return {
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "total_requests": total_requests,
            "hit_rate": round(hit_rate, 2),
            "age_seconds": self.get_cache_age_seconds(),
            "ttl_seconds": self.ttl_seconds,
            "is_valid": self._is_cache_valid(),
        }

    def reset_stats(self) -> None:
        """Reset cache statistics counters."""
        with self._lock:
            self._cache_hits = 0
            self._cache_misses = 0
            logger.info("News cache statistics reset")


# Global cache instance (singleton pattern)
# Default TTL: 1 hour (3600 seconds)
_global_cache: NewsFeedCache | None = None


def get_news_cache(ttl_seconds: int = 3600) -> NewsFeedCache:
    """
    Get the global news cache instance (singleton).

    Args:
        ttl_seconds: TTL for cache if creating new instance (default: 3600)

    Returns:
        Global NewsFeedCache instance

    Example:
        >>> cache = get_news_cache(ttl_seconds=7200)  # 2 hours
        >>> news = cache.get_news(fetch_func=fetch_latest_news)
    """
    global _global_cache

    if _global_cache is None:
        _global_cache = NewsFeedCache(ttl_seconds=ttl_seconds)
        logger.info(f"Global news cache instance created with TTL={ttl_seconds}s")

    return _global_cache
