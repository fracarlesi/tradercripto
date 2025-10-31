"""Position repository for async database operations."""

from database.models import Position
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session


class PositionRepository:
    """Repository for Position CRUD operations.

    Positions use clear-recreate sync strategy: DELETE all → INSERT fresh from Hyperliquid.
    """

    @staticmethod
    async def clear_positions(db: AsyncSession, account_id: int) -> None:
        """Delete all positions for an account (part of clear-recreate strategy).

        Args:
            db: Async database session
            account_id: Account ID whose positions to clear

        Returns:
            None
        """
        await db.execute(delete(Position).where(Position.account_id == account_id))
        await db.flush()

    @staticmethod
    async def bulk_create_positions(
        db: AsyncSession, positions_list: list[Position]
    ) -> list[Position]:
        """Insert multiple positions (part of clear-recreate strategy).

        Args:
            db: Async database session
            positions_list: List of Position instances to insert

        Returns:
            List of inserted Position instances
        """
        db.add_all(positions_list)
        await db.flush()
        return positions_list

    @staticmethod
    async def get_by_account(db: AsyncSession, account_id: int) -> list[Position]:
        """Get all positions for an account.

        Args:
            db: Async database session
            account_id: Account ID to fetch positions for

        Returns:
            List of Position instances
        """
        result = await db.execute(select(Position).where(Position.account_id == account_id))
        return list(result.scalars().all())


# Sync helper functions for legacy routes using sync Session
def list_positions(db: Session, account_id: int) -> list[Position]:
    """Get positions for account (sync version).

    Args:
        db: Sync database session
        account_id: Account ID

    Returns:
        List of Position instances
    """
    return db.query(Position).filter(Position.account_id == account_id).all()
