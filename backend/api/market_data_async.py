"""Async Market Data API Routes (Migrated from market_data_routes.py).

This module provides async versions of critical market data endpoints.
Use these as reference for migrating remaining routes.
"""

from config.logging import get_logger
from database.connection import get_db
from database.models import CryptoKline
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)

router = APIRouter(prefix="/api/market", tags=["market-async"])


class PriceResponse(BaseModel):
    """Price response model."""

    symbol: str
    market: str
    price: float
    timestamp: int


class KlineItem(BaseModel):
    """K-line data item model."""

    timestamp: int
    datetime: str
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None


class KlineResponse(BaseModel):
    """K-line data response model."""

    symbol: str
    market: str
    period: str
    count: int
    data: list[KlineItem]


@router.get("/prices/async", response_model=list[PriceResponse])
async def get_multiple_prices_async(
    symbols: str, market: str = "hyperliquid", db: AsyncSession = Depends(get_db)
):
    """Get latest prices for multiple cryptos in batch (async version) (T060).

    This endpoint fetches prices from the database instead of external API calls.
    For real-time prices from Hyperliquid API, use the sync version.

    Args:
        symbols: Comma-separated list of crypto symbols (e.g., "BTC,ETH,SOL")
        market: Market name (default: hyperliquid)
        db: Database session

    Returns:
        List of price responses with latest kline close prices
    """
    try:
        symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]

        if not symbol_list:
            raise HTTPException(status_code=400, detail="Crypto symbol list cannot be empty")

        if len(symbol_list) > 20:
            raise HTTPException(status_code=400, detail="Maximum 20 crypto symbols supported")

        results = []
        import time

        current_timestamp = int(time.time() * 1000)

        # Fetch latest kline for each symbol from database
        for symbol in symbol_list:
            try:
                # Get most recent 1-minute kline to use as current price
                result = await db.execute(
                    select(CryptoKline)
                    .where(
                        CryptoKline.symbol == symbol,
                        CryptoKline.market == market,
                        CryptoKline.period == "1m",
                    )
                    .order_by(CryptoKline.timestamp.desc())
                    .limit(1)
                )
                kline = result.scalar_one_or_none()

                if kline and kline.close is not None:
                    results.append(
                        PriceResponse(
                            symbol=symbol,
                            market=market,
                            price=float(kline.close),
                            timestamp=current_timestamp,
                        )
                    )
                else:
                    logger.warning(f"No kline data found for {symbol} in market {market}")

            except Exception as e:
                logger.warning(
                    f"Failed to get {symbol} price from database",
                    extra={"context": {"error": str(e)}},
                )

        return results

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Failed to batch get crypto prices",
            extra={"context": {"error": str(e)}},
        )
        raise HTTPException(status_code=500, detail=f"Failed to batch get crypto prices: {str(e)}")


@router.get("/klines/async/{symbol}", response_model=KlineResponse)
async def get_crypto_klines_async(
    symbol: str,
    market: str = "hyperliquid",
    period: str = "1m",
    count: int = 100,
    db: AsyncSession = Depends(get_db),
):
    """Get crypto K-line data from database (async version) (T060).

    Args:
        symbol: Crypto symbol (e.g., "BTC")
        market: Market name (default: hyperliquid)
        period: Time period (1m, 5m, 15m, 30m, 1h, 1d)
        count: Number of data points (default: 100, max: 500)
        db: Database session

    Returns:
        K-line data response
    """
    try:
        # Parameter validation
        valid_periods = ["1m", "5m", "15m", "30m", "1h", "1d"]
        if period not in valid_periods:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported time period. Supported: {', '.join(valid_periods)}",
            )

        if count <= 0 or count > 500:
            raise HTTPException(status_code=400, detail="Data count must be between 1-500")

        # Query database for kline data
        result = await db.execute(
            select(CryptoKline)
            .where(
                CryptoKline.symbol == symbol,
                CryptoKline.market == market,
                CryptoKline.period == period,
            )
            .order_by(CryptoKline.timestamp.desc())
            .limit(count)
        )
        klines = result.scalars().all()

        # Convert to response format
        kline_items = []
        for kline in reversed(klines):  # Reverse to get chronological order
            kline_items.append(
                KlineItem(
                    timestamp=kline.timestamp,
                    datetime=kline.datetime.isoformat() if kline.datetime else "",
                    open=float(kline.open) if kline.open is not None else None,
                    high=float(kline.high) if kline.high is not None else None,
                    low=float(kline.low) if kline.low is not None else None,
                    close=float(kline.close) if kline.close is not None else None,
                    volume=float(kline.volume) if kline.volume is not None else None,
                )
            )

        return KlineResponse(
            symbol=symbol,
            market=market,
            period=period,
            count=len(kline_items),
            data=kline_items,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Failed to get K-line data",
            extra={"context": {"symbol": symbol, "error": str(e)}},
        )
        raise HTTPException(status_code=500, detail=f"Failed to get K-line data: {str(e)}")
