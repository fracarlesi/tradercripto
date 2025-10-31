"""Order matching and execution service."""

import logging
from decimal import Decimal
from typing import Any

from database.models import Order
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def create_order(
    db: Session,
    account_id: int,
    symbol: str,
    side: str,
    quantity: Decimal,
    price: Decimal | None = None,
    order_type: str = "LIMIT",
) -> Order:
    """Create a new order.

    Args:
        db: Database session
        account_id: Trading account ID
        symbol: Trading symbol
        side: Order side (BUY/SELL)
        quantity: Order quantity
        price: Order price (None for market orders)
        order_type: Order type (LIMIT/MARKET)

    Returns:
        Created Order instance
    """
    order = Order(
        account_id=account_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        order_type=order_type,
        status="PENDING",
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    logger.info(
        f"Created {order_type} {side} order for {symbol}: "
        f"qty={quantity}, price={price}, order_id={order.id}"
    )
    return order


def cancel_order(db: Session, order_id: int) -> Order | None:
    """Cancel an existing order.

    Args:
        db: Database session
        order_id: Order ID to cancel

    Returns:
        Cancelled Order instance or None if not found
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        logger.warning(f"Order {order_id} not found for cancellation")
        return None

    if order.status in ("FILLED", "CANCELLED"):
        logger.warning(f"Order {order_id} already {order.status}, cannot cancel")
        return order

    order.status = "CANCELLED"
    db.commit()
    db.refresh(order)

    logger.info(f"Cancelled order {order_id}")
    return order


def check_and_execute_order(db: Session, order: Order) -> bool:
    """Check if order can be executed and execute if conditions met.

    Args:
        db: Database session
        order: Order to check and execute

    Returns:
        True if order was executed, False otherwise
    """
    if order.status != "PENDING":
        return False

    # Placeholder: In production, this would check market conditions
    # and execute against Hyperliquid API
    logger.info(f"Checking order {order.id} for execution (placeholder)")

    # For now, immediately mark market orders as filled
    if order.order_type == "MARKET":
        order.status = "FILLED"
        db.commit()
        logger.info(f"Executed market order {order.id}")
        return True

    return False


def get_pending_orders(db: Session, account_id: int | None = None) -> list[Order]:
    """Get all pending orders.

    Args:
        db: Database session
        account_id: Optional account ID filter

    Returns:
        List of pending orders
    """
    query = db.query(Order).filter(Order.status == "PENDING")

    if account_id is not None:
        query = query.filter(Order.account_id == account_id)

    orders = query.all()
    logger.debug(f"Retrieved {len(orders)} pending orders")
    return orders


def process_all_pending_orders(db: Session) -> dict[str, Any]:
    """Process all pending orders.

    Args:
        db: Database session

    Returns:
        Summary dict with processed/executed counts
    """
    pending_orders = get_pending_orders(db)
    executed_count = 0
    failed_count = 0

    for order in pending_orders:
        try:
            if check_and_execute_order(db, order):
                executed_count += 1
        except Exception as e:
            logger.error(f"Failed to process order {order.id}: {e}")
            failed_count += 1

    logger.info(
        f"Processed {len(pending_orders)} pending orders: "
        f"{executed_count} executed, {failed_count} failed"
    )

    return {
        "total_processed": len(pending_orders),
        "executed": executed_count,
        "failed": failed_count,
        "pending": len(pending_orders) - executed_count - failed_count,
    }
