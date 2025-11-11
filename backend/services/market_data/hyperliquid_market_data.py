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
        """Get kline/candlestick data for a symbol using Hyperliquid native SDK

        Uses info.candles_snapshot() instead of CCXT (which has infinite recursion bug).
        This is more reliable and efficient as it's the official Hyperliquid SDK.

        Args:
            symbol: Trading pair symbol (e.g., "BTC", "ETH")
            period: Timeframe - "1m", "5m", "15m", "1h", "1d" (default: "1d")
            count: Number of candles to fetch (default: 100, max: 5000)

        Returns:
            List of candlestick data with OHLCV and calculated metrics
        """
        try:
            # Use Hyperliquid native SDK (Info class)
            info = Info(skip_ws=True)  # skip_ws=True avoids websocket overhead

            # Extract coin name (remove /USDC or :USDC suffix if present)
            coin = symbol.split("/")[0].split(":")[0].upper()

            # Map our period format to Hyperliquid interval format
            interval_map = {
                "1m": "1m",
                "5m": "5m",
                "15m": "15m",
                "30m": "30m",  # Not sure if Hyperliquid supports this
                "1h": "1h",
                "4h": "4h",  # Hyperliquid also supports 4h
                "1d": "1d",
            }
            interval = interval_map.get(period, "1d")

            # Calculate time range (count candles back from now)
            # Hyperliquid API expects startTime and endTime in milliseconds
            import time

            end_time_ms = int(time.time() * 1000)  # Current time in ms

            # Calculate start time based on interval and count
            interval_minutes = {
                "1m": 1,
                "5m": 5,
                "15m": 15,
                "30m": 30,
                "1h": 60,
                "4h": 240,
                "1d": 1440,
            }
            minutes_back = interval_minutes.get(interval, 1440) * count
            start_time_ms = end_time_ms - (minutes_back * 60 * 1000)

            # Fetch candles from Hyperliquid API
            # Note: parameter is 'name' not 'coin'
            candles = info.candles_snapshot(
                name=coin, interval=interval, startTime=start_time_ms, endTime=end_time_ms
            )

            # Convert Hyperliquid format to our standard format
            klines = []
            for candle in candles:
                # Hyperliquid candle format:
                # {
                #   "t": <timestamp_open_ms>,
                #   "T": <timestamp_close_ms>,
                #   "s": <coin>,
                #   "i": <interval>,
                #   "o": <open_price_str>,
                #   "c": <close_price_str>,
                #   "h": <high_price_str>,
                #   "l": <low_price_str>,
                #   "v": <volume_str>,
                #   "n": <num_trades>
                # }
                timestamp_ms = candle.get("t", 0)
                open_price = float(candle.get("o", 0))
                high_price = float(candle.get("h", 0))
                low_price = float(candle.get("l", 0))
                close_price = float(candle.get("c", 0))
                volume = float(candle.get("v", 0))

                # Calculate change and percent
                change = close_price - open_price if open_price else 0
                percent = (change / open_price * 100) if open_price else 0

                klines.append(
                    {
                        "timestamp": int(timestamp_ms / 1000),  # Convert to seconds
                        "datetime_str": datetime.fromtimestamp(
                            timestamp_ms / 1000, tz=UTC
                        ).isoformat(),
                        "open": open_price,
                        "high": high_price,
                        "low": low_price,
                        "close": close_price,
                        "volume": volume,
                        "amount": volume * close_price,  # Total value traded
                        "change": change,
                        "percent": percent,
                    }
                )

            logger.info(
                f"✅ Got {len(klines)} klines for {coin} using Hyperliquid SDK (period={period})"
            )
            return klines

        except Exception as e:
            logger.error(
                f"Error fetching klines for {symbol} from Hyperliquid SDK: {e}", exc_info=True
            )
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
