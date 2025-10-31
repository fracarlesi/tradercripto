"""Hyperliquid Trading Service - Async wrapper around synchronous SDK.

This service wraps the synchronous Hyperliquid Python SDK with async methods
to prevent blocking FastAPI's event loop.
"""

from typing import Any

from config.logging import get_logger
from config.settings import settings
from services.infrastructure.async_wrapper import run_in_thread
from eth_account import Account as EthAccount
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

logger = get_logger(__name__)


class HyperliquidTradingService:
    """Async wrapper for Hyperliquid SDK operations.

    The Hyperliquid SDK is synchronous-only. All methods use run_in_thread()
    to execute SDK calls in a thread pool, preventing event loop blocking.
    """

    def __init__(self) -> None:
        """Initialize Hyperliquid SDK clients (synchronous initialization)."""
        self._exchange: Exchange | None = None
        self._info: Info | None = None
        self._wallet_address: str = settings.hyperliquid_wallet_address
        self._initialized = False

        try:
            # Initialize Exchange client
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
                "Hyperliquid SDK initialized",
                extra={"context": {"wallet": self._wallet_address}},
            )

        except Exception as e:
            logger.error(
                "Failed to initialize Hyperliquid SDK",
                extra={"context": {"error": str(e)}},
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


# Global singleton instance
hyperliquid_trading_service = HyperliquidTradingService()
