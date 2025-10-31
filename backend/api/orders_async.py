"""Async Order Management API Routes (Migrated from order_routes.py).

This module provides async versions of critical order endpoints.
Use these as reference for migrating remaining routes.
"""

from config.logging import get_logger
from database.connection import get_db
from database.models import Account, Order
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)

router = APIRouter(prefix="/api/orders", tags=["orders-async"])


class OrderResponse(BaseModel):
    """Order response model."""

    id: int
    order_no: str
    user_id: int
    account_id: int
    symbol: str
    name: str
    side: str
    order_type: str
    price: float | None
    quantity: float
    filled_quantity: float
    status: str
    created_at: str
    updated_at: str | None = None


class OrderCreate(BaseModel):
    """Order creation request."""

    user_id: int
    symbol: str
    name: str
    side: str  # BUY/SELL
    order_type: str  # MARKET/LIMIT
    price: float | None = None
    quantity: float
    username: str | None = None
    password: str | None = None
    session_token: str | None = None


class OrderStats(BaseModel):
    """Order statistics."""

    total_orders: int
    pending_orders: int
    filled_orders: int
    cancelled_orders: int


@router.get("/async/user/{user_id}", response_model=list[OrderResponse])
async def get_user_orders_async(
    user_id: int, status: str | None = None, db: AsyncSession = Depends(get_db)
):
    """Get all orders for a user (async version) (T061).

    Args:
        user_id: User ID
        status: Filter by order status (PENDING/FILLED/CANCELLED)
        db: Database session

    Returns:
        List of user's orders
    """
    try:
        # Build query
        stmt = select(Order).where(Order.user_id == user_id)

        if status:
            stmt = stmt.where(Order.status == status)

        stmt = stmt.order_by(Order.created_at.desc())

        # Execute query
        result = await db.execute(stmt)
        orders = result.scalars().all()

        # Convert to response model
        return [
            OrderResponse(
                id=order.id,
                order_no=order.order_no,
                user_id=order.user_id,
                account_id=order.account_id,
                symbol=order.symbol,
                name=order.name,
                side=order.side,
                order_type=order.order_type,
                price=float(order.price) if order.price is not None else None,
                quantity=float(order.quantity),
                filled_quantity=float(order.filled_quantity),
                status=order.status,
                created_at=order.created_at.isoformat() if order.created_at else "",
                updated_at=(order.updated_at.isoformat() if order.updated_at else None),
            )
            for order in orders
        ]

    except Exception as e:
        logger.error(
            "Failed to get user orders",
            extra={"context": {"user_id": user_id, "error": str(e)}},
        )
        raise HTTPException(status_code=500, detail=f"Failed to get user orders: {str(e)}")


@router.get("/async/pending", response_model=list[OrderResponse])
async def get_pending_orders_async(user_id: int | None = None, db: AsyncSession = Depends(get_db)):
    """Get pending orders (async version) (T061).

    Args:
        user_id: User ID filter (optional)
        db: Database session

    Returns:
        List of pending orders
    """
    try:
        stmt = select(Order).where(Order.status == "PENDING")

        if user_id is not None:
            stmt = stmt.where(Order.user_id == user_id)

        stmt = stmt.order_by(Order.created_at.desc())

        result = await db.execute(stmt)
        orders = result.scalars().all()

        return [
            OrderResponse(
                id=order.id,
                order_no=order.order_no,
                user_id=order.user_id,
                account_id=order.account_id,
                symbol=order.symbol,
                name=order.name,
                side=order.side,
                order_type=order.order_type,
                price=float(order.price) if order.price is not None else None,
                quantity=float(order.quantity),
                filled_quantity=float(order.filled_quantity),
                status=order.status,
                created_at=order.created_at.isoformat() if order.created_at else "",
                updated_at=(order.updated_at.isoformat() if order.updated_at else None),
            )
            for order in orders
        ]

    except Exception as e:
        logger.error(
            "Failed to get pending orders",
            extra={"context": {"user_id": user_id, "error": str(e)}},
        )
        raise HTTPException(status_code=500, detail=f"Failed to get pending orders: {str(e)}")


@router.get("/async/order/{order_id}", response_model=OrderResponse)
async def get_order_details_async(order_id: int, db: AsyncSession = Depends(get_db)):
    """Get order details (async version) (T061).

    Args:
        order_id: Order ID
        db: Database session

    Returns:
        Order details
    """
    try:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()

        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        return OrderResponse(
            id=order.id,
            order_no=order.order_no,
            user_id=order.user_id,
            account_id=order.account_id,
            symbol=order.symbol,
            name=order.name,
            side=order.side,
            order_type=order.order_type,
            price=float(order.price) if order.price is not None else None,
            quantity=float(order.quantity),
            filled_quantity=float(order.filled_quantity),
            status=order.status,
            created_at=order.created_at.isoformat() if order.created_at else "",
            updated_at=order.updated_at.isoformat() if order.updated_at else None,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Failed to get order details",
            extra={"context": {"order_id": order_id, "error": str(e)}},
        )
        raise HTTPException(status_code=500, detail=f"Failed to get order details: {str(e)}")


@router.get("/async/stats", response_model=OrderStats)
async def get_order_stats_async(user_id: int | None = None, db: AsyncSession = Depends(get_db)):
    """Get order statistics (async version) (T061).

    Args:
        user_id: User ID filter (optional)
        db: Database session

    Returns:
        Order statistics
    """
    try:
        # Build base query
        base = select(func.count()).select_from(Order)
        if user_id is not None:
            base = base.where(Order.user_id == user_id)

        # Get total orders
        result = await db.execute(base)
        total_orders = result.scalar() or 0

        # Get pending orders
        result = await db.execute(base.where(Order.status == "PENDING"))
        pending_orders = result.scalar() or 0

        # Get filled orders
        result = await db.execute(base.where(Order.status == "FILLED"))
        filled_orders = result.scalar() or 0

        # Get cancelled orders
        result = await db.execute(base.where(Order.status == "CANCELLED"))
        cancelled_orders = result.scalar() or 0

        return OrderStats(
            total_orders=total_orders,
            pending_orders=pending_orders,
            filled_orders=filled_orders,
            cancelled_orders=cancelled_orders,
        )

    except Exception as e:
        logger.error(
            "Failed to get order stats",
            extra={"context": {"user_id": user_id, "error": str(e)}},
        )
        raise HTTPException(status_code=500, detail=f"Failed to get order stats: {str(e)}")


@router.post("/async/cancel/{order_id}")
async def cancel_order_async(
    order_id: int, reason: str = "User cancelled", db: AsyncSession = Depends(get_db)
):
    """Cancel an order (async version) (T061).

    Args:
        order_id: Order ID
        reason: Reason for cancellation
        db: Database session

    Returns:
        Cancellation result
    """
    try:
        # Get order
        result = await db.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()

        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if order.status != "PENDING":
            raise HTTPException(
                status_code=400,
                detail=f"Order status is {order.status}, cannot be cancelled",
            )

        # Get account to release frozen cash
        result = await db.execute(select(Account).where(Account.id == order.account_id))
        account = result.scalar_one_or_none()

        if account:
            # Release frozen cash
            frozen_amount = float(order.price or 0) * float(order.quantity)
            account.frozen_cash = float(account.frozen_cash) - frozen_amount
            account.current_cash = float(account.current_cash) + frozen_amount

        # Update order status
        order.status = "CANCELLED"
        order.notes = reason

        await db.flush()
        await db.refresh(order)

        logger.info(
            f"Order {order.order_no} cancelled",
            extra={"context": {"order_id": order_id, "reason": reason}},
        )

        return {"message": "Order cancelled successfully", "order_id": order_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Failed to cancel order",
            extra={"context": {"order_id": order_id, "error": str(e)}},
        )
        raise HTTPException(status_code=500, detail=f"Failed to cancel order: {str(e)}")
