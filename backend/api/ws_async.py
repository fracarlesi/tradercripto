"""Async WebSocket Handler (Migrated from ws.py) (T063-T064).

This module provides async WebSocket support with:
- AsyncSession for database operations
- Connection pooling
- Proper cleanup on disconnect
"""

import json
from datetime import datetime

from config.logging import get_logger
from database.connection import get_db
from database.models import (
    Account,
    AIDecisionLog,
    Order,
    Position,
    Trade,
)
from repositories.account_repo import AccountRepository
from repositories.user_repo import UserRepository
from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)


class AsyncConnectionManager:
    """Async WebSocket connection manager with connection pooling (T064)."""

    def __init__(self):
        """Initialize connection manager."""
        self.active_connections: dict[int, set[WebSocket]] = {}

    async def connect(self, websocket: WebSocket):
        """Accept WebSocket connection (already done in endpoint)."""
        pass

    def register(self, account_id: int, websocket: WebSocket):
        """Register WebSocket for account.

        Args:
            account_id: Account ID
            websocket: WebSocket connection
        """
        self.active_connections.setdefault(account_id, set()).add(websocket)
        logger.info(
            f"WebSocket registered for account {account_id}",
            extra={
                "context": {
                    "account_id": account_id,
                    "total_connections": len(self.active_connections[account_id]),
                }
            },
        )

    def unregister(self, account_id: int, websocket: WebSocket):
        """Unregister WebSocket for account.

        Args:
            account_id: Account ID
            websocket: WebSocket connection
        """
        if account_id in self.active_connections:
            self.active_connections[account_id].discard(websocket)
            if not self.active_connections[account_id]:
                del self.active_connections[account_id]
                logger.info(
                    f"Last WebSocket unregistered for account {account_id}",
                    extra={"context": {"account_id": account_id}},
                )

    async def send_to_account(self, account_id: int, message: dict):
        """Send message to all connections for account.

        Args:
            account_id: Account ID
            message: Message to send
        """
        if account_id not in self.active_connections:
            return

        payload = json.dumps(message, ensure_ascii=False)
        for ws in list(self.active_connections[account_id]):
            try:
                if ws.client_state.name != "CONNECTED":
                    self.active_connections[account_id].discard(ws)
                    continue
                await ws.send_text(payload)
            except Exception as e:
                logger.warning(
                    "Failed to send message to WebSocket",
                    extra={"context": {"account_id": account_id, "error": str(e)}},
                )
                self.active_connections[account_id].discard(ws)

    async def broadcast_to_all(self, message: dict):
        """Broadcast message to all connected clients.

        Args:
            message: Message to broadcast
        """
        payload = json.dumps(message, ensure_ascii=False)
        for account_id, websockets in list(self.active_connections.items()):
            for ws in list(websockets):
                try:
                    if ws.client_state.name != "CONNECTED":
                        websockets.discard(ws)
                        continue
                    await ws.send_text(payload)
                except Exception as e:
                    logger.warning(
                        "Failed to broadcast message",
                        extra={"context": {"account_id": account_id, "error": str(e)}},
                    )
                    websockets.discard(ws)

    def get_connection_count(self, account_id: int | None = None) -> int:
        """Get connection count.

        Args:
            account_id: Account ID (if None, returns total)

        Returns:
            Number of active connections
        """
        if account_id is not None:
            return len(self.active_connections.get(account_id, set()))
        return sum(len(conns) for conns in self.active_connections.values())


manager = AsyncConnectionManager()


async def _send_snapshot_async(db: AsyncSession, account_id: int):
    """Send account snapshot via WebSocket (async version).

    Args:
        db: Database session
        account_id: Account ID
    """
    # Get account
    result = await db.execute(select(Account).where(Account.id == account_id))
    account = result.scalar_one_or_none()

    if not account:
        logger.warning(
            "Account not found for snapshot",
            extra={"context": {"account_id": account_id}},
        )
        return

    # Get positions
    result = await db.execute(select(Position).where(Position.account_id == account_id))
    positions = result.scalars().all()

    # Get orders (limit to recent 20)
    result = await db.execute(
        select(Order)
        .where(Order.account_id == account_id)
        .order_by(Order.created_at.desc())
        .limit(20)
    )
    orders = result.scalars().all()

    # Get trades (limit to recent 20)
    result = await db.execute(
        select(Trade)
        .where(Trade.account_id == account_id)
        .order_by(Trade.trade_time.desc())
        .limit(20)
    )
    trades = result.scalars().all()

    # Get AI decisions (limit to recent 20)
    result = await db.execute(
        select(AIDecisionLog)
        .where(AIDecisionLog.account_id == account_id)
        .order_by(AIDecisionLog.decision_time.desc())
        .limit(20)
    )
    ai_decisions = result.scalars().all()

    # Calculate positions value
    positions_value = sum(float(p.quantity) * float(p.average_cost) for p in positions)

    # Build overview
    overview = {
        "account": {
            "id": account.id,
            "user_id": account.user_id,
            "name": account.name,
            "account_type": account.account_type,
            "initial_capital": float(account.initial_capital),
            "current_cash": float(account.current_cash),
            "frozen_cash": float(account.frozen_cash),
        },
        "total_assets": positions_value + float(account.current_cash),
        "positions_value": positions_value,
    }

    # Enrich positions (simplified - no real-time price fetching)
    enriched_positions = [
        {
            "id": p.id,
            "account_id": p.account_id,
            "symbol": p.symbol,
            "name": p.symbol,  # Position model doesn't have name field
            "market": "CRYPTO",  # Hyperliquid is crypto only
            "quantity": float(p.quantity),
            "available_quantity": float(p.available_quantity),
            "avg_cost": float(p.average_cost),
            "last_price": None,  # Would need async price fetching
            "market_value": None,
        }
        for p in positions
    ]

    # Build response
    response_data = {
        "type": "snapshot",
        "overview": overview,
        "positions": enriched_positions,
        "orders": [
            {
                "id": o.id,
                "order_no": o.order_no,
                "user_id": o.account_id,
                "symbol": o.symbol,
                "name": o.symbol,
                "market": "CRYPTO",
                "side": o.side,
                "order_type": o.order_type,
                "price": float(o.price) if o.price is not None else None,
                "quantity": float(o.quantity),
                "filled_quantity": float(o.filled_quantity),
                "status": o.status,
            }
            for o in orders
        ],
        "trades": [
            {
                "id": t.id,
                "order_id": t.order_id,
                "user_id": t.account_id,
                "symbol": t.symbol,
                "name": t.symbol,  # Trade model does not have name field
                "market": "CRYPTO",  # Hyperliquid is crypto only
                "side": t.side,
                "price": float(t.price),
                "quantity": float(t.quantity),
                "commission": float(t.commission),
                "trade_time": str(t.trade_time),
            }
            for t in trades
        ],
        "ai_decisions": [
            {
                "id": d.id,
                "decision_time": str(d.decision_time),
                "reason": d.reason,
                "operation": d.operation,
                "symbol": d.symbol,
                "prev_portion": float(d.prev_portion),
                "target_portion": float(d.target_portion),
                "total_balance": float(d.total_balance),
                "executed": str(d.executed).lower() if d.executed else "false",
                "order_id": d.order_id,
            }
            for d in ai_decisions
        ],
        "timestamp": datetime.now().timestamp(),
    }

    await manager.send_to_account(account_id, response_data)


async def websocket_endpoint_async(websocket: WebSocket):
    """Async WebSocket endpoint (T063).

    Args:
        websocket: WebSocket connection
    """
    await websocket.accept()
    account_id: int | None = None
    user_id: int | None = None

    try:
        while True:
            # Check connection state
            if websocket.client_state.name != "CONNECTED":
                break

            try:
                data = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.error(
                    "WebSocket receive error",
                    extra={"context": {"error": str(e)}},
                )
                break

            try:
                msg = json.loads(data)
            except json.JSONDecodeError as e:
                logger.error(
                    "Invalid JSON received",
                    extra={"context": {"error": str(e)}},
                )
                try:
                    await websocket.send_text(
                        json.dumps({"type": "error", "message": "Invalid JSON format"})
                    )
                except Exception:
                    break
                continue

            kind = msg.get("type")

            # Use async context manager for database session (T064)
            async for db in get_db():
                try:
                    if kind == "bootstrap":
                        # Get or create user
                        username = msg.get("username", "default")
                        user = await UserRepository.get_or_create_user(db, username)

                        # Get or create default account
                        initial_capital = float(msg.get("initial_capital", 1000.0))
                        account = await AccountRepository.get_or_create_default_account(
                            db, user.id
                        )

                        account_id = account.id
                        manager.register(account_id, websocket)

                        # Send confirmation
                        try:
                            await manager.send_to_account(
                                account_id,
                                {
                                    "type": "bootstrap_ok",
                                    "user": {"id": user.id, "username": user.username},
                                    "account": {
                                        "id": account.id,
                                        "name": account.name,
                                        "user_id": account.user_id,
                                    },
                                },
                            )
                            await _send_snapshot_async(db, account_id)
                        except Exception as e:
                            logger.error(
                                "Failed to send bootstrap response",
                                extra={"context": {"error": str(e)}},
                            )
                            break

                    elif kind == "switch_account":
                        # Switch to different account
                        target_account_id = msg.get("account_id")
                        if not target_account_id:
                            await websocket.send_text(
                                json.dumps({"type": "error", "message": "account_id required"})
                            )
                            continue

                        # Unregister from current account
                        if account_id is not None:
                            manager.unregister(account_id, websocket)

                        # Get target account
                        result = await db.execute(
                            select(Account).where(Account.id == target_account_id)
                        )
                        target_account = result.scalar_one_or_none()

                        if not target_account:
                            await websocket.send_text(
                                json.dumps({"type": "error", "message": "account not found"})
                            )
                            continue

                        account_id = target_account.id
                        manager.register(account_id, websocket)

                        # Send confirmation
                        await manager.send_to_account(
                            account_id,
                            {
                                "type": "account_switched",
                                "account": {
                                    "id": target_account.id,
                                    "user_id": target_account.user_id,
                                    "name": target_account.name,
                                },
                            },
                        )
                        await _send_snapshot_async(db, account_id)

                    elif kind == "get_snapshot":
                        if account_id is not None:
                            await _send_snapshot_async(db, account_id)

                    elif kind == "ping":
                        try:
                            await websocket.send_text(json.dumps({"type": "pong"}))
                        except Exception:
                            break

                    else:
                        try:
                            await websocket.send_text(
                                json.dumps({"type": "error", "message": "unknown message"})
                            )
                        except Exception:
                            break

                except Exception as e:
                    logger.error(
                        "WebSocket message handling error",
                        extra={
                            "context": {
                                "message_type": kind,
                                "account_id": account_id,
                                "error": str(e),
                            }
                        },
                    )
                    try:
                        await websocket.send_text(
                            json.dumps({"type": "error", "message": f"Internal error: {str(e)}"})
                        )
                    except Exception:
                        break
                finally:
                    # Session automatically commits/rolls back via get_db()
                    pass

    except WebSocketDisconnect:
        pass
    finally:
        # Clean up connections (T064)
        if account_id is not None:
            manager.unregister(account_id, websocket)
        if user_id is not None:
            manager.unregister(user_id, websocket)

        logger.info(
            "WebSocket disconnected",
            extra={
                "context": {
                    "account_id": account_id,
                    "remaining_connections": manager.get_connection_count(),
                }
            },
        )
