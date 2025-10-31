"""Async Account API Routes (Migrated from account_routes.py).

This module provides async versions of critical account endpoints.
Use these as reference for migrating remaining routes.
"""

from decimal import Decimal

from config.logging import get_logger
from database.connection import get_db
from database.models import Account, Order, Position, User
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

logger = get_logger(__name__)

router = APIRouter(prefix="/api/accounts", tags=["accounts-async"])


class AccountResponse(BaseModel):
    """Account response model."""

    id: int
    user_id: int
    username: str
    name: str
    account_type: str
    initial_capital: float
    current_cash: float
    frozen_cash: float
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    is_active: bool


class AccountOverview(BaseModel):
    """Account overview response."""

    account: dict
    total_assets: float
    positions_value: float
    positions_count: int
    pending_orders: int


class AccountCreate(BaseModel):
    """Account creation request."""

    name: str
    account_type: str = "AI"
    initial_capital: float = 1000.0
    model: str | None = "deepseek-chat"
    base_url: str | None = "https://api.deepseek.com"
    api_key: str | None = None


class AccountUpdate(BaseModel):
    """Account update request."""

    name: str | None = None
    is_active: bool | None = None
    model: str | None = None
    api_key: str | None = None


@router.get("", response_model=list[AccountResponse])
async def list_accounts(db: AsyncSession = Depends(get_db)):
    """Get all active accounts (T059).

    Returns:
        List of active accounts with user information
    """
    try:
        # Query with join to get user information
        result = await db.execute(
            select(Account, User)
            .join(User, Account.user_id == User.id)
            .where(Account.is_active == "true")
        )
        rows = result.all()

        accounts = []
        for account, user in rows:
            accounts.append(
                AccountResponse(
                    id=account.id,
                    user_id=account.user_id,
                    username=user.username,
                    name=account.name,
                    account_type=account.account_type,
                    initial_capital=float(account.initial_capital),
                    current_cash=float(account.current_cash),
                    frozen_cash=float(account.frozen_cash),
                    model=account.model,
                    base_url=account.base_url,
                    api_key=account.api_key,
                    is_active=account.is_active == "true",
                )
            )

        return accounts

    except Exception as e:
        logger.error("Failed to list accounts", extra={"context": {"error": str(e)}})
        raise HTTPException(status_code=500, detail=f"Failed to list accounts: {str(e)}")


@router.get("/{account_id}", response_model=AccountOverview)
async def get_account_overview(account_id: int, db: AsyncSession = Depends(get_db)):
    """Get overview for a specific account (T059).

    Args:
        account_id: Account ID
        db: Database session

    Returns:
        Account overview with positions and orders summary
    """
    try:
        # Get account with positions eager loaded
        result = await db.execute(
            select(Account)
            .where(Account.id == account_id, Account.is_active == "true")
            .options(selectinload(Account.positions))
        )
        account = result.scalar_one_or_none()

        if not account:
            raise HTTPException(status_code=404, detail="Account not found")

        # Calculate positions value
        positions_value = 0.0
        for position in account.positions:
            # Simplified calculation - in production, get current market price
            positions_value += float(position.quantity * position.average_cost)

        # Count positions
        result = await db.execute(
            select(func.count())
            .select_from(Position)
            .where(Position.account_id == account_id, Position.quantity > 0)
        )
        positions_count = result.scalar()

        # Count pending orders
        result = await db.execute(
            select(func.count())
            .select_from(Order)
            .where(Order.account_id == account_id, Order.status == "PENDING")
        )
        pending_orders = result.scalar()

        return AccountOverview(
            account={
                "id": account.id,
                "name": account.name,
                "account_type": account.account_type,
                "current_cash": float(account.current_cash),
                "frozen_cash": float(account.frozen_cash),
            },
            total_assets=positions_value + float(account.current_cash),
            positions_value=positions_value,
            positions_count=positions_count or 0,
            pending_orders=pending_orders or 0,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Failed to get account overview",
            extra={"context": {"account_id": account_id, "error": str(e)}},
        )
        raise HTTPException(status_code=500, detail=f"Failed to get account overview: {str(e)}")


@router.post("", response_model=AccountResponse)
async def create_account(data: AccountCreate, db: AsyncSession = Depends(get_db)):
    """Create a new account (T059).

    Args:
        data: Account creation data
        db: Database session

    Returns:
        Created account
    """
    try:
        # Get or create default user
        from repositories.user_repo import UserRepository

        user = await UserRepository.get_or_create_user(db, username="default")

        # Create account
        account = Account(
            user_id=user.id,
            name=data.name,
            account_type=data.account_type,
            initial_capital=Decimal(str(data.initial_capital)),
            current_cash=Decimal(str(data.initial_capital)),
            frozen_cash=Decimal("0"),
            model=data.model,
            base_url=data.base_url,
            api_key=data.api_key or "default-key-please-update",
            is_active="true",
        )

        db.add(account)
        await db.flush()
        await db.refresh(account)

        return AccountResponse(
            id=account.id,
            user_id=account.user_id,
            username=user.username,
            name=account.name,
            account_type=account.account_type,
            initial_capital=float(account.initial_capital),
            current_cash=float(account.current_cash),
            frozen_cash=float(account.frozen_cash),
            model=account.model,
            base_url=account.base_url,
            api_key=account.api_key,
            is_active=True,
        )

    except Exception as e:
        logger.error(
            "Failed to create account",
            extra={"context": {"name": data.name, "error": str(e)}},
        )
        raise HTTPException(status_code=500, detail=f"Failed to create account: {str(e)}")


@router.put("/{account_id}", response_model=AccountResponse)
async def update_account(account_id: int, data: AccountUpdate, db: AsyncSession = Depends(get_db)):
    """Update an existing account (T059).

    Args:
        account_id: Account ID
        data: Account update data
        db: Database session

    Returns:
        Updated account
    """
    try:
        # Get account with user
        result = await db.execute(
            select(Account, User)
            .join(User, Account.user_id == User.id)
            .where(Account.id == account_id)
        )
        row = result.one_or_none()

        if not row:
            raise HTTPException(status_code=404, detail="Account not found")

        account, user = row

        # Update fields
        if data.name is not None:
            account.name = data.name
        if data.is_active is not None:
            account.is_active = "true" if data.is_active else "false"
        if data.model is not None:
            account.model = data.model
        if data.api_key is not None:
            account.api_key = data.api_key

        await db.flush()
        await db.refresh(account)

        return AccountResponse(
            id=account.id,
            user_id=account.user_id,
            username=user.username,
            name=account.name,
            account_type=account.account_type,
            initial_capital=float(account.initial_capital),
            current_cash=float(account.current_cash),
            frozen_cash=float(account.frozen_cash),
            model=account.model,
            base_url=account.base_url,
            api_key=account.api_key,
            is_active=account.is_active == "true",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Failed to update account",
            extra={"context": {"account_id": account_id, "error": str(e)}},
        )
        raise HTTPException(status_code=500, detail=f"Failed to update account: {str(e)}")
