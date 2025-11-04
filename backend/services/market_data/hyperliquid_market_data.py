"""
Hyperliquid market data service using CCXT and native SDK
"""

import logging
from datetime import UTC, datetime
from typing import Any

import ccxt
from hyperliquid.info import Info

logger = logging.getLogger(__name__)


class HyperliquidClient:
    def __init__(self):
        self.exchange = None
        self._initialize_exchange()

    def _initialize_exchange(self):
        """Initialize CCXT Hyperliquid exchange"""
        try:
            self.exchange = ccxt.hyperliquid(
                {
                    "sandbox": False,  # Set to True for testnet
                    "enableRateLimit": True,
                }
            )
            logger.info("Hyperliquid exchange initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Hyperliquid exchange: {e}", exc_info=True)
            raise

    def get_kline_data(
        self, symbol: str, period: str = "1d", count: int = 100
    ) -> list[dict[str, Any]]:
        """Get kline/candlestick data for a symbol"""
        try:
            if not self.exchange:
                self._initialize_exchange()

            formatted_symbol = self._format_symbol(symbol)

            # Map period to CCXT timeframe
            timeframe_map = {
                "1m": "1m",
                "5m": "5m",
                "15m": "15m",
                "30m": "30m",
                "1h": "1h",
                "1d": "1d",
            }
            timeframe = timeframe_map.get(period, "1d")

            # Fetch OHLCV data
            ohlcv = self.exchange.fetch_ohlcv(formatted_symbol, timeframe, limit=count)

            # Convert to our format
            klines = []
            for candle in ohlcv:
                timestamp_ms = candle[0]
                open_price = candle[1]
                high_price = candle[2]
                low_price = candle[3]
                close_price = candle[4]
                volume = candle[5]

                # Calculate change
                change = close_price - open_price if open_price else 0
                percent = (change / open_price * 100) if open_price else 0

                klines.append(
                    {
                        "timestamp": int(timestamp_ms / 1000),  # Convert to seconds
                        "datetime_str": datetime.fromtimestamp(
                            timestamp_ms / 1000, tz=UTC
                        ).isoformat(),
                        "open": float(open_price) if open_price else None,
                        "high": float(high_price) if high_price else None,
                        "low": float(low_price) if low_price else None,
                        "close": float(close_price) if close_price else None,
                        "volume": float(volume) if volume else None,
                        "amount": float(volume * close_price) if volume and close_price else None,
                        "change": float(change),
                        "percent": float(percent),
                    }
                )

            logger.info(f"Got {len(klines)} klines for {formatted_symbol}")
            return klines

        except Exception as e:
            logger.error(f"Error fetching klines for {symbol}: {e}", exc_info=True)
            return []

    def get_market_status(self, symbol: str) -> dict[str, Any]:
        """Get market status for a symbol"""
        try:
            if not self.exchange:
                self._initialize_exchange()

            formatted_symbol = self._format_symbol(symbol)

            # Hyperliquid is 24/7, but we can check if the market exists
            markets = self.exchange.load_markets()
            market_exists = formatted_symbol in markets

            status = {
                "market_status": "OPEN" if market_exists else "CLOSED",
                "is_trading": market_exists,
                "symbol": formatted_symbol,
                "exchange": "Hyperliquid",
                "market_type": "crypto",
            }

            if market_exists:
                market_info = markets[formatted_symbol]
                status.update(
                    {
                        "base_currency": market_info.get("base"),
                        "quote_currency": market_info.get("quote"),
                        "active": market_info.get("active", True),
                    }
                )

            logger.info(f"Market status for {formatted_symbol}: {status['market_status']}")
            return status

        except Exception as e:
            logger.error(f"Error getting market status for {symbol}: {e}", exc_info=True)
            return {"market_status": "ERROR", "is_trading": False, "error": str(e)}

    def get_all_symbols(self) -> list[str]:
        """Get all available trading symbols"""
        try:
            if not self.exchange:
                self._initialize_exchange()

            markets = self.exchange.load_markets()
            symbols = list(markets.keys())

            # Filter for USDC pairs (both spot and perpetual)
            usdc_symbols = [s for s in symbols if "/USDC" in s]

            # Prioritize mainstream cryptos (perpetual swaps) and popular spot pairs
            mainstream_perps = [
                s
                for s in usdc_symbols
                if any(crypto in s for crypto in ["BTC/", "ETH/", "SOL/", "DOGE/", "BNB/", "XRP/"])
            ]
            other_symbols = [s for s in usdc_symbols if s not in mainstream_perps]

            # Return mainstream first, then others
            result = mainstream_perps + other_symbols[:50]

            logger.info(f"Found {len(usdc_symbols)} USDC trading pairs, returning {len(result)}")
            return result

        except Exception as e:
            logger.error(f"Error getting symbols: {e}", exc_info=True)
            return ["BTC/USD", "ETH/USD", "SOL/USD"]  # Fallback popular pairs

    def get_all_prices(self) -> dict[str, float]:
        """Get ALL market prices in ONE API call using Hyperliquid native SDK.

        This is the most efficient method to get prices for all available symbols.
        Uses the all_mids() endpoint which returns all mid prices in a single call.

        Returns:
            dict[str, float]: Dictionary mapping symbol (e.g., "BTC") to price
        """
        try:
            # Use native Hyperliquid SDK for maximum efficiency
            info = Info()
            all_mids = info.all_mids()

            # Convert string prices to float
            prices = {symbol: float(price) for symbol, price in all_mids.items()}

            logger.info(f"Got {len(prices)} prices from all_mids() endpoint in ONE API call")
            return prices

        except Exception as e:
            logger.error(f"Error fetching all prices: {e}", exc_info=True)
            return {}

    def _format_symbol(self, symbol: str) -> str:
        """Format symbol for CCXT (e.g., 'BTC' -> 'BTC/USDC:USDC')"""
        if "/" in symbol and ":" in symbol:
            return symbol
        elif "/" in symbol:
            # If it's BTC/USDC, convert to BTC/USDC:USDC for Hyperliquid
            return f"{symbol}:USDC"

        # For single symbols like 'BTC', use perpetual swap format
        # Hyperliquid primarily uses perpetual swaps with format: SYMBOL/USDC:USDC
        symbol_upper = symbol.upper()
        return f"{symbol_upper}/USDC:USDC"


# Global client instance
hyperliquid_client = HyperliquidClient()


def get_kline_data_from_hyperliquid(
    symbol: str, period: str = "1d", count: int = 100
) -> list[dict[str, Any]]:
    """Get kline data from Hyperliquid"""
    return hyperliquid_client.get_kline_data(symbol, period, count)


def get_market_status_from_hyperliquid(symbol: str) -> dict[str, Any]:
    """Get market status from Hyperliquid"""
    return hyperliquid_client.get_market_status(symbol)


def get_all_symbols_from_hyperliquid() -> list[str]:
    """Get all available symbols from Hyperliquid"""
    return hyperliquid_client.get_all_symbols()


def get_all_prices_from_hyperliquid() -> dict[str, float]:
    """Get ALL market prices from Hyperliquid in ONE API call.

    This is the most efficient method - returns 468+ prices in a single call.
    Uses the all_mids() endpoint from Hyperliquid native SDK.

    Returns:
        dict[str, float]: Dictionary mapping symbol (e.g., "BTC") to price
    """
    return hyperliquid_client.get_all_prices()
