"""Async WebSocket Handler (Migrated from ws.py) (T063-T064).

This module provides async WebSocket support with:
- AsyncSession for database operations
- Connection pooling
- Proper cleanup on disconnect
"""

import asyncio  # FIX: Add asyncio import
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

    Fetches real-time data from Hyperliquid to avoid stale database values.

    Args:
        db: Database session
        account_id: Account ID
    """
    try:
        await _send_snapshot_async_impl(db, account_id)
    except Exception as e:
        import traceback
        import sys
        print(f"\n\n{'='*80}", file=sys.stderr)
        print(f"FATAL ERROR in _send_snapshot_async:", file=sys.stderr)
        print(f"{'='*80}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        print(f"{'='*80}\n\n", file=sys.stderr)
        raise


async def _send_snapshot_async_impl(db: AsyncSession, account_id: int):
    """Implementation of snapshot sending."""
    logger.info(f"=== SENDING SNAPSHOT FOR ACCOUNT {account_id} ===")
    # Get account
    result = await db.execute(select(Account).where(Account.id == account_id))
    account = result.scalar_one_or_none()

    if not account:
        logger.warning(
            "Account not found for snapshot",
            extra={"context": {"account_id": account_id}},
        )
        return

    # Fetch real-time data from Hyperliquid (NO REDUNDANCY!)
    from services.trading.hyperliquid_trading_service import hyperliquid_trading_service

    try:
        user_state = await hyperliquid_trading_service.get_user_state_async()

        # Validate response structure
        if not user_state or 'marginSummary' not in user_state:
            raise ValueError(f"Invalid Hyperliquid response: {user_state}")

        margin = user_state['marginSummary']
        hl_positions = user_state.get('assetPositions', [])

        # accountValue and totalMarginUsed are always strings from Hyperliquid API
        account_value = float(margin['accountValue'])
        total_margin_used = float(margin['totalMarginUsed'])

        # Calculate position value from Hyperliquid using CURRENT market prices (not entry prices!)
        positions_value = 0
        for p in hl_positions:
            pos = p.get('position', {})
            # Use positionValue from Hyperliquid which already includes current market price
            position_value = float(pos.get('positionValue', '0'))
            positions_value += abs(position_value)  # Use abs() because shorts have negative positionValue

        cash_available = account_value - positions_value

    except Exception as e:
        logger.error(f"Failed to fetch real-time data from Hyperliquid: {e}")
        # Fallback to zeros if API fails
        account_value = 0
        cash_available = 0
        total_margin_used = 0
        positions_value = 0

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

    # Build overview with REAL-TIME Hyperliquid data
    # Balance data fetched from Hyperliquid API, not stored in DB
    overview = {
        "account": {
            "id": account.id,
            "user_id": account.user_id,
            "name": account.name,
            "account_type": account.account_type,
        },
        "total_assets": account_value,  # Real-time from Hyperliquid
        "cash_available": cash_available,  # Real-time from Hyperliquid
        "margin_used": total_margin_used,   # Real-time from Hyperliquid
        "positions_value": positions_value,  # Real-time from Hyperliquid
    }

    # Fetch current market prices
    try:
        all_mids = await hyperliquid_trading_service.get_all_mids_async()
    except Exception as e:
        logger.error(f"Failed to fetch market prices from Hyperliquid: {e}")
        all_mids = {}

    # Build positions list DIRECTLY from Hyperliquid (NO DATABASE!)
    # Single source of truth: only positions that exist on Hyperliquid are shown
    enriched_positions = []
    for asset_pos in hl_positions:
        pos = asset_pos.get('position', {})
        coin = pos.get('coin', '')
        if not coin:
            continue

        size = float(pos.get('szi', '0'))
        entry_px = float(pos.get('entryPx', '0'))
        position_value = float(pos.get('positionValue', '0'))
        unrealized_pnl = float(pos.get('unrealizedPnl', '0'))
        return_on_equity = float(pos.get('returnOnEquity', '0'))
        margin_used = float(pos.get('marginUsed', '0'))

        # Get current market price
        current_price = all_mids.get(coin)
        if current_price is None:
            logger.warning(f"No current price available for {coin}, using entry price")
            current_price = entry_px
        else:
            current_price = float(current_price)

        enriched_positions.append({
            "id": 0,  # No database ID - this is real-time from Hyperliquid
            "account_id": account_id,
            "symbol": coin,
            "name": coin,
            "market": "CRYPTO",
            "quantity": abs(size),  # Absolute value, side determined by sign
            "available_quantity": abs(size),
            "avg_cost": entry_px,
            "last_price": current_price,
            "market_value": abs(position_value),
            "unrealized_pnl": unrealized_pnl,
            "return_on_equity": return_on_equity,
            "margin_used": margin_used,
        })

    # Get asset curve data for the chart (default timeframe: 1h)
    # FIX: Call async function directly to avoid event loop deadlock
    import asyncio
    from services.asset_curve_calculator import get_all_asset_curves_data_new_async
    from database.connection import SessionLocal

    def get_curves_sync():
        """Sync wrapper that creates its own session for the curve calculation"""
        try:
            # Create a new sync session for this operation
            sync_db = SessionLocal()
            try:
                # Run the async function in a new event loop (safe because we're in a thread)
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    curves = loop.run_until_complete(
                        get_all_asset_curves_data_new_async(sync_db, "1h")
                    )
                    logger.info(f"Asset curves calculated: {len(curves)} points")
                    return curves
                finally:
                    loop.close()
            finally:
                sync_db.close()
        except Exception as e:
            logger.error(f"Failed to calculate asset curves: {e}", exc_info=True)
            return []

    # Execute in a thread pool to avoid blocking the async event loop
    all_asset_curves = await asyncio.to_thread(get_curves_sync)

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
        "all_asset_curves": all_asset_curves,
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
                            import traceback
                            import sys
                            traceback.print_exc(file=sys.stderr)
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

                    elif kind == "get_asset_curve":
                        # Get asset curve data with specific timeframe
                        timeframe = msg.get("timeframe", "1h")
                        if timeframe not in ["5m", "1h", "1d"]:
                            await websocket.send_text(
                                json.dumps(
                                    {
                                        "type": "error",
                                        "message": "Invalid timeframe. Must be 5m, 1h, or 1d",
                                    }
                                )
                            )
                            continue

                        # FIX: Use asyncio.to_thread() to avoid deadlock
                        from services.asset_curve_calculator import get_all_asset_curves_data_new_async
                        from database.connection import SessionLocal

                        def get_curves_sync():
                            sync_db = SessionLocal()
                            try:
                                loop = asyncio.new_event_loop()
                                asyncio.set_event_loop(loop)
                                try:
                                    curves = loop.run_until_complete(
                                        get_all_asset_curves_data_new_async(sync_db, timeframe)
                                    )
                                    return curves
                                finally:
                                    loop.close()
                            finally:
                                sync_db.close()

                        asset_curves = await asyncio.to_thread(get_curves_sync)

                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "asset_curve_data",
                                    "timeframe": timeframe,
                                    "data": asset_curves,
                                }
                            )
                        )

                    elif kind == "switch_user":
                        # Switch to different user account
                        target_username = msg.get("username")
                        if not target_username:
                            await websocket.send_text(
                                json.dumps({"type": "error", "message": "username required"})
                            )
                            continue

                        # Unregister from current account if any
                        if account_id is not None:
                            manager.unregister(account_id, websocket)

                        # Find or create target user
                        target_user = await UserRepository.get_or_create_user(db, target_username)
                        user_id = target_user.id

                        # Get or create default account for this user
                        target_account = await AccountRepository.get_or_create_default_account(
                            db, user_id
                        )
                        account_id = target_account.id

                        # Register to new account
                        manager.register(account_id, websocket)

                        # Send confirmation
                        await manager.send_to_account(
                            account_id,
                            {
                                "type": "user_switched",
                                "user": {"id": target_user.id, "username": target_user.username},
                            },
                        )
                        await _send_snapshot_async(db, account_id)

                    elif kind == "place_order":
                        # Place trading order via Hyperliquid
                        if account_id is None:
                            await websocket.send_text(
                                json.dumps({"type": "error", "message": "not authenticated"})
                            )
                            continue

                        try:
                            # Get account
                            result = await db.execute(select(Account).where(Account.id == account_id))
                            account = result.scalar_one_or_none()

                            if not account:
                                await websocket.send_text(
                                    json.dumps({"type": "error", "message": "account not found"})
                                )
                                continue

                            # Extract order parameters
                            symbol = msg.get("symbol")
                            name = msg.get("name", symbol)
                            market = msg.get("market", "CRYPTO")
                            side = msg.get("side")
                            order_type = msg.get("order_type")
                            price = msg.get("price")
                            quantity = msg.get("quantity")

                            # Validate required parameters
                            if not all([symbol, side, order_type, quantity]):
                                await websocket.send_text(
                                    json.dumps(
                                        {"type": "error", "message": "missing required parameters"}
                                    )
                                )
                                continue

                            # Convert quantity to float
                            try:
                                quantity = float(quantity)
                            except (ValueError, TypeError):
                                await websocket.send_text(
                                    json.dumps({"type": "error", "message": "invalid quantity"})
                                )
                                continue

                            # Import order creation service
                            from services.order_matching import create_order

                            # Create the order
                            order = create_order(
                                db=db,
                                account=account,
                                symbol=symbol,
                                name=name,
                                side=side,
                                order_type=order_type,
                                price=price,
                                quantity=quantity,
                            )

                            # Commit the order
                            await db.commit()

                            # Send success response
                            await manager.send_to_account(
                                account_id, {"type": "order_pending", "order_id": order.id}
                            )

                            # Send updated snapshot
                            await _send_snapshot_async(db, account_id)

                        except ValueError as e:
                            # Business logic errors (insufficient funds, etc.)
                            try:
                                await websocket.send_text(
                                    json.dumps({"type": "error", "message": str(e)})
                                )
                            except Exception:
                                break
                        except Exception as e:
                            # Unexpected errors
                            logger.error(
                                "Order placement error",
                                extra={"context": {"account_id": account_id, "error": str(e)}},
                            )
                            try:
                                await websocket.send_text(
                                    json.dumps(
                                        {
                                            "type": "error",
                                            "message": f"order placement failed: {str(e)}",
                                        }
                                    )
                                )
                            except Exception:
                                break

                    else:
                        logger.warning(
                            f"Unknown WebSocket message type received: {kind}",
                            extra={"context": {"message_type": kind, "full_message": msg}},
                        )
                        try:
                            await websocket.send_text(
                                json.dumps({"type": "error", "message": f"unknown message type: {kind}"})
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
                        exc_info=True,  # FIX: Add traceback to logs
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
