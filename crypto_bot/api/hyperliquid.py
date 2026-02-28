"""
Hyperliquid API Client
======================

Enhanced async wrapper around hyperliquid-python SDK with:
- Built-in rate limiting (token bucket)
- Retry with exponential backoff
- Proper error handling and logging
- Cache for static data with TTL
- Support for testnet and mainnet

Usage:
    from crypto_bot.api import HyperliquidClient

    client = HyperliquidClient()
    await client.connect()

    # Market data
    markets = await client.get_all_markets()
    orderbook = await client.get_orderbook("ETH", depth=10)

    # Account
    positions = await client.get_positions()
    orders = await client.get_open_orders()

    # Trading
    result = await client.place_order("ETH", is_buy=True, size=0.1, price=3000.0)
    await client.cancel_order("ETH", order_id=12345)

    await client.disconnect()
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Optional, TypeVar

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from .exceptions import (
    APIResponseError,
    AuthenticationError,
    ConnectionError,
    HyperliquidError,
    InsufficientMarginError,
    InvalidOrderError,
    OrderRejectedError,
    RateLimitError,
    SymbolNotFoundError,
)
from .rate_limiter import HyperliquidRateLimiter

# Type variable for generic return type
T = TypeVar("T")

# Configure module logger
logger = logging.getLogger(__name__)


class OrderType(str, Enum):
    """Order types supported by Hyperliquid."""
    MARKET = "market"
    LIMIT = "limit"
    STOP_MARKET = "stop_market"
    STOP_LIMIT = "stop_limit"
    TAKE_PROFIT_MARKET = "take_profit_market"
    TAKE_PROFIT_LIMIT = "take_profit_limit"


@dataclass
class CacheEntry:
    """Cache entry with TTL."""
    data: Any
    expires_at: float

    def is_expired(self) -> bool:
        return time.monotonic() > self.expires_at


@dataclass
class TTLCache:
    """Simple TTL cache for static data."""
    _cache: dict[str, CacheEntry] = field(default_factory=dict)
    default_ttl: float = 60.0  # seconds

    def get(self, key: str) -> Optional[Any]:
        """Get value from cache if not expired."""
        entry = self._cache.get(key)
        if entry and not entry.is_expired():
            return entry.data
        if entry:
            del self._cache[key]
        return None

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """Set value in cache with TTL."""
        expires_at = time.monotonic() + (ttl or self.default_ttl)
        self._cache[key] = CacheEntry(data=value, expires_at=expires_at)

    def clear(self) -> None:
        """Clear all cached data."""
        self._cache.clear()

    def invalidate(self, key: str) -> None:
        """Invalidate a specific cache entry."""
        self._cache.pop(key, None)


def with_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    exponential_base: float = 2.0,
    retryable_exceptions: tuple = (ConnectionError, RateLimitError)
):
    """
    Decorator for retry with exponential backoff.

    Args:
        max_attempts: Maximum number of retry attempts
        base_delay: Initial delay between retries (seconds)
        max_delay: Maximum delay between retries (seconds)
        exponential_base: Base for exponential backoff
        retryable_exceptions: Exceptions that trigger retry
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            last_exception = None

            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        delay = min(
                            base_delay * (exponential_base ** attempt),
                            max_delay
                        )
                        # For rate limit, use retry_after if available
                        if isinstance(e, RateLimitError) and e.retry_after:
                            delay = e.retry_after

                        logger.warning(
                            f"{func.__name__} failed (attempt {attempt + 1}/{max_attempts}): {e}. "
                            f"Retrying in {delay:.2f}s..."
                        )
                        await asyncio.sleep(delay)
                    else:
                        logger.error(f"{func.__name__} failed after {max_attempts} attempts: {e}")

            raise last_exception

        return wrapper
    return decorator


class HyperliquidClient:
    """
    Enhanced async client for Hyperliquid DEX API.

    Features:
    - Async/await for all I/O operations
    - Built-in rate limiting with token bucket algorithm
    - Retry with exponential backoff
    - Cache for static data (market info) with TTL
    - Proper exception hierarchy
    - Support for testnet and mainnet

    Args:
        testnet: Use testnet instead of mainnet
        private_key: Private key for signing (defaults to PRIVATE_KEY env var)
        wallet_address: Wallet address (defaults to WALLET_ADDRESS env var or derived from key)
    """

    def __init__(
        self,
        testnet: bool = False,
        private_key: Optional[str] = None,
        wallet_address: Optional[str] = None
    ):
        self.testnet = testnet or os.getenv("HYPERLIQUID_TESTNET", "false").lower() == "true"

        # Get credentials from environment if not provided
        self._private_key = private_key or os.getenv("HYPERLIQUID_PRIVATE_KEY") or os.getenv("PRIVATE_KEY")
        self._wallet_address = wallet_address or os.getenv("WALLET_ADDRESS")

        # Clients (initialized in connect())
        self._exchange: Optional[Exchange] = None
        self._info: Optional[Info] = None
        self._account: Optional[Account] = None

        # Rate limiter
        self._rate_limiter = HyperliquidRateLimiter()

        # Cache for static data
        self._cache = TTLCache(default_ttl=300.0)  # 5 minute default TTL

        # Symbol metadata cache (long TTL since it rarely changes)
        self._symbol_info: dict[str, dict] = {}

        # Connection state
        self._connected = False

        # Base URL
        self._base_url = constants.TESTNET_API_URL if self.testnet else constants.MAINNET_API_URL

    @property
    def is_connected(self) -> bool:
        """Check if client is connected."""
        return self._connected

    @property
    def address(self) -> str:
        """Get the wallet address."""
        if not self._account:
            raise ConnectionError("Client not connected")
        return self._account.address

    async def connect(self) -> None:
        """
        Connect to Hyperliquid API.

        Initializes the Info and Exchange clients and validates credentials.

        Raises:
            AuthenticationError: If private key is invalid or missing
            ConnectionError: If connection to API fails
        """
        if self._connected:
            logger.debug("Already connected")
            return

        logger.info(f"Connecting to Hyperliquid {'TESTNET' if self.testnet else 'MAINNET'}")

        # Validate private key
        if not self._private_key:
            raise AuthenticationError("Private key not found. Set PRIVATE_KEY environment variable.")

        try:
            # Create account from private key
            self._account = Account.from_key(self._private_key)
            logger.info(f"Wallet address: {self._account.address}")

            # Override wallet address if derived
            if not self._wallet_address:
                self._wallet_address = self._account.address

            # Initialize clients
            # Note: SDK clients are synchronous but we wrap them in async
            self._info = Info(self._base_url, skip_ws=True)
            self._exchange = Exchange(self._account, self._base_url)

            # Verify connection
            await self._verify_connection()

            self._connected = True
            logger.info("Connected successfully")

        except Exception as e:
            logger.error(f"Connection failed: {e}")
            raise ConnectionError(f"Failed to connect: {e}")

    async def disconnect(self) -> None:
        """Disconnect from Hyperliquid API."""
        if not self._connected:
            return

        logger.info("Disconnecting from Hyperliquid")
        self._cache.clear()
        self._exchange = None
        self._info = None
        self._connected = False
        logger.info("Disconnected")

    async def _verify_connection(self) -> None:
        """Verify connection by fetching account state."""
        try:
            user_state = await self._run_sync(
                lambda: self._info.user_state(self._account.address)
            )
            margin = float(user_state.get("marginSummary", {}).get("accountValue", 0))
            logger.info(f"Account value: ${margin:.2f}")
        except Exception as e:
            raise ConnectionError(f"Failed to verify connection: {e}")

    async def _run_sync(self, func: Callable[[], T]) -> T:
        """Run synchronous SDK function in executor."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, func)

    def _ensure_connected(self) -> None:
        """Ensure client is connected."""
        if not self._connected:
            raise ConnectionError("Client not connected. Call connect() first.")

    # =========================================================================
    # Market Data Methods
    # =========================================================================

    @with_retry(max_attempts=3)
    async def get_all_markets(self) -> list[dict]:
        """
        Get all available markets/symbols.

        Returns:
            List of market info dicts with keys:
            - name: Symbol name (e.g., "ETH", "BTC")
            - szDecimals: Size decimals for orders
            - maxLeverage: Maximum allowed leverage

        Cached for 5 minutes.
        """
        self._ensure_connected()

        cached = self._cache.get("markets")
        if cached:
            return cached

        async with self._rate_limiter.info:
            meta = await self._run_sync(lambda: self._info.meta())

        markets = meta.get("universe", [])

        # Update symbol info cache
        for market in markets:
            self._symbol_info[market["name"]] = market

        self._cache.set("markets", markets, ttl=300.0)
        return markets

    @with_retry(max_attempts=3)
    async def get_market_summary(self, symbol: str) -> dict:
        """
        Get summary for a specific market.

        Args:
            symbol: Trading symbol (e.g., "ETH")

        Returns:
            Dict with market info including:
            - name: Symbol name
            - markPx: Current mark price
            - midPx: Mid price
            - funding: Current funding rate
            - openInterest: Total open interest
            - dayNtlVlm: 24h notional volume
        """
        self._ensure_connected()

        async with self._rate_limiter.info:
            all_mids = await self._run_sync(lambda: self._info.all_mids())

        if symbol not in all_mids:
            raise SymbolNotFoundError(symbol)

        # Get additional data from meta_and_asset_ctxs
        async with self._rate_limiter.info:
            ctx = await self._run_sync(lambda: self._info.meta_and_asset_ctxs())

        # Find context for symbol
        asset_ctx = None
        for asset in ctx[1] if len(ctx) > 1 else []:
            if isinstance(asset, dict) and asset.get("name") == symbol:
                asset_ctx = asset
                break

        return {
            "name": symbol,
            "midPx": float(all_mids[symbol]),
            "markPx": float(asset_ctx.get("markPx", all_mids[symbol])) if asset_ctx else float(all_mids[symbol]),
            "funding": float(asset_ctx.get("funding", 0)) if asset_ctx else 0.0,
            "openInterest": float(asset_ctx.get("openInterest", 0)) if asset_ctx else 0.0,
            "dayNtlVlm": float(asset_ctx.get("dayNtlVlm", 0)) if asset_ctx else 0.0,
        }

    async def _get_asset_data(self, field: str, cache_key: str, ttl: float = 60.0) -> dict[str, float]:
        """
        Helper to extract per-symbol data from meta_and_asset_ctxs.

        Args:
            field: Field name to extract from asset context (e.g., "funding", "openInterest")
            cache_key: Key for caching the result
            ttl: Cache TTL in seconds

        Returns:
            Dict mapping symbol to field value
        """
        self._ensure_connected()

        cached = self._cache.get(cache_key)
        if cached:
            return cached

        async with self._rate_limiter.info:
            ctx = await self._run_sync(lambda: self._info.meta_and_asset_ctxs())

        result = {}
        meta = ctx[0] if len(ctx) > 0 else {}
        assets = ctx[1] if len(ctx) > 1 else []
        universe = meta.get("universe", [])

        for i, asset_ctx in enumerate(assets):
            if i < len(universe):
                symbol = universe[i].get("name", f"UNKNOWN_{i}")
                result[symbol] = float(asset_ctx.get(field, 0))

        self._cache.set(cache_key, result, ttl=ttl)
        return result

    @with_retry(max_attempts=3)
    async def get_funding_rates(self) -> dict[str, float]:
        """
        Get current funding rates for all symbols.

        Returns:
            Dict mapping symbol to funding rate
        """
        return await self._get_asset_data("funding", "funding_rates", ttl=60.0)

    @with_retry(max_attempts=3)
    async def get_open_interest(self) -> dict[str, float]:
        """
        Get open interest for all symbols.

        Returns:
            Dict mapping symbol to open interest in base currency
        """
        return await self._get_asset_data("openInterest", "open_interest", ttl=60.0)

    @with_retry(max_attempts=3)
    async def get_candles(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 100
    ) -> list[dict]:
        """
        Get OHLCV candle data.

        Args:
            symbol: Trading symbol (e.g., "ETH")
            interval: Candle interval ("1m", "5m", "15m", "1h", "4h", "1d")
            limit: Number of candles to fetch (max 5000)

        Returns:
            List of candle dicts with keys:
            - t: Timestamp (ms)
            - o: Open price
            - h: High price
            - l: Low price
            - c: Close price
            - v: Volume
        """
        self._ensure_connected()

        # Calculate time range
        interval_ms = {
            "1m": 60_000,
            "5m": 300_000,
            "15m": 900_000,
            "1h": 3_600_000,
            "4h": 14_400_000,
            "1d": 86_400_000,
        }.get(interval, 60_000)

        end_time = int(time.time() * 1000)
        start_time = end_time - (limit * interval_ms)

        async with self._rate_limiter.info:
            candles = await self._run_sync(
                lambda: self._info.candles_snapshot(symbol, interval, start_time, end_time)
            )

        return [
            {
                "t": c["t"],
                "o": float(c["o"]),
                "h": float(c["h"]),
                "l": float(c["l"]),
                "c": float(c["c"]),
                "v": float(c["v"]),
            }
            for c in candles
        ]

    @with_retry(max_attempts=3)
    async def get_orderbook(self, symbol: str, depth: int = 10) -> dict:
        """
        Get orderbook for a symbol.

        Args:
            symbol: Trading symbol (e.g., "ETH")
            depth: Number of levels per side (default: 10)

        Returns:
            Dict with:
            - bids: List of [price, size] for bids
            - asks: List of [price, size] for asks
            - time: Timestamp
        """
        self._ensure_connected()

        async with self._rate_limiter.info:
            l2 = await self._run_sync(lambda: self._info.l2_snapshot(symbol))

        return {
            "bids": [[float(p["px"]), float(p["sz"])] for p in l2.get("levels", [[]])[0][:depth]],
            "asks": [[float(p["px"]), float(p["sz"])] for p in l2.get("levels", [[], []])[1][:depth]],
            "time": l2.get("time", int(time.time() * 1000)),
        }

    async def get_spread_pct(self, symbol: str) -> float:
        """
        Get the bid-ask spread as a percentage of mid price.

        Used as a pre-trade liquidity filter to avoid entering positions
        on illiquid assets where slippage would eat into profits.

        Args:
            symbol: Trading symbol (e.g., "ETH")

        Returns:
            Spread as percentage of mid price (e.g., 0.05 for 0.05%).
            Returns 0.0 on error (fail-open: don't block trades due to API errors).
        """
        try:
            self._ensure_connected()

            async with self._rate_limiter.info:
                l2 = await self._run_sync(lambda: self._info.l2_snapshot(symbol))

            levels = l2.get("levels", [[], []])
            bids = levels[0] if len(levels) > 0 else []
            asks = levels[1] if len(levels) > 1 else []

            if not bids or not asks:
                logger.warning("No bid/ask data for %s, returning 0.0 (fail-open)", symbol)
                return 0.0

            best_bid = Decimal(bids[0]["px"])
            best_ask = Decimal(asks[0]["px"])

            if best_bid <= 0 or best_ask <= 0:
                return 0.0

            mid_price = (best_bid + best_ask) / 2
            spread_pct = float((best_ask - best_bid) / mid_price * 100)

            return spread_pct

        except Exception as e:
            logger.warning("Failed to get spread for %s: %s (fail-open)", symbol, e)
            return 0.0

    # =========================================================================
    # Account Methods
    # =========================================================================

    @with_retry(max_attempts=3)
    async def get_account_state(self) -> dict:
        """
        Get full account state.

        Returns:
            Dict with:
            - equity: Total account value
            - availableBalance: Available margin
            - marginUsed: Total margin used
            - unrealizedPnl: Total unrealized PnL
            - positions: List of position dicts
        """
        self._ensure_connected()

        async with self._rate_limiter.info:
            user_state = await self._run_sync(
                lambda: self._info.user_state(self._account.address)
            )

        margin_summary = user_state.get("marginSummary", {})
        equity = float(margin_summary.get("accountValue", 0))
        margin_used = float(margin_summary.get("totalMarginUsed", 0))

        # Calculate unrealized PnL
        unrealized_pnl = 0.0
        positions = []
        for pos in user_state.get("assetPositions", []):
            pos_info = pos.get("position", {})
            size = float(pos_info.get("szi", 0))
            if abs(size) > 0.0001:
                upnl = float(pos_info.get("unrealizedPnl", 0))
                unrealized_pnl += upnl
                positions.append({
                    "symbol": pos_info.get("coin"),
                    "side": "long" if size > 0 else "short",
                    "size": abs(size),
                    "entryPrice": float(pos_info.get("entryPx", 0)),
                    "markPrice": float(pos_info.get("positionValue", 0)) / abs(size) if abs(size) > 0 else 0,
                    "unrealizedPnl": upnl,
                    "leverage": int(pos_info.get("leverage", {}).get("value", 1)),
                    "liquidationPrice": float(pos_info.get("liquidationPx", 0)) if pos_info.get("liquidationPx") else None,
                    "marginUsed": float(pos_info.get("marginUsed", 0)),
                })

        return {
            "equity": equity,
            "availableBalance": equity - margin_used,
            "marginUsed": margin_used,
            "unrealizedPnl": unrealized_pnl,
            "positions": positions,
        }

    @with_retry(max_attempts=3)
    async def get_positions(self) -> list[dict]:
        """
        Get all open positions.

        Returns:
            List of position dicts with keys:
            - symbol: Trading symbol
            - side: "long" or "short"
            - size: Position size
            - entryPrice: Average entry price
            - markPrice: Current mark price
            - unrealizedPnl: Unrealized PnL
            - leverage: Current leverage
            - liquidationPrice: Liquidation price
            - marginUsed: Margin used for position
        """
        state = await self.get_account_state()
        return state["positions"]

    @with_retry(max_attempts=3)
    async def get_open_orders(self) -> list[dict]:
        """
        Get all open orders.

        Returns:
            List of order dicts with keys:
            - orderId: Order ID
            - symbol: Trading symbol
            - side: "buy" or "sell"
            - size: Order size
            - price: Limit price
            - orderType: Order type
            - reduceOnly: Whether reduce-only
            - createdAt: Creation timestamp
        """
        self._ensure_connected()

        async with self._rate_limiter.info:
            orders = await self._run_sync(
                lambda: self._info.open_orders(self._account.address)
            )

        return [
            {
                "orderId": int(o.get("oid", 0)),
                "symbol": o.get("coin"),
                "side": "buy" if o.get("side", "").upper() == "B" else "sell",
                "size": float(o.get("sz", 0)),
                "price": float(o.get("limitPx", 0)),
                "orderType": o.get("orderType", "limit"),
                "reduceOnly": o.get("reduceOnly", False),
                "createdAt": datetime.fromtimestamp(o.get("timestamp", 0) / 1000) if o.get("timestamp") else None,
            }
            for o in orders
        ]

    @with_retry(max_attempts=3)
    async def get_fills(self, limit: int = 100) -> list[dict]:
        """
        Get recent trade fills.

        Args:
            limit: Maximum number of fills to return

        Returns:
            List of fill dicts with keys:
            - fillId: Fill ID
            - symbol: Trading symbol
            - side: "buy" or "sell"
            - price: Fill price
            - size: Fill size
            - fee: Trading fee
            - time: Fill timestamp
            - orderId: Associated order ID
        """
        self._ensure_connected()

        async with self._rate_limiter.info:
            fills = await self._run_sync(
                lambda: self._info.user_fills(self._account.address)
            )

        result = []
        for f in fills[:limit]:
            result.append({
                "fillId": f.get("tid"),
                "symbol": f.get("coin"),
                "side": "buy" if f.get("side", "").upper() == "B" else "sell",
                "price": float(f.get("px", 0)),
                "size": float(f.get("sz", 0)),
                "fee": float(f.get("fee", 0)),
                "time": datetime.fromtimestamp(f.get("time", 0) / 1000) if f.get("time") else None,
                "orderId": f.get("oid"),
                "closedPnl": float(f.get("closedPnl", 0)),
            })

        return result

    # =========================================================================
    # Trading Methods
    # =========================================================================

    async def place_order(
        self,
        symbol: str,
        is_buy: bool,
        size: float,
        price: Optional[float] = None,
        order_type: str = "limit",
        reduce_only: bool = False,
        time_in_force: str = "Gtc",
        slippage: float = 0.01
    ) -> dict:
        """
        Place an order.

        Args:
            symbol: Trading symbol (e.g., "ETH")
            is_buy: True for buy, False for sell
            size: Order size in base currency
            price: Limit price (None for market orders)
            order_type: "limit" or "market"
            reduce_only: Whether order should only reduce position
            time_in_force: "Gtc" (Good til cancelled), "Ioc" (Immediate or cancel), "Alo" (Post only)
            slippage: Slippage tolerance for market orders (default: 1%)

        Returns:
            Dict with:
            - success: True if order was accepted
            - orderId: Order ID if limit order
            - fillPrice: Average fill price if market order
            - filledSize: Size filled (for market orders)
            - status: Order status

        Raises:
            OrderRejectedError: If order is rejected
            InsufficientMarginError: If insufficient margin
            InvalidOrderError: If parameters are invalid
        """
        self._ensure_connected()

        # Validate symbol
        if symbol not in self._symbol_info:
            await self.get_all_markets()
        if symbol not in self._symbol_info:
            raise SymbolNotFoundError(symbol)

        # Get size decimals
        sz_decimals = self._symbol_info[symbol].get("szDecimals", 4)
        size = round(size, sz_decimals)

        # Round price to valid precision (Hyperliquid uses 5 significant figures)
        if price is not None:
            price = self._round_price(price)

        logger.info(f"Placing {'BUY' if is_buy else 'SELL'} {order_type} order: {size} {symbol} @ {price or 'MARKET'}")

        try:
            async with self._rate_limiter.orders:
                if order_type.lower() == "market":
                    result = await self._run_sync(
                        lambda: self._exchange.market_open(
                            symbol, is_buy, size, price, slippage
                        )
                    )
                else:
                    # Build order
                    order = {
                        "a": self._get_asset_index(symbol),
                        "b": is_buy,
                        "p": str(price),
                        "s": str(size),
                        "r": reduce_only,
                        "t": {"limit": {"tif": time_in_force}},
                    }
                    result = await self._run_sync(
                        lambda: self._exchange.order(symbol, is_buy, size, price, {"limit": {"tif": time_in_force}}, reduce_only=reduce_only)
                    )

            return self._parse_order_result(result, is_market=(order_type.lower() == "market"))

        except Exception as e:
            error_msg = str(e).lower()
            if "insufficient" in error_msg or "margin" in error_msg:
                raise InsufficientMarginError(str(e), symbol=symbol)
            elif "invalid" in error_msg:
                raise InvalidOrderError(str(e), symbol=symbol)
            else:
                raise OrderRejectedError(str(e), symbol=symbol)

    async def place_trigger_order(
        self,
        symbol: str,
        is_buy: bool,
        size: float,
        trigger_price: float,
        limit_price: Optional[float] = None,
        tpsl: str = "sl",
        reduce_only: bool = True,
    ) -> dict:
        """
        Place a trigger order (stop loss or take profit).

        Args:
            symbol: Trading symbol (e.g., "ETH")
            is_buy: True for buy, False for sell
            size: Order size in base currency
            trigger_price: Price at which to trigger the order
            limit_price: Limit price after trigger (None for market)
            tpsl: "sl" for stop loss, "tp" for take profit
            reduce_only: Whether order should only reduce position (default True)

        Returns:
            Dict with:
            - success: True if order was accepted
            - orderId: Order ID
            - status: Order status

        Raises:
            OrderRejectedError: If order is rejected
        """
        self._ensure_connected()

        # Validate symbol
        if symbol not in self._symbol_info:
            await self.get_all_markets()
        if symbol not in self._symbol_info:
            raise SymbolNotFoundError(symbol)

        # Get size decimals
        sz_decimals = self._symbol_info[symbol].get("szDecimals", 4)
        size = round(size, sz_decimals)

        # Round prices
        trigger_price = self._round_price(trigger_price)
        if limit_price is not None:
            limit_price = self._round_price(limit_price)

        # For trigger orders, use limit_price if provided, else use trigger_price
        order_price = limit_price if limit_price is not None else trigger_price
        is_market = limit_price is None

        logger.info(
            f"Placing {tpsl.upper()} trigger order: {'BUY' if is_buy else 'SELL'} {size} {symbol} "
            f"trigger@{trigger_price} {'MARKET' if is_market else f'limit@{limit_price}'}"
        )

        try:
            async with self._rate_limiter.orders:
                # Build trigger order type
                order_type = {
                    "trigger": {
                        "triggerPx": trigger_price,
                        "isMarket": is_market,
                        "tpsl": tpsl,  # "sl" or "tp"
                    }
                }

                result = await self._run_sync(
                    lambda: self._exchange.order(
                        symbol,
                        is_buy,
                        size,
                        order_price,
                        order_type,
                        reduce_only=reduce_only
                    )
                )

            return self._parse_order_result(result, is_market=False)

        except Exception as e:
            error_msg = str(e).lower()
            logger.error(f"Trigger order failed: {e}")
            if "insufficient" in error_msg or "margin" in error_msg:
                raise InsufficientMarginError(str(e), symbol=symbol)
            elif "invalid" in error_msg:
                raise InvalidOrderError(str(e), symbol=symbol)
            else:
                raise OrderRejectedError(str(e), symbol=symbol)

    async def cancel_order(self, symbol: str, order_id: int) -> bool:
        """
        Cancel a specific order.

        Args:
            symbol: Trading symbol
            order_id: Order ID to cancel

        Returns:
            True if successfully cancelled

        Raises:
            OrderError: If cancellation fails
        """
        self._ensure_connected()

        logger.info(f"Cancelling order {order_id} for {symbol}")

        try:
            async with self._rate_limiter.orders:
                result = await self._run_sync(
                    lambda: self._exchange.cancel(symbol, order_id)
                )

            success = result.get("status") == "ok"
            if success:
                logger.info(f"Order {order_id} cancelled")
            else:
                logger.warning(f"Failed to cancel order {order_id}: {result}")

            return success

        except Exception as e:
            logger.error(f"Cancel order failed: {e}")
            raise OrderRejectedError(f"Cancel failed: {e}", order_id=order_id, symbol=symbol)

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        """
        Cancel all open orders, optionally for a specific symbol.

        Args:
            symbol: Trading symbol (None for all symbols)

        Returns:
            Number of orders cancelled
        """
        self._ensure_connected()

        logger.info(f"Cancelling all orders{f' for {symbol}' if symbol else ''}")

        # Get open orders
        orders = await self.get_open_orders()

        if symbol:
            orders = [o for o in orders if o["symbol"] == symbol]

        if not orders:
            logger.info("No orders to cancel")
            return 0

        cancelled = 0
        for order in orders:
            try:
                if await self.cancel_order(order["symbol"], order["orderId"]):
                    cancelled += 1
            except Exception as e:
                logger.warning(f"Failed to cancel order {order['orderId']}: {e}")

        logger.info(f"Cancelled {cancelled}/{len(orders)} orders")
        return cancelled

    async def update_leverage(self, symbol: str, leverage: int) -> bool:
        """
        Update leverage for a symbol.

        Args:
            symbol: Trading symbol
            leverage: New leverage (1-100 depending on symbol)

        Returns:
            True if successful

        Raises:
            InvalidOrderError: If leverage is invalid
        """
        self._ensure_connected()

        # Validate symbol
        if symbol not in self._symbol_info:
            await self.get_all_markets()
        if symbol not in self._symbol_info:
            raise SymbolNotFoundError(symbol)

        max_leverage = self._symbol_info[symbol].get("maxLeverage", 100)
        if leverage > max_leverage:
            logger.warning(
                "Leverage %dx exceeds max %dx for %s, capping to %dx",
                leverage, max_leverage, symbol, max_leverage,
            )
            leverage = max_leverage

        logger.info(f"Setting leverage for {symbol} to {leverage}x")

        try:
            async with self._rate_limiter.orders:
                result = await self._run_sync(
                    lambda: self._exchange.update_leverage(leverage, symbol)
                )

            success = result.get("status") == "ok"
            if success:
                logger.info(f"Leverage updated to {leverage}x for {symbol}")
            else:
                logger.warning(f"Failed to update leverage: {result}")

            return success

        except Exception as e:
            logger.error(f"Update leverage failed: {e}")
            raise InvalidOrderError(f"Leverage update failed: {e}")

    async def close_position(
        self,
        symbol: str,
        slippage: float = 0.01
    ) -> dict:
        """
        Close entire position for a symbol.

        Args:
            symbol: Trading symbol
            slippage: Slippage tolerance (default: 1%)

        Returns:
            Order result dict
        """
        self._ensure_connected()

        # Get current position
        positions = await self.get_positions()
        position = next((p for p in positions if p["symbol"] == symbol), None)

        if not position:
            logger.info(f"No position to close for {symbol}")
            return {"success": True, "message": "No position"}

        logger.info(f"Closing {position['side']} position: {position['size']} {symbol}")

        try:
            async with self._rate_limiter.orders:
                result = await self._run_sync(
                    lambda: self._exchange.market_close(symbol, position["size"], slippage=slippage)
                )

            return self._parse_order_result(result, is_market=True)

        except Exception as e:
            logger.error(f"Close position failed: {e}")
            raise OrderRejectedError(f"Close failed: {e}", symbol=symbol)

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _round_price(self, price: float) -> float:
        """
        Round price to valid precision for Hyperliquid.

        Hyperliquid requires prices to have at most 4 decimal places
        AND at most 5 significant figures.

        Args:
            price: Raw price value

        Returns:
            Price rounded to valid precision
        """
        if price <= 0:
            return price

        from math import log10, floor

        # First, round to 4 decimal places (Hyperliquid requirement)
        rounded = round(price, 4)

        # Also ensure we don't exceed 5 significant figures
        if rounded > 0:
            magnitude = floor(log10(abs(rounded)))
            max_decimals = max(0, 4 - magnitude)  # e.g., 1000 -> 0 decimals, 0.01 -> 4 decimals
            rounded = round(rounded, min(4, max_decimals))

        return rounded

    def _get_asset_index(self, symbol: str) -> int:
        """Get asset index for symbol from metadata."""
        if symbol not in self._symbol_info:
            raise SymbolNotFoundError(symbol)
        # The index is the position in the universe array
        return list(self._symbol_info.keys()).index(symbol)

    def _parse_order_result(self, result: dict, is_market: bool = False) -> dict:
        """Parse order result from SDK response."""
        if result.get("status") != "ok":
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            error = statuses[0] if statuses else "Unknown error"
            raise OrderRejectedError(str(error))

        response_data = result.get("response", {}).get("data", {})
        statuses = response_data.get("statuses", [])

        parsed = {
            "success": True,
            "status": "filled" if is_market else "open",
        }

        for status in statuses:
            if "filled" in status:
                fill_info = status["filled"]
                parsed["fillPrice"] = float(fill_info.get("avgPx", 0))
                parsed["filledSize"] = float(fill_info.get("totalSz", 0))
                parsed["orderId"] = fill_info.get("oid")
                parsed["status"] = "filled"
            elif "resting" in status:
                rest_info = status["resting"]
                parsed["orderId"] = rest_info.get("oid")
                parsed["status"] = "open"
            elif "error" in status:
                raise OrderRejectedError(status["error"])

        return parsed


# Convenience function for creating a client
async def create_client(testnet: bool = False) -> HyperliquidClient:
    """
    Create and connect a HyperliquidClient.

    Args:
        testnet: Use testnet instead of mainnet

    Returns:
        Connected HyperliquidClient instance
    """
    client = HyperliquidClient(testnet=testnet)
    await client.connect()
    return client
