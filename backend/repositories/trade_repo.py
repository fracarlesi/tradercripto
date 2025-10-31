"""Trade repository for async database operations."""

from datetime import datetime
from decimal import Decimal

from database.models import Trade
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class TradeRepository:
    """Repository for Trade CRUD operations.

    Trades are deduplicated by composite key: (trade_time, symbol, quantity, price).
    """

    @staticmethod
    async def find_duplicate(
        db: AsyncSession,
        trade_time: datetime,
        symbol: str,
        quantity: Decimal,
        price: Decimal,
    ) -> Trade | None:
        """Check for existing trade by composite unique key (deduplication).

        Args:
            db: Async database session
            trade_time: Trade execution timestamp
            symbol: Trading symbol
            quantity: Trade quantity
            price: Trade price

        Returns:
            Trade instance if duplicate exists, None otherwise
        """
        result = await db.execute(
            select(Trade).where(
                Trade.trade_time == trade_time,
                Trade.symbol == symbol,
                Trade.quantity == quantity,
                Trade.price == price,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def create_trade(db: AsyncSession, trade_data: Trade) -> Trade:
        """Insert a new trade (with deduplication check recommended before calling).

        Args:
            db: Async database session
            trade_data: Trade instance to insert

        Returns:
            Inserted Trade instance
        """
        db.add(trade_data)
        await db.flush()
        return trade_data

    @staticmethod
    async def get_by_account(db: AsyncSession, account_id: int, limit: int = 100) -> list[Trade]:
        """Get recent trades for an account.

        Args:
            db: Async database session
            account_id: Account ID to fetch trades for
            limit: Maximum number of trades to return (default 100)

        Returns:
            List of Trade instances ordered by trade_time DESC
        """
        result = await db.execute(
            select(Trade)
            .where(Trade.account_id == account_id)
            .order_by(Trade.trade_time.desc())
            .limit(limit)
        )
        return list(result.scalars().all())
