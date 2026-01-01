"""
Token Bucket Rate Limiter
=========================

Async rate limiter using token bucket algorithm.
Implements Hyperliquid rate limits:
- Orders: 10 requests/second
- Info API: ~100 requests/minute
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TokenBucket:
    """
    Token bucket rate limiter.

    Tokens are added at a constant rate up to a maximum capacity.
    Each request consumes one token. If no tokens are available,
    the request waits until a token becomes available.

    Args:
        rate: Number of tokens added per second
        capacity: Maximum number of tokens in the bucket
    """
    rate: float
    capacity: float
    tokens: float = field(init=False)
    last_update: float = field(init=False)
    _lock: asyncio.Lock = field(init=False, repr=False)

    def __post_init__(self):
        self.tokens = self.capacity
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self.last_update
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_update = now

    async def acquire(self, tokens: float = 1.0) -> float:
        """
        Acquire tokens from the bucket.

        Waits if necessary until tokens are available.

        Args:
            tokens: Number of tokens to acquire (default: 1)

        Returns:
            Time waited in seconds
        """
        async with self._lock:
            self._refill()

            if self.tokens >= tokens:
                self.tokens -= tokens
                return 0.0

            # Calculate wait time
            tokens_needed = tokens - self.tokens
            wait_time = tokens_needed / self.rate

            # Wait for tokens
            await asyncio.sleep(wait_time)

            # Update after waiting
            self._refill()
            self.tokens -= tokens
            return wait_time

    def available(self) -> float:
        """Return currently available tokens."""
        self._refill()
        return self.tokens

    async def __aenter__(self):
        """Context manager entry - acquires one token."""
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        pass


class HyperliquidRateLimiter:
    """
    Rate limiter configured for Hyperliquid API limits.

    Hyperliquid Rate Limits:
    - Orders: 10 requests/second (burst allowed)
    - Info API: 100 requests/minute (~1.67/second)
    - WebSocket: No explicit limit but respect reasonable usage

    Usage:
        limiter = HyperliquidRateLimiter()

        # For order operations
        async with limiter.orders:
            await client.place_order(...)

        # For info operations
        async with limiter.info:
            data = await client.get_positions(...)
    """

    def __init__(
        self,
        orders_rate: float = 10.0,
        orders_capacity: float = 10.0,
        info_rate: float = 1.67,  # ~100/minute
        info_capacity: float = 20.0  # Allow some burst
    ):
        self.orders = TokenBucket(rate=orders_rate, capacity=orders_capacity)
        self.info = TokenBucket(rate=info_rate, capacity=info_capacity)

    async def wait_for_order(self) -> float:
        """Wait for order rate limit slot. Returns wait time."""
        return await self.orders.acquire()

    async def wait_for_info(self) -> float:
        """Wait for info rate limit slot. Returns wait time."""
        return await self.info.acquire()

    def orders_available(self) -> float:
        """Return available order tokens."""
        return self.orders.available()

    def info_available(self) -> float:
        """Return available info tokens."""
        return self.info.available()


# Convenience function for creating a default rate limiter
def create_rate_limiter() -> HyperliquidRateLimiter:
    """Create a rate limiter with default Hyperliquid limits."""
    return HyperliquidRateLimiter()
