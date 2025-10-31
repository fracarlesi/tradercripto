"""Order repository for async database operations."""

from database.models import Order
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session


class OrderRepository:
    """Repository for Order CRUD operations.

    Orders are deduplicated by unique order_no field from Hyperliquid.
    """

    @staticmethod
    async def get_by_order_no(db: AsyncSession, order_no: str) -> Order | None:
        """Get order by unique order number (for deduplication).

        Args:
            db: Async database session
            order_no: Unique order number from Hyperliquid

        Returns:
            Order instance or None if not found
        """
        result = await db.execute(select(Order).where(Order.order_no == order_no))
        return result.scalar_one_or_none()

    @staticmethod
    async def create_order(db: AsyncSession, order_data: Order) -> Order:
        """Insert a new order (typically from Hyperliquid fill).

        Args:
            db: Async database session
            order_data: Order instance to insert

        Returns:
            Inserted Order instance
        """
        db.add(order_data)
        await db.flush()
        return order_data

    @staticmethod
    async def get_by_account(db: AsyncSession, account_id: int, limit: int = 100) -> list[Order]:
        """Get recent orders for an account.

        Args:
            db: Async database session
            account_id: Account ID to fetch orders for
            limit: Maximum number of orders to return (default 100)

        Returns:
            List of Order instances ordered by created_at DESC
        """
        result = await db.execute(
            select(Order)
            .where(Order.account_id == account_id)
            .order_by(Order.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


# Sync helper functions for legacy routes using sync Session
def list_orders(db: Session, account_id: int, limit: int = 100) -> list[Order]:
    """Get orders for account (sync version).

    Args:
        db: Sync database session
        account_id: Account ID
        limit: Maximum number of orders

    Returns:
        List of Order instances ordered by created_at DESC
    """
    return (
        db.query(Order)
        .filter(Order.account_id == account_id)
        .order_by(Order.created_at.desc())
        .limit(limit)
        .all()
    )
