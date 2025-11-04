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
    # Balance data fetched from Hyperliquid API, not stored in DB
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
    # Balance data fetched from Hyperliquid API, not stored in DB
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
            .where(Account.is_active == True)
        )
        rows = result.all()

        # Balance data fetched from Hyperliquid API, not stored in DB
        accounts = []
        for account, user in rows:
            accounts.append(
                AccountResponse(
                    id=account.id,
                    user_id=account.user_id,
                    username=user.username,
                    name=account.name,
                    account_type=account.account_type,
                    model=account.model,
                    base_url=account.base_url,
                    api_key=account.api_key,
                    is_active=account.is_active,
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

    Note:
        All financial data (balance, positions value) is fetched in real-time from Hyperliquid.
        Database is used only for account metadata and counting records.
    """
    try:
        # Get account metadata from database (NOT positions - fetched from Hyperliquid)
        result = await db.execute(
            select(Account).where(Account.id == account_id, Account.is_active == True)
        )
        account = result.scalar_one_or_none()

        if not account:
            raise HTTPException(status_code=404, detail="Account not found")

        # Fetch real-time data from Hyperliquid (single source of truth)
        from services.trading.hyperliquid_trading_service import hyperliquid_trading_service

        user_state = await hyperliquid_trading_service.get_user_state_async()
        margin_summary = user_state.get('marginSummary', {})

        # Real-time account balance from Hyperliquid
        account_value = float(margin_summary.get('accountValue', '0'))
        total_margin_used = float(margin_summary.get('totalMarginUsed', '0'))
        available_cash = account_value - total_margin_used

        # Get real-time positions from Hyperliquid (NOT from database - may be stale)
        hyperliquid_positions = user_state.get('assetPositions', [])

        # Calculate positions value from Hyperliquid position values
        positions_value = 0.0
        for pos in hyperliquid_positions:
            position_data = pos.get('position', {})
            # Use positionValue from Hyperliquid (already calculated correctly for LONG/SHORT)
            pos_value = abs(float(position_data.get('positionValue', 0)))
            positions_value += pos_value

        # Count positions from Hyperliquid (single source of truth)
        positions_count = len(hyperliquid_positions)

        # Count pending orders
        result = await db.execute(
            select(func.count())
            .select_from(Order)
            .where(Order.account_id == account_id, Order.status == "PENDING")
        )
        pending_orders = result.scalar()

        # Balance data fetched from Hyperliquid API, not stored in DB
        return AccountOverview(
            account={
                "id": account.id,
                "name": account.name,
                "account_type": account.account_type,
                "available_cash": available_cash,  # Real-time from Hyperliquid
                "margin_used": total_margin_used,  # Real-time from Hyperliquid
            },
            total_assets=account_value,  # Real-time total from Hyperliquid
            positions_value=positions_value,  # Calculated with current market prices
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

        # Balance data fetched from Hyperliquid API, not stored in DB
        # Create account
        account = Account(
            user_id=user.id,
            name=data.name,
            account_type=data.account_type,
            model=data.model,
            base_url=data.base_url,
            api_key=data.api_key or "default-key-please-update",
            is_active="true",
        )

        db.add(account)
        await db.flush()
        await db.refresh(account)

        # Balance data fetched from Hyperliquid API, not stored in DB
        return AccountResponse(
            id=account.id,
            user_id=account.user_id,
            username=user.username,
            name=account.name,
            account_type=account.account_type,
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

        # Balance data fetched from Hyperliquid API, not stored in DB
        return AccountResponse(
            id=account.id,
            user_id=account.user_id,
            username=user.username,
            name=account.name,
            account_type=account.account_type,
            model=account.model,
            base_url=account.base_url,
            api_key=account.api_key,
            is_active=account.is_active,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Failed to update account",
            extra={"context": {"account_id": account_id, "error": str(e)}},
        )
        raise HTTPException(status_code=500, detail=f"Failed to update account: {str(e)}")
