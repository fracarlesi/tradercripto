"""
Hyperliquid API Client Package
==============================

Enhanced async wrapper for Hyperliquid DEX with rate limiting,
retry logic, and proper error handling.

Usage:
    from crypto_bot.api import HyperliquidClient, create_client

    # Option 1: Manual connect/disconnect
    client = HyperliquidClient(testnet=False)
    await client.connect()

    markets = await client.get_all_markets()
    positions = await client.get_positions()

    await client.disconnect()

    # Option 2: Factory function
    client = await create_client(testnet=False)
    # ... use client ...
    await client.disconnect()
"""

from .exceptions import (
    APIResponseError,
    AuthenticationError,
    ConnectionError,
    HyperliquidError,
    InsufficientMarginError,
    InvalidOrderError,
    OrderError,
    OrderRejectedError,
    RateLimitError,
    SymbolNotFoundError,
    TimeoutError,
)
from .hyperliquid import HyperliquidClient, OrderType, create_client
from .rate_limiter import (
    HyperliquidRateLimiter,
    TokenBucket,
    create_rate_limiter,
)

__all__ = [
    # Main client
    "HyperliquidClient",
    "create_client",
    "OrderType",
    # Rate limiting
    "HyperliquidRateLimiter",
    "TokenBucket",
    "create_rate_limiter",
    # Exceptions
    "HyperliquidError",
    "RateLimitError",
    "ConnectionError",
    "AuthenticationError",
    "OrderError",
    "OrderRejectedError",
    "InsufficientMarginError",
    "InvalidOrderError",
    "SymbolNotFoundError",
    "APIResponseError",
    "TimeoutError",
]

__version__ = "2.0.0"
