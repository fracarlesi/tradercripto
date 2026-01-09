"""
HLQuantBot Execution Engine Service
====================================

Service responsible for executing trading orders on Hyperliquid.

Features:
- Subscribes to SIZED_SIGNALS from message bus
- Executes orders via HyperliquidClient
- Smart order routing (market/limit selection)
- TP/SL management after fills
- Position lifecycle tracking
- Slippage protection
- Retry with exponential backoff

Flow:
    SIZED_SIGNAL -> Validate -> Place Order -> Monitor Fill -> Set TP/SL -> Track Position

Usage:
    from simple_bot.services import ExecutionEngineService
    from simple_bot.api import HyperliquidClient

    client = HyperliquidClient()
    await client.connect()

    engine = ExecutionEngineService(
        bus=message_bus,
        config=config,
        client=client
    )
    await engine.start()

Author: Francesco Carlesi
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Set

from .base import BaseService
from .message_bus import Message, MessageBus, Topic

# Type hints for optional imports
try:
    from simple_bot.api.hyperliquid import HyperliquidClient
    from simple_bot.config.loader import Config, ExecutionEngineConfig
except ImportError:
    HyperliquidClient = Any  # type: ignore
    Config = Any  # type: ignore
    ExecutionEngineConfig = Any  # type: ignore


logger = logging.getLogger(__name__)


# =============================================================================
# Data Classes
# =============================================================================

class OrderStatus(str, Enum):
    """Order lifecycle status."""
    
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    
    def __str__(self) -> str:
        return self.value


class PositionStatus(str, Enum):
    """Position lifecycle status."""
    
    OPENING = "opening"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    
    def __str__(self) -> str:
        return self.value


@dataclass
class Order:
    """
    Represents a trading order.
    
    Attributes:
        order_id: Unique order identifier (from exchange or internal)
        symbol: Trading symbol (e.g., "ETH")
        side: Order side ("buy" or "sell")
        size: Order size in base currency
        price: Limit price (None for market orders)
        order_type: Type of order ("limit", "market", "stop_market")
        status: Current order status
        reduce_only: Whether order only reduces position
        signal_id: Associated signal ID
        strategy: Strategy that generated the signal
        submitted_at: When order was submitted
        filled_at: When order was filled
        avg_price: Average fill price
        filled_size: Size filled so far
        fee: Trading fee paid
    """
    
    order_id: Optional[str] = None
    symbol: str = ""
    side: str = ""  # "buy" or "sell"
    size: float = 0.0
    price: Optional[float] = None
    order_type: str = "limit"
    status: OrderStatus = OrderStatus.PENDING
    reduce_only: bool = False
    signal_id: Optional[str] = None
    strategy: Optional[str] = None
    submitted_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None
    avg_price: Optional[float] = None
    filled_size: float = 0.0
    fee: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "size": self.size,
            "price": self.price,
            "order_type": self.order_type,
            "status": str(self.status),
            "reduce_only": self.reduce_only,
            "signal_id": self.signal_id,
            "strategy": self.strategy,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "filled_at": self.filled_at.isoformat() if self.filled_at else None,
            "avg_price": self.avg_price,
            "filled_size": self.filled_size,
            "fee": self.fee,
        }


@dataclass
class ExecutionPosition:
    """
    Represents an active trading position.
    
    Attributes:
        symbol: Trading symbol
        side: Position side ("long" or "short")
        size: Position size
        entry_price: Average entry price
        current_price: Current mark price
        unrealized_pnl: Unrealized profit/loss
        realized_pnl: Realized profit/loss
        leverage: Position leverage
        status: Position lifecycle status
        strategy: Strategy that opened the position
        signal_id: Original signal ID
        tp_order_id: Take profit order ID
        sl_order_id: Stop loss order ID
        opened_at: When position was opened
        closed_at: When position was closed
    """
    
    symbol: str = ""
    side: str = ""  # "long" or "short"
    size: float = 0.0
    entry_price: float = 0.0
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    leverage: int = 1
    status: PositionStatus = PositionStatus.OPENING
    strategy: Optional[str] = None
    signal_id: Optional[str] = None
    tp_order_id: Optional[str] = None
    sl_order_id: Optional[str] = None
    tp_price: Optional[float] = None
    sl_price: Optional[float] = None
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "symbol": self.symbol,
            "side": self.side,
            "size": self.size,
            "entry_price": self.entry_price,
            "current_price": self.current_price,
            "unrealized_pnl": self.unrealized_pnl,
            "realized_pnl": self.realized_pnl,
            "leverage": self.leverage,
            "status": str(self.status),
            "strategy": self.strategy,
            "signal_id": self.signal_id,
            "tp_order_id": self.tp_order_id,
            "sl_order_id": self.sl_order_id,
            "tp_price": self.tp_price,
            "sl_price": self.sl_price,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
        }


@dataclass
class ExecutionMetrics:
    """Metrics for execution engine performance."""
    
    orders_submitted: int = 0
    orders_filled: int = 0
    orders_rejected: int = 0
    orders_cancelled: int = 0
    total_slippage_pct: float = 0.0
    total_fees: float = 0.0
    avg_fill_time_ms: float = 0.0
    positions_opened: int = 0
    positions_closed: int = 0
    last_execution_time: Optional[datetime] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        avg_slippage = (
            self.total_slippage_pct / self.orders_filled 
            if self.orders_filled > 0 else 0.0
        )
        return {
            "orders_submitted": self.orders_submitted,
            "orders_filled": self.orders_filled,
            "orders_rejected": self.orders_rejected,
            "orders_cancelled": self.orders_cancelled,
            "fill_rate": (
                self.orders_filled / self.orders_submitted 
                if self.orders_submitted > 0 else 0.0
            ),
            "avg_slippage_pct": round(avg_slippage, 4),
            "total_fees": round(self.total_fees, 4),
            "avg_fill_time_ms": round(self.avg_fill_time_ms, 2),
            "positions_opened": self.positions_opened,
            "positions_closed": self.positions_closed,
            "last_execution_time": (
                self.last_execution_time.isoformat() 
                if self.last_execution_time else None
            ),
        }


# =============================================================================
# Execution Engine Service
# =============================================================================

class ExecutionEngineService(BaseService):
    """
    Service responsible for executing trading orders on Hyperliquid.
    
    Subscribes to SIZED_SIGNALS and executes orders with:
    - Smart order type selection (market/limit)
    - Slippage protection
    - TP/SL order placement after fills
    - Position lifecycle tracking
    - Retry logic for failed orders
    
    Example:
        engine = ExecutionEngineService(
            bus=message_bus,
            config=config,
            client=hyperliquid_client
        )
        await engine.start()
    """
    
    def __init__(
        self,
        bus: MessageBus,
        config: Config,
        client: HyperliquidClient,
        db: Optional[Any] = None,
    ) -> None:
        """
        Initialize execution engine service.
        
        Args:
            bus: MessageBus instance for pub/sub
            config: Bot configuration
            client: HyperliquidClient for API calls
            db: Optional database connection
        """
        super().__init__(
            name="execution_engine",
            bus=bus,
            db=db,
            loop_interval_seconds=5.0,  # Position monitoring interval
        )
        
        self._bot_config = config
        self.client = client
        
        # State tracking
        self.pending_orders: Dict[str, Order] = {}
        self.active_positions: Dict[str, ExecutionPosition] = {}
        self.processed_signals: Set[str] = set()
        
        # Metrics
        self.metrics = ExecutionMetrics()
        
        # Background tasks
        self._position_monitor_task: Optional[asyncio.Task] = None
        self._fill_sync_task: Optional[asyncio.Task] = None
        
        # Configuration shortcuts
        self._exec_config = self._bot_config.services.execution_engine
        
        self._logger.info(
            "ExecutionEngine initialized: order_type=%s, max_slippage=%.2f%%",
            self._exec_config.order_type,
            self._exec_config.max_slippage_pct,
        )
    
    # =========================================================================
    # Lifecycle
    # =========================================================================
    
    async def _on_start(self) -> None:
        """Initialize service and subscribe to topics."""
        self._logger.info("Starting ExecutionEngine service")
        
        # Subscribe to trade intents (from risk manager)
        await self.subscribe(Topic.TRADE_INTENT, self._handle_signal)
        
        # Start background tasks
        self._position_monitor_task = asyncio.create_task(
            self._monitor_positions(),
            name="execution_position_monitor"
        )
        self._fill_sync_task = asyncio.create_task(
            self._sync_fills(),
            name="execution_fill_sync"
        )
        
        # Sync existing positions from exchange
        await self._sync_positions_from_exchange()
        
        self._logger.info(
            "ExecutionEngine started: %d active positions",
            len(self.active_positions)
        )
    
    async def _on_stop(self) -> None:
        """Cleanup service resources."""
        self._logger.info("Stopping ExecutionEngine service")
        
        # Cancel background tasks
        for task in [self._position_monitor_task, self._fill_sync_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        self._logger.info(
            "ExecutionEngine stopped: %d orders pending, %d positions open",
            len(self.pending_orders),
            len(self.active_positions)
        )
    
    async def _run_iteration(self) -> None:
        """Periodic position sync (backup to continuous monitoring)."""
        await self._sync_positions_from_exchange()
    
    async def _health_check_impl(self) -> bool:
        """Check execution engine health."""
        try:
            # Verify client connection
            if not self.client.is_connected:
                self._logger.warning("HyperliquidClient not connected")
                return False
            
            # Check for stale pending orders (> 5 minutes old)
            now = datetime.now(timezone.utc)
            stale_orders = [
                order for order in self.pending_orders.values()
                if order.submitted_at and (now - order.submitted_at) > timedelta(minutes=5)
            ]
            if stale_orders:
                self._logger.warning(
                    "%d stale pending orders detected",
                    len(stale_orders)
                )
            
            return True
            
        except Exception as e:
            self._logger.error("Health check failed: %s", e)
            return False
    
    # =========================================================================
    # Signal Handling
    # =========================================================================

    def _normalize_trade_intent(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize TradeIntent fields to execution format.

        TradeIntent model uses: position_size, setup_type, direction (enum/str)
        ExecutionEngine expects: size, strategy, direction (string "long"/"short")

        Args:
            raw: Raw TradeIntent payload from message bus

        Returns:
            Normalized signal dictionary for execution
        """
        # Handle direction - can be Direction enum, string, or dict
        direction = raw.get("direction", "")
        if isinstance(direction, dict):
            # Pydantic serialization {"value": "long"}
            direction = direction.get("value", str(direction))
        elif hasattr(direction, "value"):
            # Direction enum
            direction = direction.value
        direction = str(direction).lower()

        # Handle setup_type/strategy - can be SetupType enum, string, or dict
        strategy = raw.get("setup_type") or raw.get("strategy", "unknown")
        if isinstance(strategy, dict):
            strategy = strategy.get("value", str(strategy))
        elif hasattr(strategy, "value"):
            strategy = strategy.value
        strategy = str(strategy)

        # Handle size - TradeIntent uses position_size, we expect size
        size = raw.get("position_size") or raw.get("size", 0)
        if isinstance(size, str):
            size = float(size)

        # Handle entry_price - can be Decimal serialized as string
        entry_price = raw.get("entry_price", 0)
        if isinstance(entry_price, str):
            entry_price = float(entry_price)

        return {
            **raw,  # Keep all original fields
            "direction": direction,
            "strategy": strategy,
            "size": float(size),
            "entry_price": float(entry_price),
        }

    async def _handle_signal(self, message: Message) -> None:
        """
        Handle incoming sized signal from capital allocator.

        Args:
            message: Message containing sized signal payload (TradeIntent)
        """
        raw_signal = message.payload
        signal_id: str = (
            raw_signal.get("id")
            or raw_signal.get("signal_id")
            or message.message_id
            or str(uuid.uuid4())
        )

        # Normalize TradeIntent fields to execution format
        # TradeIntent uses: position_size, setup_type, direction (enum)
        # ExecutionEngine expects: size, strategy, direction (string)
        signal = self._normalize_trade_intent(raw_signal)

        # Prevent duplicate processing
        if signal_id in self.processed_signals:
            self._logger.debug("Signal already processed: %s", signal_id[:8])
            return

        self._logger.info(
            "Received signal: %s %s %.4f %s @ %.4f",
            signal.get("direction", "unknown"),
            signal.get("symbol", "unknown"),
            float(signal.get("size", 0)),
            signal.get("strategy", "unknown"),
            float(signal.get("entry_price", 0)),
        )
        
        # Validate signal
        if not self._validate_signal(signal):
            self._logger.warning("Invalid signal rejected: %s", signal_id[:8])
            return
        
        # Mark as processed
        self.processed_signals.add(signal_id)
        
        # Execute order
        try:
            order_result = await self._execute_order(signal, signal_id)
            
            if order_result:
                # Publish order event
                await self.publish(Topic.ORDERS, {
                    **order_result.to_dict(),
                    "signal": signal,
                })
                
                # If filled, set TP/SL
                if order_result.status == OrderStatus.FILLED:
                    await self._handle_order_filled(signal, order_result)
                else:
                    # Track pending order
                    self.pending_orders[order_result.order_id] = order_result
                    
        except Exception as e:
            self._logger.error(
                "Order execution failed for signal %s: %s",
                signal_id[:8],
                e,
                exc_info=True
            )
            await self._handle_order_error(signal, e)
    
    def _validate_signal(self, signal: Dict[str, Any]) -> bool:
        """
        Validate signal before execution.
        
        Args:
            signal: Signal dictionary from capital allocator
            
        Returns:
            True if signal is valid for execution
        """
        required_fields = ["symbol", "direction", "size", "entry_price"]
        
        for field in required_fields:
            if field not in signal or signal[field] is None:
                self._logger.warning("Signal missing required field: %s", field)
                return False
        
        # Validate direction
        if signal["direction"] not in ("long", "short"):
            self._logger.warning("Invalid direction: %s", signal["direction"])
            return False
        
        # Validate size
        if signal["size"] <= 0:
            self._logger.warning("Invalid size: %s", signal["size"])
            return False
        
        # Validate entry price
        if signal["entry_price"] <= 0:
            self._logger.warning("Invalid entry price: %s", signal["entry_price"])
            return False
        
        # Check if we already have a position in this symbol
        symbol = signal["symbol"]
        if symbol in self.active_positions:
            existing = self.active_positions[symbol]
            if existing.status in (PositionStatus.OPEN, PositionStatus.OPENING):
                self._logger.info(
                    "Already have %s position in %s, skipping signal",
                    existing.side,
                    symbol
                )
                return False
        
        return True
    
    # =========================================================================
    # Order Execution
    # =========================================================================
    
    async def _execute_order(
        self, 
        signal: Dict[str, Any],
        signal_id: str
    ) -> Optional[Order]:
        """
        Execute order on Hyperliquid.
        
        Args:
            signal: Sized signal with execution parameters
            signal_id: Unique signal identifier
            
        Returns:
            Order object with execution result
        """
        symbol = signal["symbol"]
        direction = signal["direction"]
        size = signal["size"]
        entry_price = signal["entry_price"]
        strategy = signal.get("strategy", "unknown")
        
        # Determine buy/sell
        is_buy = direction == "long"
        side = "buy" if is_buy else "sell"
        
        # Determine order type
        order_type = await self._determine_order_type(signal)
        
        # Create order object
        order = Order(
            symbol=symbol,
            side=side,
            size=size,
            price=entry_price if order_type == "limit" else None,
            order_type=order_type,
            signal_id=signal_id,
            strategy=strategy,
            submitted_at=datetime.now(timezone.utc),
        )
        
        self._logger.info(
            "Placing %s %s order: %.4f %s @ %s",
            order_type.upper(),
            side.upper(),
            size,
            symbol,
            f"{entry_price:.2f}" if order_type == "limit" else "MARKET"
        )
        
        # Execute with retry
        for attempt in range(self._exec_config.retry_attempts):
            try:
                result = await self._place_order_on_exchange(order, signal)
                
                # Update order with result
                order.order_id = result.get("orderId")
                order.status = self._parse_order_status(result.get("status", "open"))
                order.avg_price = result.get("fillPrice")
                order.filled_size = result.get("filledSize", 0)
                
                if order.status == OrderStatus.FILLED:
                    order.filled_at = datetime.now(timezone.utc)
                    
                    # Check slippage
                    if order.avg_price:
                        self._record_slippage(entry_price, order.avg_price, is_buy)
                
                self.metrics.orders_submitted += 1
                if order.status == OrderStatus.FILLED:
                    self.metrics.orders_filled += 1
                
                self.metrics.last_execution_time = datetime.now(timezone.utc)
                
                return order
                
            except Exception as e:
                self._logger.warning(
                    "Order attempt %d/%d failed: %s",
                    attempt + 1,
                    self._exec_config.retry_attempts,
                    e
                )
                
                if attempt < self._exec_config.retry_attempts - 1:
                    await asyncio.sleep(self._exec_config.retry_delay_seconds)
                else:
                    order.status = OrderStatus.REJECTED
                    self.metrics.orders_rejected += 1
                    raise
        
        return order
    
    async def _place_order_on_exchange(
        self, 
        order: Order,
        signal: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Place order via HyperliquidClient.
        
        Args:
            order: Order to place
            signal: Original signal data
            
        Returns:
            Order result from exchange
        """
        is_buy = order.side == "buy"
        
        if order.order_type == "market":
            # Market order
            slippage = self._exec_config.max_slippage_pct / 100
            result = await self.client.place_order(
                symbol=order.symbol,
                is_buy=is_buy,
                size=order.size,
                price=None,
                order_type="market",
                reduce_only=order.reduce_only,
                slippage=slippage,
            )
        else:
            # Limit order with slight improvement for fill probability
            price = order.price
            if price:
                if is_buy:
                    price *= 1.001  # 0.1% above for buy
                else:
                    price *= 0.999  # 0.1% below for sell
            
            result = await self.client.place_order(
                symbol=order.symbol,
                is_buy=is_buy,
                size=order.size,
                price=price,
                order_type="limit",
                reduce_only=order.reduce_only,
                time_in_force="Gtc",
            )
        
        return result
    
    async def _determine_order_type(self, signal: Dict[str, Any]) -> str:
        """
        Decide between limit and market order.
        
        Args:
            signal: Signal with market context
            
        Returns:
            Order type: "limit" or "market"
        """
        config_type = self._exec_config.order_type
        
        if config_type == "market":
            return "market"
        elif config_type == "limit":
            return "limit"
        else:
            # Smart routing
            symbol = signal["symbol"]
            confidence = signal.get("confidence", 0.5)
            urgency = signal.get("urgency", "normal")
            
            # Use market if:
            # - High confidence (> 0.9)
            # - High urgency
            # - Wide spread (low liquidity)
            if confidence > 0.9:
                self._logger.debug("Using market order: high confidence (%.2f)", confidence)
                return "market"
            
            if urgency == "high":
                self._logger.debug("Using market order: high urgency")
                return "market"
            
            # Check spread
            try:
                spread = await self._get_spread(symbol)
                if spread > 0.3:  # 0.3% spread threshold
                    self._logger.debug("Using market order: wide spread (%.2f%%)", spread)
                    return "market"
            except Exception as e:
                self._logger.warning("Failed to check spread: %s", e)
            
            return "limit"
    
    async def _get_spread(self, symbol: str) -> float:
        """
        Get current bid-ask spread as percentage.
        
        Args:
            symbol: Trading symbol
            
        Returns:
            Spread as percentage of mid price
        """
        try:
            orderbook = await self.client.get_orderbook(symbol, depth=1)
            
            if orderbook["bids"] and orderbook["asks"]:
                best_bid = orderbook["bids"][0][0]
                best_ask = orderbook["asks"][0][0]
                mid_price = (best_bid + best_ask) / 2
                spread_pct = ((best_ask - best_bid) / mid_price) * 100
                return spread_pct
            
            return 0.0
            
        except Exception as e:
            self._logger.warning("Failed to get spread for %s: %s", symbol, e)
            return 0.0
    
    def _parse_order_status(self, status: str) -> OrderStatus:
        """Parse order status from exchange response."""
        status_map = {
            "open": OrderStatus.SUBMITTED,
            "filled": OrderStatus.FILLED,
            "partially_filled": OrderStatus.PARTIALLY_FILLED,
            "cancelled": OrderStatus.CANCELLED,
            "rejected": OrderStatus.REJECTED,
        }
        return status_map.get(status.lower(), OrderStatus.SUBMITTED)
    
    def _record_slippage(
        self, 
        expected_price: float, 
        actual_price: float,
        is_buy: bool
    ) -> None:
        """Record slippage for metrics."""
        if is_buy:
            slippage_pct = ((actual_price - expected_price) / expected_price) * 100
        else:
            slippage_pct = ((expected_price - actual_price) / expected_price) * 100
        
        self.metrics.total_slippage_pct += abs(slippage_pct)
        
        if abs(slippage_pct) > self._exec_config.max_slippage_pct:
            self._logger.warning(
                "High slippage detected: %.2f%% (max: %.2f%%)",
                slippage_pct,
                self._exec_config.max_slippage_pct
            )
    
    def _check_slippage(
        self, 
        expected_price: float, 
        actual_price: float
    ) -> bool:
        """
        Check if slippage is within acceptable limits.
        
        Args:
            expected_price: Expected execution price
            actual_price: Actual execution price
            
        Returns:
            True if slippage is acceptable
        """
        max_slippage = self._exec_config.max_slippage_pct / 100
        slippage = abs(actual_price - expected_price) / expected_price
        return slippage <= max_slippage
    
    # =========================================================================
    # TP/SL Management
    # =========================================================================
    
    async def _handle_order_filled(
        self, 
        signal: Dict[str, Any],
        order: Order
    ) -> None:
        """
        Handle a filled entry order.
        
        Creates position tracking and places TP/SL orders.
        
        Args:
            signal: Original signal
            order: Filled order
        """
        symbol = signal["symbol"]
        direction = signal["direction"]
        entry_price = order.avg_price or signal["entry_price"]
        size = order.filled_size or signal["size"]
        
        # Create position
        position = ExecutionPosition(
            symbol=symbol,
            side=direction,
            size=size,
            entry_price=entry_price,
            current_price=entry_price,
            strategy=signal.get("strategy"),
            signal_id=order.signal_id,
            status=PositionStatus.OPEN,
            opened_at=datetime.now(timezone.utc),
        )
        
        self.active_positions[symbol] = position
        self.metrics.positions_opened += 1
        
        self._logger.info(
            "Position opened: %s %s %.4f @ %.2f",
            direction.upper(),
            symbol,
            size,
            entry_price
        )
        
        # Set TP/SL orders
        await self._set_tp_sl(signal, order, position)
    
    async def _set_tp_sl(
        self, 
        signal: Dict[str, Any],
        order: Order,
        position: ExecutionPosition
    ) -> None:
        """
        Set take profit and stop loss orders.
        
        Args:
            signal: Original signal with TP/SL levels
            order: Filled entry order
            position: ExecutionPosition to protect
        """
        symbol = signal["symbol"]
        is_long = signal["direction"] == "long"
        entry_price = order.avg_price or signal["entry_price"]
        size = position.size
        
        # Get TP/SL percentages from signal or config
        tp_pct = signal.get("tp_pct", self._bot_config.risk.take_profit_pct / 100)
        sl_pct = signal.get("sl_pct", self._bot_config.risk.stop_loss_pct / 100)
        
        # Calculate TP/SL prices
        if is_long:
            tp_price = entry_price * (1 + tp_pct)
            sl_price = entry_price * (1 - sl_pct)
        else:
            tp_price = entry_price * (1 - tp_pct)
            sl_price = entry_price * (1 + sl_pct)
        
        self._logger.info(
            "Setting TP/SL for %s: TP=%.2f (%.1f%%), SL=%.2f (%.1f%%)",
            symbol,
            tp_price,
            tp_pct * 100,
            sl_price,
            sl_pct * 100
        )
        
        # Place TP order (limit, reduce-only)
        try:
            tp_result = await self.client.place_order(
                symbol=symbol,
                is_buy=not is_long,  # Opposite side to close
                size=size,
                price=tp_price,
                order_type="limit",
                reduce_only=True,
            )
            position.tp_order_id = tp_result.get("orderId")
            position.tp_price = tp_price
            self._logger.debug("TP order placed: %s", position.tp_order_id)
            
        except Exception as e:
            self._logger.error("Failed to place TP order: %s", e)
        
        # Place SL order (stop market, reduce-only)
        try:
            # For stop orders, we use market close with trigger
            # Since SDK might not support stop orders directly,
            # we'll use limit order at worse price as alternative
            sl_result = await self.client.place_order(
                symbol=symbol,
                is_buy=not is_long,
                size=size,
                price=sl_price,
                order_type="limit",  # Using limit as stop-loss fallback
                reduce_only=True,
            )
            position.sl_order_id = sl_result.get("orderId")
            position.sl_price = sl_price
            self._logger.debug("SL order placed: %s", position.sl_order_id)
            
        except Exception as e:
            self._logger.error("Failed to place SL order: %s", e)
    
    # =========================================================================
    # Position Monitoring
    # =========================================================================
    
    async def _monitor_positions(self) -> None:
        """Background task to monitor positions and fills."""
        self._logger.info("Position monitoring started")
        
        while True:
            try:
                # Sync positions from exchange
                await self._sync_positions_from_exchange()
                
                # Check for closed positions
                await self._check_closed_positions()
                
                await asyncio.sleep(5)  # Check every 5 seconds
                
            except asyncio.CancelledError:
                self._logger.debug("Position monitoring cancelled")
                break
            except Exception as e:
                self._logger.error("Position monitoring error: %s", e)
                await asyncio.sleep(10)
    
    async def _sync_positions_from_exchange(self) -> None:
        """Sync positions from exchange to local state."""
        try:
            exchange_positions = await self.client.get_positions()
            
            exchange_symbols = set()
            
            for pos in exchange_positions:
                symbol = pos["symbol"]
                size = pos.get("size", 0)
                
                if abs(size) > 0.0001:
                    exchange_symbols.add(symbol)
                    
                    if symbol in self.active_positions:
                        # Update existing position
                        local_pos = self.active_positions[symbol]
                        local_pos.current_price = pos.get("markPrice", local_pos.entry_price)
                        local_pos.unrealized_pnl = pos.get("unrealizedPnl", 0)
                        local_pos.size = abs(size)
                    else:
                        # New position (opened externally or before service start)
                        self.active_positions[symbol] = ExecutionPosition(
                            symbol=symbol,
                            side="long" if size > 0 else "short",
                            size=abs(size),
                            entry_price=pos.get("entryPrice", 0),
                            current_price=pos.get("markPrice", 0),
                            unrealized_pnl=pos.get("unrealizedPnl", 0),
                            leverage=pos.get("leverage", 1),
                            status=PositionStatus.OPEN,
                            opened_at=datetime.now(timezone.utc),
                        )
                        self._logger.info(
                            "Synced existing position: %s %s %.4f",
                            self.active_positions[symbol].side,
                            symbol,
                            abs(size)
                        )
            
            # Check for closed positions
            closed_symbols = set(self.active_positions.keys()) - exchange_symbols
            for symbol in closed_symbols:
                if self.active_positions[symbol].status == PositionStatus.OPEN:
                    await self._handle_position_closed(symbol)

            # Persist positions to database for dashboard visibility
            if self.db:
                positions_for_db = [
                    {
                        "symbol": pos.symbol,
                        "side": pos.side.upper(),  # DB expects LONG/SHORT uppercase
                        "size": pos.size,
                        "entry_price": pos.entry_price,
                        "mark_price": pos.current_price,
                        "unrealized_pnl": pos.unrealized_pnl,
                        "leverage": pos.leverage,
                        "liquidation_price": None,
                        "margin_used": 0.0,
                        "stop_loss_price": pos.sl_price,
                        "take_profit_price": pos.tp_price,
                        "strategy_id": pos.strategy,
                    }
                    for pos in self.active_positions.values()
                    if pos.status == PositionStatus.OPEN
                ]
                await self.db.upsert_positions(positions_for_db)

        except Exception as e:
            self._logger.error("Failed to sync positions: %s", e)
    
    async def _check_closed_positions(self) -> None:
        """Check for positions that have been closed."""
        for symbol, position in list(self.active_positions.items()):
            if position.status == PositionStatus.CLOSED:
                continue
            
            # Check if TP or SL was hit based on price
            if position.tp_price and position.sl_price:
                if position.side == "long":
                    if position.current_price >= position.tp_price:
                        self._logger.info(
                            "%s TP hit: %.2f >= %.2f",
                            symbol,
                            position.current_price,
                            position.tp_price
                        )
                    elif position.current_price <= position.sl_price:
                        self._logger.info(
                            "%s SL hit: %.2f <= %.2f",
                            symbol,
                            position.current_price,
                            position.sl_price
                        )
                else:
                    if position.current_price <= position.tp_price:
                        self._logger.info(
                            "%s TP hit: %.2f <= %.2f",
                            symbol,
                            position.current_price,
                            position.tp_price
                        )
                    elif position.current_price >= position.sl_price:
                        self._logger.info(
                            "%s SL hit: %.2f >= %.2f",
                            symbol,
                            position.current_price,
                            position.sl_price
                        )
    
    async def _handle_position_closed(self, symbol: str) -> None:
        """
        Handle a position that has been closed.
        
        Args:
            symbol: Symbol of closed position
        """
        if symbol not in self.active_positions:
            return
        
        position = self.active_positions[symbol]
        position.status = PositionStatus.CLOSED
        position.closed_at = datetime.now(timezone.utc)
        
        self.metrics.positions_closed += 1
        
        self._logger.info(
            "Position closed: %s %s - PnL: %.2f",
            position.side,
            symbol,
            position.unrealized_pnl
        )
        
        # Publish fill/close event
        await self.publish(Topic.FILLS, {
            "event": "position_closed",
            "symbol": symbol,
            "position": position.to_dict(),
            "pnl": position.unrealized_pnl,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        
        # Clean up - remove from active after a delay
        # to allow fill processing
        await asyncio.sleep(1)
        if symbol in self.active_positions:
            del self.active_positions[symbol]
    
    async def _sync_fills(self) -> None:
        """Background task to sync recent fills."""
        self._logger.info("Fill sync started")
        
        processed_fill_ids: Set[str] = set()
        
        while True:
            try:
                fills = await self.client.get_fills(limit=20)
                
                for fill in fills:
                    fill_id = fill.get("fillId")
                    if fill_id and fill_id not in processed_fill_ids:
                        processed_fill_ids.add(fill_id)
                        await self._process_fill(fill)
                
                # Prevent memory growth
                if len(processed_fill_ids) > 1000:
                    processed_fill_ids = set(list(processed_fill_ids)[-500:])
                
                await asyncio.sleep(10)  # Check every 10 seconds
                
            except asyncio.CancelledError:
                self._logger.debug("Fill sync cancelled")
                break
            except Exception as e:
                self._logger.error("Fill sync error: %s", e)
                await asyncio.sleep(30)
    
    async def _process_fill(self, fill: Dict[str, Any]) -> None:
        """
        Process a fill from the exchange.
        
        Args:
            fill: Fill data from exchange
        """
        symbol = fill.get("symbol")
        side = fill.get("side")
        size = fill.get("size", 0)
        price = fill.get("price", 0)
        fee = fill.get("fee", 0)
        
        self.metrics.total_fees += fee
        
        # Publish fill event
        await self.publish(Topic.FILLS, {
            "fill_id": fill.get("fillId"),
            "order_id": fill.get("orderId"),
            "symbol": symbol,
            "side": side,
            "size": size,
            "price": price,
            "fee": fee,
            "closed_pnl": fill.get("closedPnl", 0),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    
    # =========================================================================
    # Error Handling
    # =========================================================================
    
    async def _handle_order_error(
        self, 
        signal: Dict[str, Any],
        error: Exception
    ) -> None:
        """
        Handle order execution error.
        
        Args:
            signal: Signal that failed to execute
            error: Exception that occurred
        """
        self._logger.error(
            "Order error for %s %s: %s",
            signal.get("direction"),
            signal.get("symbol"),
            error
        )
        
        # Publish error event
        await self.publish(Topic.ORDERS, {
            "event": "order_error",
            "signal": signal,
            "error": str(error),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    
    # =========================================================================
    # Public API
    # =========================================================================
    
    def get_active_positions(self) -> List[Dict[str, Any]]:
        """Get all active positions as list of dicts."""
        return [pos.to_dict() for pos in self.active_positions.values()]
    
    def get_pending_orders(self) -> List[Dict[str, Any]]:
        """Get all pending orders as list of dicts."""
        return [order.to_dict() for order in self.pending_orders.values()]
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get execution metrics."""
        return self.metrics.to_dict()
    
    async def cancel_all_pending(self, symbol: Optional[str] = None) -> int:
        """
        Cancel all pending orders.
        
        Args:
            symbol: Optional symbol filter
            
        Returns:
            Number of orders cancelled
        """
        cancelled = await self.client.cancel_all_orders(symbol)
        
        # Clean up local state
        if symbol:
            self.pending_orders = {
                oid: order for oid, order in self.pending_orders.items()
                if order.symbol != symbol
            }
        else:
            self.pending_orders.clear()
        
        self.metrics.orders_cancelled += cancelled
        return cancelled
    
    async def close_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Close a specific position.
        
        Args:
            symbol: Symbol to close
            
        Returns:
            Order result or None if no position
        """
        if symbol not in self.active_positions:
            self._logger.warning("No position to close for %s", symbol)
            return None
        
        position = self.active_positions[symbol]
        
        # Cancel existing TP/SL orders
        for order_id in [position.tp_order_id, position.sl_order_id]:
            if order_id:
                try:
                    await self.client.cancel_order(symbol, int(order_id))
                except Exception as e:
                    self._logger.warning("Failed to cancel TP/SL order %s: %s", order_id, e)
        
        # Close position
        result = await self.client.close_position(symbol)
        
        position.status = PositionStatus.CLOSING
        
        return result


# =============================================================================
# Factory Function
# =============================================================================

def create_execution_engine(
    bus: MessageBus,
    config: Config,
    client: HyperliquidClient,
    db: Optional[Any] = None,
) -> ExecutionEngineService:
    """
    Create and configure an ExecutionEngineService.
    
    Args:
        bus: MessageBus instance
        config: Bot configuration
        client: Connected HyperliquidClient
        db: Optional database connection
        
    Returns:
        Configured ExecutionEngineService
    """
    return ExecutionEngineService(
        bus=bus,
        config=config,
        client=client,
        db=db,
    )
