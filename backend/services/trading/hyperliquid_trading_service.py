"""Hyperliquid Trading Service - Async wrapper around synchronous SDK.

This service wraps the synchronous Hyperliquid Python SDK with async methods
to prevent blocking FastAPI's event loop.
"""

import time
from typing import Any

from config.logging import get_logger
from config.settings import settings
from services.infrastructure.async_wrapper import run_in_thread
from eth_account import Account as EthAccount
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.error import ClientError

logger = get_logger(__name__)


class HyperliquidTradingService:
    """Async wrapper for Hyperliquid SDK operations.

    The Hyperliquid SDK is synchronous-only. All methods use run_in_thread()
    to execute SDK calls in a thread pool, preventing event loop blocking.
    """

    def __init__(self) -> None:
        """Initialize Hyperliquid SDK clients with retry logic for rate limiting."""
        self._exchange: Exchange | None = None
        self._info: Info | None = None
        self._wallet_address: str = settings.hyperliquid_wallet_address
        self._initialized = False

        # Retry configuration for handling rate limits during startup
        max_retries = 5
        retry_delays = [1, 2, 4, 8, 16]  # Exponential backoff in seconds

        for attempt in range(max_retries):
            try:
                logger.info(
                    f"Initializing Hyperliquid SDK (attempt {attempt + 1}/{max_retries})...",
                    extra={"context": {"wallet": self._wallet_address, "attempt": attempt + 1}},
                )

                # Initialize Exchange client (may hit rate limit during startup)
                eth_account = EthAccount.from_key(settings.hyperliquid_private_key)
                self._exchange = Exchange(
                    wallet=eth_account,
                    base_url=constants.MAINNET_API_URL,
                    account_address=self._wallet_address,
                )

                # Initialize Info client
                self._info = Info(constants.MAINNET_API_URL)

                self._initialized = True
                logger.info(
                    f"✅ Hyperliquid SDK initialized successfully (attempt {attempt + 1})",
                    extra={"context": {"wallet": self._wallet_address}},
                )
                break  # Success - exit retry loop

            except ClientError as e:
                # Check if it's a rate limit error (429)
                error_msg = str(e)
                is_rate_limited = "429" in error_msg or "rate limit" in error_msg.lower()

                if is_rate_limited and attempt < max_retries - 1:
                    wait_time = retry_delays[attempt]
                    logger.warning(
                        f"⚠️ Rate limited during Hyperliquid SDK initialization, "
                        f"retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})",
                        extra={
                            "context": {
                                "error": error_msg,
                                "wait_time": wait_time,
                                "attempt": attempt + 1,
                            }
                        },
                    )
                    time.sleep(wait_time)
                    continue
                else:
                    # Either not rate limited, or final attempt failed
                    logger.error(
                        f"❌ Failed to initialize Hyperliquid SDK after {attempt + 1} attempts",
                        extra={"context": {"error": str(e), "attempt": attempt + 1}},
                        exc_info=True,
                    )
                    raise

            except Exception as e:
                # Non-rate-limit error - fail immediately
                logger.error(
                    "Failed to initialize Hyperliquid SDK (non-rate-limit error)",
                    extra={"context": {"error": str(e)}},
                    exc_info=True,
                )
                raise

    @property
    def info(self) -> Info:
        """Get Info client instance (for read-only operations)."""
        if not self._initialized or self._info is None:
            raise RuntimeError("HyperliquidTradingService not initialized")
        return self._info

    @property
    def exchange(self) -> Exchange:
        """Get Exchange client instance (for trading operations)."""
        if not self._initialized or self._exchange is None:
            raise RuntimeError("HyperliquidTradingService not initialized")
        return self._exchange

    async def get_user_state_async(self) -> dict[str, Any]:
        """Get user state from Hyperliquid (async wrapper).

        Returns:
            Dict containing user state with balance and positions:
            {
                "marginSummary": {
                    "accountValue": "52.34",
                    "totalMarginUsed": "10.5"
                },
                "assetPositions": [...]
            }

        Raises:
            Exception: If API call fails
        """
        return await run_in_thread(self.info.user_state, self._wallet_address)

    async def get_user_fills_async(self, limit: int = 100) -> list[dict[str, Any]]:
        """Get recent fills (trades) from Hyperliquid (async wrapper).

        Args:
            limit: Maximum number of fills to retrieve (default 100)

        Returns:
            List of fill dicts containing:
            {
                "coin": "BTC",
                "side": "B" or "S",
                "sz": "0.01",
                "px": "50000.0",
                "time": 1234567890000
            }

        Raises:
            Exception: If API call fails
        """
        fills = await run_in_thread(self.info.user_fills, self._wallet_address)
        return fills[:limit] if fills else []

    async def get_all_mids_async(self) -> dict[str, str]:
        """Get mid prices for all symbols (async wrapper).

        Returns:
            Dict mapping symbol to mid price:
            {"BTC": "50000.0", "ETH": "3000.0", ...}

        Raises:
            Exception: If API call fails
        """
        return await run_in_thread(self.info.all_mids)

    async def get_meta_async(self) -> dict[str, Any]:
        """Get market metadata including asset specs (async wrapper).

        Returns:
            Dict containing:
            {
                "universe": [
                    {
                        "name": "BTC",
                        "szDecimals": 5,
                        "maxLeverage": 50
                    },
                    ...
                ]
            }

        Raises:
            Exception: If API call fails
        """
        return await run_in_thread(self.info.meta)

    async def update_leverage_async(
        self, symbol: str, leverage: int, is_cross: bool = True
    ) -> dict[str, Any]:
        """Update leverage for a specific asset on Hyperliquid (async).

        Args:
            symbol: Trading symbol (e.g., "BTC", "ETH")
            leverage: Leverage multiplier (1-50, but we limit to 1-10)
            is_cross: True for cross-margin, False for isolated margin

        Returns:
            Dict containing leverage update response

        Raises:
            Exception: If leverage update fails
        """
        if not self._initialized or not self._exchange:
            raise RuntimeError("Hyperliquid SDK not initialized")

        def _update_leverage():
            """Synchronous leverage update."""
            result = self._exchange.update_leverage(
                leverage=leverage, name=symbol, is_cross=is_cross
            )
            return result

        logger.info(
            f"Updating leverage for {symbol}: {leverage}x ({'cross' if is_cross else 'isolated'})",
            extra={
                "context": {
                    "symbol": symbol,
                    "leverage": leverage,
                    "is_cross": is_cross,
                }
            },
        )

        result = await run_in_thread(_update_leverage)

        logger.info(
            f"Leverage updated for {symbol}: {result}",
            extra={"context": {"result": result}},
        )

        return result

    async def place_market_order_async(
        self, symbol: str, is_buy: bool, size: float, reduce_only: bool = False, leverage: int = 1
    ) -> dict[str, Any]:
        """Place a market order on Hyperliquid (async).

        Args:
            symbol: Trading symbol (e.g., "BTC", "ETH")
            is_buy: True for buy, False for sell
            size: Order size (quantity of base asset)
            reduce_only: If True, order can only reduce existing position
            leverage: Leverage multiplier (1-10x) - will be set before opening position

        Returns:
            Dict containing order response:
            {
                "status": "ok" | "error",
                "response": {
                    "type": "order",
                    "data": {
                        "statuses": [...]
                    }
                }
            }

        Raises:
            Exception: If order placement fails
        """
        if not self._initialized or not self._exchange:
            raise RuntimeError("Hyperliquid SDK not initialized")

        # Update leverage BEFORE opening position (only if not closing position)
        if not reduce_only and leverage > 1:
            try:
                await self.update_leverage_async(symbol=symbol, leverage=leverage, is_cross=True)
            except Exception as e:
                logger.error(f"Failed to update leverage for {symbol}: {e}", exc_info=True)
                # Continue with order placement anyway (will use existing leverage)

        def _place_order():
            """Synchronous order placement."""
            order_result = self._exchange.market_open(
                name=symbol, is_buy=is_buy, sz=size
            )
            return order_result

        logger.info(
            f"Placing {'BUY' if is_buy else 'SELL'} market order: {size} {symbol} (leverage: {leverage}x)",
            extra={
                "context": {
                    "symbol": symbol,
                    "side": "BUY" if is_buy else "SELL",
                    "size": size,
                    "reduce_only": reduce_only,
                    "leverage": leverage,
                }
            },
        )

        result = await run_in_thread(_place_order)

        logger.info(
            f"Order placed: {result.get('status', 'unknown')}",
            extra={"context": {"result": result}},
        )

        return result


# Global singleton instance
hyperliquid_trading_service = HyperliquidTradingService()
