"""Account repository for async database operations."""

from decimal import Decimal

from database.models import Account, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session


class AccountRepository:
    """Repository for Account CRUD operations."""

    @staticmethod
    async def get_by_id(db: AsyncSession, account_id: int) -> Account | None:
        """Get account by ID.

        Args:
            db: Async database session
            account_id: Account ID to fetch

        Returns:
            Account instance or None if not found
        """
        result = await db.execute(select(Account).where(Account.id == account_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def get_all_active(db: AsyncSession) -> list[Account]:
        """Get all active accounts.

        Args:
            db: Async database session

        Returns:
            List of active Account instances
        """
        result = await db.execute(
            select(Account).where(Account.is_active == True)  # noqa: E712
        )
        return list(result.scalars().all())

    @staticmethod
    async def update_balance(
        db: AsyncSession,
        account: Account,
        current_cash: Decimal,
        frozen_cash: Decimal,
    ) -> Account:
        """Update account balance (synced from Hyperliquid).

        Args:
            db: Async database session
            account: Account instance to update
            current_cash: New current_cash value from Hyperliquid
            frozen_cash: New frozen_cash value from Hyperliquid

        Returns:
            Updated Account instance
        """
        account.current_cash = current_cash
        account.frozen_cash = frozen_cash
        await db.flush()
        return account


# Sync helper functions for legacy routes using sync Session
def get_account(db: Session, account_id: int) -> Account | None:
    """Get account by ID (sync version).

    Args:
        db: Sync database session
        account_id: Account ID

    Returns:
        Account instance or None if not found
    """
    return db.query(Account).filter(Account.id == account_id).first()


def get_or_create_default_account(db: Session, user_id: int) -> Account:
    """Get or create default account for user (sync version).

    Args:
        db: Sync database session
        user_id: User ID

    Returns:
        Account instance
    """
    # Try to get first active account for user
    account = db.query(Account).filter(
        Account.user_id == user_id,
        Account.is_active == True  # noqa: E712
    ).first()

    if account:
        return account

    # Create new default account
    account = Account(
        user_id=user_id,
        name="Default Account",
        initial_capital=Decimal("10000"),
        current_cash=Decimal("10000"),
        frozen_cash=Decimal("0"),
        is_active=True,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account
