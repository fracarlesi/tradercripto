"""
Market Data Services

This module contains services for fetching and caching market data.
"""

import logging
from typing import Any

from .hyperliquid_market_data import (
    get_all_prices_from_hyperliquid,
    get_all_symbols_from_hyperliquid,
    get_kline_data_from_hyperliquid,
    get_market_status_from_hyperliquid,
)
from .news_cache import NewsFeedCache, get_news_cache
from .news_feed import fetch_latest_news, get_news_cache_stats
from .price_cache import (
    cache_price,
    clear_expired_prices,
    get_cached_price,
    get_price_cache_stats,
)

logger = logging.getLogger(__name__)


# High-level wrapper functions with caching
def get_last_price(symbol: str, market: str = "CRYPTO") -> float:
    """Get last price with caching support."""
    key = f"{symbol}.{market}"

    # Check cache first
    cached_price = get_cached_price(symbol, market)
    if cached_price is not None:
        logger.debug(f"Using cached price for {key}: {cached_price}")
        return cached_price

    logger.info(f"Getting real-time price for {key} from API...")

    try:
        # Get ALL prices using efficient all_mids() endpoint, then filter
        all_prices = get_all_prices_from_hyperliquid()
        price = all_prices.get(symbol)

        if price and price > 0:
            logger.info(f"Got real-time price for {key} from Hyperliquid: {price}")
            # Cache the price
            cache_price(symbol, market, price)
            return price
        raise Exception(f"Hyperliquid returned invalid price: {price}")
    except Exception as hl_err:
        logger.error(f"Failed to get price from Hyperliquid: {hl_err}", exc_info=True)
        raise Exception(f"Unable to get real-time price for {key}: {hl_err}")


def get_kline_data(
    symbol: str, market: str = "CRYPTO", period: str = "1d", count: int = 100
) -> list[dict[str, Any]]:
    """Get K-line data from Hyperliquid."""
    key = f"{symbol}.{market}"

    try:
        data = get_kline_data_from_hyperliquid(symbol, period, count)
        if data:
            logger.info(f"Got K-line data for {key} from Hyperliquid, total {len(data)} items")
            return data
        raise Exception("Hyperliquid returned empty K-line data")
    except Exception as hl_err:
        logger.error(f"Failed to get K-line data from Hyperliquid: {hl_err}", exc_info=True)
        raise Exception(f"Unable to get K-line data for {key}: {hl_err}")


def get_market_status(symbol: str, market: str = "CRYPTO") -> dict[str, Any]:
    """Get market status from Hyperliquid."""
    key = f"{symbol}.{market}"

    try:
        status = get_market_status_from_hyperliquid(symbol)
        logger.info(
            f"Retrieved market status for {key} from Hyperliquid: {status.get('market_status')}"
        )
        return status
    except Exception as hl_err:
        logger.error(f"Failed to get market status: {hl_err}", exc_info=True)
        raise Exception(f"Unable to get market status for {key}: {hl_err}")


def get_all_symbols() -> list[str]:
    """Get all available trading pairs."""
    try:
        symbols = get_all_symbols_from_hyperliquid()
        logger.info(f"Got {len(symbols)} trading pairs from Hyperliquid")
        return symbols
    except Exception as hl_err:
        logger.error(f"Failed to get trading pairs list: {hl_err}", exc_info=True)
        return ["BTC/USD", "ETH/USD", "SOL/USD"]  # default trading pairs


__all__ = [
    # News cache
    "NewsFeedCache",
    "get_news_cache",
    "fetch_latest_news",
    "get_news_cache_stats",
    # Price cache
    "get_cached_price",
    "cache_price",
    "clear_expired_prices",
    "get_price_cache_stats",
    # Hyperliquid market data (low-level)
    "get_all_prices_from_hyperliquid",
    "get_kline_data_from_hyperliquid",
    "get_market_status_from_hyperliquid",
    "get_all_symbols_from_hyperliquid",
    # High-level wrapper functions
    "get_last_price",
    "get_kline_data",
    "get_market_status",
    "get_all_symbols",
]
