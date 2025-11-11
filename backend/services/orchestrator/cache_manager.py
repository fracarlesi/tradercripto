"""
Unified Cache Manager - Centralized caching for all microservices.

Provides:
- Per-service cache with configurable TTLs
- Batch get/set operations
- Cache invalidation
- Cache statistics

Cache Layers (TTLs):
- Prices: 30s (volatile)
- Technical Analysis: 180s (recalculate every cycle)
- Pivot Points: 3600s (1 hour, stable)
- Prophet Forecasts: 86400s (24 hours, expensive)
- Sentiment: 300s (5 minutes)
- Whale Alerts: 60s (1 minute, near real-time)
- News: 3600s (1 hour)
"""

import logging
import time
from typing import Any, Dict, List, Optional, TypeVar
from threading import Lock

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CacheEntry:
    """Single cache entry with expiration."""

    def __init__(self, value: Any, ttl: int):
        """
        Initialize cache entry.

        Args:
            value: Cached value
            ttl: Time-to-live in seconds
        """
        self.value = value
        self.expires_at = time.time() + ttl
        self.created_at = time.time()

    def is_expired(self) -> bool:
        """Check if entry has expired."""
        return time.time() > self.expires_at

    def age_seconds(self) -> float:
        """Get age of cache entry in seconds."""
        return time.time() - self.created_at


class CacheManager:
    """
    Unified cache manager for all microservices.

    Thread-safe with locking.

    Usage:
        cache = CacheManager()

        # Set single value
        cache.set("prices", "BTC", 102450.0, ttl=30)

        # Get single value
        price = cache.get("prices", "BTC")

        # Batch operations
        prices = cache.get_batch("prices", ["BTC", "ETH", "SOL"])
        cache.set_batch("pivot_points", pivot_data, ttl=3600)
    """

    # Default TTLs for each service (in seconds)
    DEFAULT_TTLS = {
        "prices": 30,  # 30 seconds (volatile)
        "technical_analysis": 180,  # 3 minutes (recalculate every cycle)
        "pivot_points": 3600,  # 1 hour (stable)
        "prophet_forecasts": 86400,  # 24 hours (expensive)
        "sentiment": 300,  # 5 minutes
        "whale_alerts": 60,  # 1 minute (near real-time)
        "news": 3600,  # 1 hour
    }

    def __init__(self):
        """Initialize empty cache with locks."""
        self._cache: Dict[str, Dict[str, CacheEntry]] = {}
        self._lock = Lock()
        self._stats = {
            "hits": 0,
            "misses": 0,
            "sets": 0,
            "expirations": 0,
        }

    def get(self, namespace: str, key: str) -> Optional[Any]:
        """
        Get single value from cache.

        Args:
            namespace: Cache namespace (e.g., "prices", "pivot_points")
            key: Cache key (e.g., "BTC")

        Returns:
            Cached value if present and not expired, None otherwise
        """
        with self._lock:
            if namespace not in self._cache:
                self._stats["misses"] += 1
                return None

            entry = self._cache[namespace].get(key)
            if not entry:
                self._stats["misses"] += 1
                return None

            # Check expiration
            if entry.is_expired():
                logger.debug(f"Cache expired: {namespace}/{key} (age: {entry.age_seconds():.1f}s)")
                del self._cache[namespace][key]
                self._stats["expirations"] += 1
                self._stats["misses"] += 1
                return None

            self._stats["hits"] += 1
            logger.debug(f"Cache hit: {namespace}/{key} (age: {entry.age_seconds():.1f}s)")
            return entry.value

    def get_batch(self, namespace: str, keys: List[str]) -> Dict[str, Any]:
        """
        Get multiple values from cache.

        Args:
            namespace: Cache namespace
            keys: List of cache keys

        Returns:
            Dictionary mapping key to value (only for present, non-expired entries)

        Example:
            >>> cache.get_batch("prices", ["BTC", "ETH", "SOL"])
            {"BTC": 102450.0, "ETH": 3850.5}  # SOL not in cache
        """
        results = {}
        for key in keys:
            value = self.get(namespace, key)
            if value is not None:
                results[key] = value
        return results

    def set(self, namespace: str, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """
        Set single value in cache.

        Args:
            namespace: Cache namespace
            key: Cache key
            value: Value to cache
            ttl: Time-to-live in seconds (uses default if not provided)
        """
        if ttl is None:
            ttl = self.DEFAULT_TTLS.get(namespace, 300)  # Default 5 minutes

        with self._lock:
            if namespace not in self._cache:
                self._cache[namespace] = {}

            self._cache[namespace][key] = CacheEntry(value, ttl)
            self._stats["sets"] += 1
            logger.debug(f"Cache set: {namespace}/{key} (TTL: {ttl}s)")

    def set_batch(self, namespace: str, items: Dict[str, Any], ttl: Optional[int] = None) -> None:
        """
        Set multiple values in cache.

        Args:
            namespace: Cache namespace
            items: Dictionary mapping key to value
            ttl: Time-to-live in seconds (uses default if not provided)

        Example:
            >>> cache.set_batch("prices", {"BTC": 102450.0, "ETH": 3850.5}, ttl=30)
        """
        for key, value in items.items():
            self.set(namespace, key, value, ttl=ttl)

    def invalidate(self, namespace: str, key: Optional[str] = None) -> None:
        """
        Invalidate cache entries.

        Args:
            namespace: Cache namespace to invalidate
            key: Specific key to invalidate (if None, invalidate entire namespace)

        Example:
            >>> cache.invalidate("prices", "BTC")  # Invalidate single key
            >>> cache.invalidate("prices")  # Invalidate all prices
        """
        with self._lock:
            if namespace not in self._cache:
                return

            if key is None:
                # Invalidate entire namespace
                count = len(self._cache[namespace])
                del self._cache[namespace]
                logger.info(f"Invalidated {count} entries in namespace: {namespace}")
            else:
                # Invalidate single key
                if key in self._cache[namespace]:
                    del self._cache[namespace][key]
                    logger.debug(f"Invalidated cache: {namespace}/{key}")

    def clear_expired(self) -> int:
        """
        Remove all expired entries from cache.

        Returns:
            Number of entries removed

        This should be called periodically (e.g., every 2 minutes) to prevent memory bloat.
        """
        removed_count = 0
        with self._lock:
            for namespace in list(self._cache.keys()):
                expired_keys = [
                    key for key, entry in self._cache[namespace].items() if entry.is_expired()
                ]

                for key in expired_keys:
                    del self._cache[namespace][key]
                    removed_count += 1

                # Remove empty namespaces
                if not self._cache[namespace]:
                    del self._cache[namespace]

        if removed_count > 0:
            logger.info(f"Cleared {removed_count} expired cache entries")

        return removed_count

    def get_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics.

        Returns:
            Dictionary with cache stats

        Example:
            >>> cache.get_stats()
            {
                "hits": 150,
                "misses": 50,
                "sets": 200,
                "expirations": 20,
                "hit_rate": 0.75,
                "total_entries": 180,
                "namespaces": {
                    "prices": 468,
                    "pivot_points": 142,
                    ...
                }
            }
        """
        with self._lock:
            # Calculate hit rate
            total_requests = self._stats["hits"] + self._stats["misses"]
            hit_rate = self._stats["hits"] / total_requests if total_requests > 0 else 0.0

            # Count entries per namespace
            namespaces = {
                namespace: len(entries) for namespace, entries in self._cache.items()
            }

            total_entries = sum(namespaces.values())

            return {
                "hits": self._stats["hits"],
                "misses": self._stats["misses"],
                "sets": self._stats["sets"],
                "expirations": self._stats["expirations"],
                "hit_rate": round(hit_rate, 3),
                "total_entries": total_entries,
                "namespaces": namespaces,
            }

    def reset_stats(self) -> None:
        """Reset cache statistics."""
        with self._lock:
            self._stats = {
                "hits": 0,
                "misses": 0,
                "sets": 0,
                "expirations": 0,
            }
            logger.info("Cache statistics reset")


# Global singleton instance
_cache_manager: Optional[CacheManager] = None
_cache_lock = Lock()


def get_cache_manager() -> CacheManager:
    """
    Get global cache manager instance (singleton).

    Returns:
        Global CacheManager instance
    """
    global _cache_manager

    if _cache_manager is None:
        with _cache_lock:
            if _cache_manager is None:  # Double-check locking
                _cache_manager = CacheManager()
                logger.info("Initialized global cache manager")

    return _cache_manager
