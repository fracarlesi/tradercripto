"""Order lifecycle management."""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass, field

from ..core.models import ApprovedOrder, Position, ClosedTrade
from ..core.enums import OrderStatus, Side, ExitReason, StrategyId


logger = logging.getLogger(__name__)


@dataclass
class ManagedOrder:
    """Order with management metadata."""
    order: ApprovedOrder
    hl_order_id: Optional[str] = None  # Hyperliquid order ID
    hl_response: Optional[dict] = None
    fill_updates: List[dict] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_update: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ManagedPosition:
    """Position with management metadata."""
    position: Position
    stop_order_id: Optional[str] = None
    tp_order_id: Optional[str] = None
    trailing_stop_active: bool = False
    trailing_stop_price: Optional[Decimal] = None
    peak_price: Optional[Decimal] = None


class OrderManager:
    """
    Manages order lifecycle and position tracking.

    Responsibilities:
    - Track order status
    - Manage SL/TP orders
    - Track trailing stops
    - Handle partial fills
    - Maintain position state
    """

    def __init__(self):
        # Active orders by our order_id
        self._orders: Dict[str, ManagedOrder] = {}

        # Map from HL order ID to our order_id
        self._hl_order_map: Dict[str, str] = {}

        # Managed positions by symbol
        self._positions: Dict[str, ManagedPosition] = {}

        # Completed trades
        self._closed_trades: List[ClosedTrade] = []

        # Callbacks
        self._fill_callbacks: List[Callable] = []
        self._close_callbacks: List[Callable] = []

        # Lock
        self._lock = asyncio.Lock()

    def on_fill(self, callback: Callable):
        """Register callback for order fills."""
        self._fill_callbacks.append(callback)

    def on_position_close(self, callback: Callable):
        """Register callback for position closes."""
        self._close_callbacks.append(callback)

    # -------------------------------------------------------------------------
    # Order Tracking
    # -------------------------------------------------------------------------
    async def add_order(
        self,
        order: ApprovedOrder,
        hl_order_id: Optional[str] = None,
        hl_response: Optional[dict] = None,
    ):
        """Add an order to tracking."""
        async with self._lock:
            managed = ManagedOrder(
                order=order,
                hl_order_id=hl_order_id,
                hl_response=hl_response,
            )
            self._orders[order.order_id] = managed

            if hl_order_id:
                self._hl_order_map[hl_order_id] = order.order_id

            logger.debug(f"Tracking order {order.order_id} (HL: {hl_order_id})")

    async def update_order_status(
        self,
        order_id: str,
        status: OrderStatus,
        filled_size: Optional[Decimal] = None,
        filled_price: Optional[Decimal] = None,
        fees: Optional[Decimal] = None,
    ):
        """Update order status."""
        async with self._lock:
            if order_id not in self._orders:
                # Try HL order ID
                order_id = self._hl_order_map.get(order_id, order_id)

            if order_id not in self._orders:
                logger.warning(f"Unknown order: {order_id}")
                return

            managed = self._orders[order_id]
            managed.order.status = status
            managed.last_update = datetime.now(timezone.utc)

            if filled_size is not None:
                managed.order.filled_size = filled_size
            if filled_price is not None:
                managed.order.filled_price = filled_price
            if fees is not None:
                managed.order.fees = fees

            if status == OrderStatus.FILLED:
                managed.order.executed_at = datetime.now(timezone.utc)
                await self._handle_fill(managed)

    async def _handle_fill(self, managed: ManagedOrder):
        """Handle order fill - update positions."""
        order = managed.order

        # Notify callbacks
        for callback in self._fill_callbacks:
            try:
                await callback(order)
            except Exception as e:
                logger.error(f"Fill callback error: {e}")

        # Update or create position
        if order.symbol in self._positions:
            await self._update_position_from_fill(order)
        else:
            await self._create_position_from_fill(order)

    async def _create_position_from_fill(self, order: ApprovedOrder):
        """Create new position from filled order."""
        position = Position(
            symbol=order.symbol,
            side=order.side,
            size=order.filled_size,
            entry_price=order.filled_price or Decimal(0),
            current_price=order.filled_price or Decimal(0),
            leverage=order.leverage_used,
            stop_loss_price=order.stop_loss_price,
            take_profit_price=order.take_profit_price,
            strategy_id=order.strategy_id,
            opened_at=order.executed_at or datetime.now(timezone.utc),
        )

        managed_pos = ManagedPosition(
            position=position,
            peak_price=order.filled_price,
        )
        self._positions[order.symbol] = managed_pos

        logger.info(
            f"New position: {position.side.value} {position.size} {position.symbol} "
            f"@ {position.entry_price}"
        )

    async def _update_position_from_fill(self, order: ApprovedOrder):
        """Update existing position from fill."""
        managed_pos = self._positions[order.symbol]
        position = managed_pos.position

        # Same direction = add to position
        if position.side == order.side:
            # Calculate new average entry
            old_notional = position.size * position.entry_price
            new_notional = order.filled_size * (order.filled_price or Decimal(0))
            total_size = position.size + order.filled_size

            if total_size > 0:
                position.entry_price = (old_notional + new_notional) / total_size
            position.size = total_size

            logger.info(f"Added to position: {position.symbol} now {position.size}")

        # Opposite direction = reduce or close position
        else:
            if order.filled_size >= position.size:
                # Full close
                await self._close_position(
                    managed_pos,
                    order.filled_price or position.current_price,
                    ExitReason.SIGNAL_EXIT,
                    order.fees,
                )
            else:
                # Partial close
                position.size -= order.filled_size
                logger.info(f"Reduced position: {position.symbol} now {position.size}")

    async def _close_position(
        self,
        managed_pos: ManagedPosition,
        exit_price: Decimal,
        exit_reason: ExitReason,
        fees: Decimal = Decimal(0),
    ):
        """Close a position and record the trade."""
        position = managed_pos.position

        # Calculate P&L
        if position.side == Side.LONG:
            pnl = (exit_price - position.entry_price) * position.size
        else:
            pnl = (position.entry_price - exit_price) * position.size

        entry_notional = position.entry_price * position.size
        pnl_pct = pnl / entry_notional if entry_notional > 0 else Decimal(0)

        # Create closed trade record
        trade = ClosedTrade(
            trade_id=f"{position.symbol}_{position.opened_at.timestamp():.0f}",
            symbol=position.symbol,
            side=position.side,
            size=position.size,
            entry_price=position.entry_price,
            exit_price=exit_price,
            pnl=pnl,
            pnl_pct=pnl_pct,
            fees=fees,
            entry_time=position.opened_at,
            exit_time=datetime.now(timezone.utc),
            strategy_id=position.strategy_id or StrategyId.FUNDING_BIAS,
            exit_reason=exit_reason,
        )
        trade.duration_seconds = int((trade.exit_time - trade.entry_time).total_seconds())

        self._closed_trades.append(trade)

        # Remove position
        del self._positions[position.symbol]

        logger.info(
            f"Position closed: {position.symbol} P&L: ${pnl:.2f} ({pnl_pct:.2%}) "
            f"Reason: {exit_reason.value}"
        )

        # Notify callbacks
        for callback in self._close_callbacks:
            try:
                await callback(trade)
            except Exception as e:
                logger.error(f"Close callback error: {e}")

    # -------------------------------------------------------------------------
    # Position Management
    # -------------------------------------------------------------------------
    def get_position(self, symbol: str) -> Optional[Position]:
        """Get position for a symbol."""
        managed = self._positions.get(symbol)
        return managed.position if managed else None

    def get_all_positions(self) -> List[Position]:
        """Get all positions."""
        return [m.position for m in self._positions.values()]

    def get_managed_position(self, symbol: str) -> Optional[ManagedPosition]:
        """Get managed position with metadata."""
        return self._positions.get(symbol)

    async def update_position_price(self, symbol: str, current_price: Decimal):
        """Update position with current price."""
        async with self._lock:
            if symbol not in self._positions:
                return

            managed = self._positions[symbol]
            position = managed.position
            position.update_pnl(current_price)

            # Update peak price for trailing stop
            if position.side == Side.LONG:
                if managed.peak_price is None or current_price > managed.peak_price:
                    managed.peak_price = current_price
            else:
                if managed.peak_price is None or current_price < managed.peak_price:
                    managed.peak_price = current_price

    async def set_stop_order_id(self, symbol: str, order_id: str):
        """Set the stop loss order ID for a position."""
        if symbol in self._positions:
            self._positions[symbol].stop_order_id = order_id

    async def set_tp_order_id(self, symbol: str, order_id: str):
        """Set the take profit order ID for a position."""
        if symbol in self._positions:
            self._positions[symbol].tp_order_id = order_id

    # -------------------------------------------------------------------------
    # Trailing Stop
    # -------------------------------------------------------------------------
    async def activate_trailing_stop(
        self,
        symbol: str,
        trail_pct: Decimal,
    ) -> Optional[Decimal]:
        """
        Activate trailing stop for a position.

        Returns the initial trailing stop price.
        """
        async with self._lock:
            if symbol not in self._positions:
                return None

            managed = self._positions[symbol]
            position = managed.position

            if managed.peak_price is None:
                managed.peak_price = position.current_price

            # Calculate trailing stop price
            if position.side == Side.LONG:
                trailing_price = managed.peak_price * (1 - trail_pct)
            else:
                trailing_price = managed.peak_price * (1 + trail_pct)

            managed.trailing_stop_active = True
            managed.trailing_stop_price = trailing_price

            logger.info(
                f"Trailing stop activated for {symbol}: {trailing_price:.2f} "
                f"(trail: {trail_pct:.2%})"
            )

            return trailing_price

    async def update_trailing_stop(
        self,
        symbol: str,
        current_price: Decimal,
        trail_pct: Decimal,
    ) -> Optional[Decimal]:
        """
        Update trailing stop based on price movement.

        Returns new trailing stop price if updated, None otherwise.
        """
        async with self._lock:
            if symbol not in self._positions:
                return None

            managed = self._positions[symbol]
            if not managed.trailing_stop_active:
                return None

            position = managed.position
            old_trailing = managed.trailing_stop_price

            # Update peak price
            if position.side == Side.LONG:
                if current_price > managed.peak_price:
                    managed.peak_price = current_price
                    new_trailing = managed.peak_price * (1 - trail_pct)
                    if new_trailing > old_trailing:
                        managed.trailing_stop_price = new_trailing
                        return new_trailing
            else:
                if current_price < managed.peak_price:
                    managed.peak_price = current_price
                    new_trailing = managed.peak_price * (1 + trail_pct)
                    if new_trailing < old_trailing:
                        managed.trailing_stop_price = new_trailing
                        return new_trailing

            return None

    def check_trailing_stop_hit(self, symbol: str, current_price: Decimal) -> bool:
        """Check if trailing stop has been hit."""
        if symbol not in self._positions:
            return False

        managed = self._positions[symbol]
        if not managed.trailing_stop_active or not managed.trailing_stop_price:
            return False

        position = managed.position
        if position.side == Side.LONG:
            return current_price <= managed.trailing_stop_price
        else:
            return current_price >= managed.trailing_stop_price

    # -------------------------------------------------------------------------
    # Order Queries
    # -------------------------------------------------------------------------
    def get_order(self, order_id: str) -> Optional[ApprovedOrder]:
        """Get order by ID."""
        managed = self._orders.get(order_id)
        return managed.order if managed else None

    def get_pending_orders(self) -> List[ApprovedOrder]:
        """Get all pending orders."""
        return [
            m.order for m in self._orders.values()
            if m.order.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED)
        ]

    def get_orders_for_symbol(self, symbol: str) -> List[ApprovedOrder]:
        """Get all orders for a symbol."""
        return [m.order for m in self._orders.values() if m.order.symbol == symbol]

    # -------------------------------------------------------------------------
    # Trade History
    # -------------------------------------------------------------------------
    def get_closed_trades(self, limit: int = 100) -> List[ClosedTrade]:
        """Get recent closed trades."""
        return self._closed_trades[-limit:]

    def get_trades_for_strategy(
        self,
        strategy_id: StrategyId,
        limit: int = 100,
    ) -> List[ClosedTrade]:
        """Get trades for a specific strategy."""
        trades = [t for t in self._closed_trades if t.strategy_id == strategy_id]
        return trades[-limit:]

    def get_daily_pnl(self) -> Decimal:
        """Get P&L for today's closed trades."""
        today = datetime.now(timezone.utc).date()
        daily_trades = [
            t for t in self._closed_trades
            if t.exit_time.date() == today
        ]
        return sum((t.net_pnl for t in daily_trades), Decimal(0))

    # -------------------------------------------------------------------------
    # Cleanup
    # -------------------------------------------------------------------------
    async def cleanup_old_orders(self, max_age_hours: int = 24):
        """Remove old completed orders from tracking."""
        async with self._lock:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
            to_remove = []

            for order_id, managed in self._orders.items():
                if managed.order.status in (
                    OrderStatus.FILLED,
                    OrderStatus.CANCELLED,
                    OrderStatus.REJECTED,
                    OrderStatus.EXPIRED,
                ):
                    if managed.last_update < cutoff:
                        to_remove.append(order_id)

            for order_id in to_remove:
                managed = self._orders.pop(order_id)
                if managed.hl_order_id:
                    self._hl_order_map.pop(managed.hl_order_id, None)

            if to_remove:
                logger.debug(f"Cleaned up {len(to_remove)} old orders")
