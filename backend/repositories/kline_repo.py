"""K-line (candlestick) data repository for async database operations."""

import time
from typing import Any

from database.models import CryptoKline
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession


class KlineRepository:
    """Repository for CryptoKline CRUD operations."""

    @staticmethod
    async def save_kline_data(
        db: AsyncSession,
        symbol: str,
        market: str,
        period: str,
        kline_data: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Save K-line data to database (upsert mode).

        Args:
            db: Async database session
            symbol: Trading symbol
            market: Market identifier
            period: Time period (e.g., "1m", "5m", "1h")
            kline_data: List of kline dicts with OHLCV data

        Returns:
            Dict with counts: {"inserted": N, "updated": M, "total": N+M}
        """
        inserted_count = 0
        updated_count = 0

        for item in kline_data:
            timestamp = item.get("timestamp")
            if not timestamp:
                continue

            # Check if record exists
            result = await db.execute(
                select(CryptoKline).where(
                    and_(
                        CryptoKline.symbol == symbol,
                        CryptoKline.market == market,
                        CryptoKline.period == period,
                        CryptoKline.timestamp == timestamp,
                    )
                )
            )
            existing = result.scalar_one_or_none()

            kline_dict = {
                "symbol": symbol,
                "market": market,
                "period": period,
                "timestamp": timestamp,
                "datetime_str": item.get("datetime", ""),
                "open_price": item.get("open"),
                "high_price": item.get("high"),
                "low_price": item.get("low"),
                "close_price": item.get("close"),
                "volume": item.get("volume"),
                "amount": item.get("amount"),
                "change": item.get("chg"),
                "percent": item.get("percent"),
            }

            if existing:
                # Update existing record
                for key, value in kline_dict.items():
                    if key not in ["symbol", "market", "period", "timestamp"]:
                        setattr(existing, key, value)
                updated_count += 1
            else:
                # Insert new record
                kline_record = CryptoKline(**kline_dict)
                db.add(kline_record)
                inserted_count += 1

        if inserted_count > 0 or updated_count > 0:
            await db.flush()

        return {
            "inserted": inserted_count,
            "updated": updated_count,
            "total": inserted_count + updated_count,
        }

    @staticmethod
    async def get_kline_data(
        db: AsyncSession,
        symbol: str,
        market: str,
        period: str,
        limit: int = 100,
    ) -> list[CryptoKline]:
        """Get K-line data.

        Args:
            db: Async database session
            symbol: Trading symbol
            market: Market identifier
            period: Time period
            limit: Maximum number of records to return

        Returns:
            List of CryptoKline instances ordered by timestamp DESC
        """
        result = await db.execute(
            select(CryptoKline)
            .where(
                and_(
                    CryptoKline.symbol == symbol,
                    CryptoKline.market == market,
                    CryptoKline.period == period,
                )
            )
            .order_by(CryptoKline.timestamp.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def delete_old_kline_data(
        db: AsyncSession,
        symbol: str,
        market: str,
        period: str,
        keep_days: int = 30,
    ) -> int:
        """Delete old K-line data.

        Args:
            db: Async database session
            symbol: Trading symbol
            market: Market identifier
            period: Time period
            keep_days: Number of days to keep (default 30)

        Returns:
            Number of records deleted
        """
        cutoff_timestamp = int((time.time() - keep_days * 24 * 3600) * 1000)

        result = await db.execute(
            select(CryptoKline).where(
                and_(
                    CryptoKline.symbol == symbol,
                    CryptoKline.market == market,
                    CryptoKline.period == period,
                    CryptoKline.timestamp < cutoff_timestamp,
                )
            )
        )
        records = result.scalars().all()
        count = len(records)

        for record in records:
            await db.delete(record)

        await db.flush()
        return count
