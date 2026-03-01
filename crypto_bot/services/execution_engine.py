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
    from crypto_bot.services import ExecutionEngineService
    from crypto_bot.api import HyperliquidClient

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
    from crypto_bot.api.hyperliquid import HyperliquidClient
    from crypto_bot.config.loader import Config, ExecutionEngineConfig
except ImportError:
    HyperliquidClient = Any  # type: ignore
    Config = Any  # type: ignore
    ExecutionEngineConfig = Any  # type: ignore


logger = logging.getLogger(__name__)

# Minimum acceptable distance between entry and TP/SL after rounding.
# If rounding collapses the distance below this (e.g. low-price assets),
# the trade is rejected to prevent instant SL triggers.
MIN_STOP_DISTANCE_PCT = 0.001  # 0.1%

# When a position reaches this unrealized P&L %, the stop-loss is moved
# to the entry price (breakeven) so the trade becomes risk-free.
BREAKEVEN_THRESHOLD_PCT = 1.2  # 1.2% — was 0.6% which cut winners too early (avg win $0.36 vs avg loss $0.79)

# Offset above/below entry for breakeven SL to cover exchange fees.
# For LONG: SL = entry * (1 + offset/100); for SHORT: SL = entry * (1 - offset/100).
BREAKEVEN_OFFSET_PCT = 0.15  # 0.15% offset covers 2x round-trip fees + spread


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
    original_signal: Optional[Dict[str, Any]] = None  # Stored for deferred fill handling

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
    exit_reason: Optional[str] = None  # "stop_loss", "take_profit", "roi_target", "regime_change", "manual"
    entry_regime: Optional[str] = None  # Regime at entry ("trend", "range", "chaos")
    breakeven_activated: bool = False  # True once SL has been moved to entry price
    highest_price: float = 0.0       # Peak price for LONG trailing stop
    lowest_price: float = float('inf')  # Trough price for SHORT trailing stop
    entry_atr_pct: float = 0.0       # ATR% at entry time (for trailing distance)
    trailing_active: bool = False     # True when trailing stop is actively following price
    entry_rsi_slope: float = 0.0     # RSI slope at entry (for momentum fade tracking)
    entry_ema_spread: float = 0.0    # EMA spread at entry (for momentum fade tracking)

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
            "exit_reason": self.exit_reason,
            "entry_regime": self.entry_regime,
            "breakeven_activated": self.breakeven_activated,
            "highest_price": self.highest_price,
            "lowest_price": self.lowest_price,
            "entry_atr_pct": self.entry_atr_pct,
            "trailing_active": self.trailing_active,
            "entry_rsi_slope": self.entry_rsi_slope,
            "entry_ema_spread": self.entry_ema_spread,
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
    ) -> None:
        """
        Initialize execution engine service.

        Args:
            bus: MessageBus instance for pub/sub
            config: Bot configuration
            client: HyperliquidClient for API calls
        """
        super().__init__(
            name="execution_engine",
            bus=bus,
            loop_interval_seconds=5.0,  # Position monitoring interval
        )
        
        self._bot_config = config
        self.client = client
        
        # State tracking
        self.pending_orders: Dict[str, Order] = {}
        self.active_positions: Dict[str, ExecutionPosition] = {}
        self.processed_signals: Set[str] = set()
        self._tp_sl_confirmed: Set[str] = set()  # Positions with TP/SL confirmed (retry on each sync until success)
        self._closing_positions: Set[str] = set()  # Positions with close order already sent (prevent SL/TP spam)
        self._settling_symbols: Set[str] = set()  # Symbols mid-open/close/rejection — sync loop must skip

        # Partial fill tracking: order_id -> (first_seen_time, last_seen_size)
        # Used to give partial fills a grace period before processing as complete.
        self._partial_fill_first_seen: Dict[str, tuple[datetime, float]] = {}

        # Size at which TP/SL were last placed for each symbol.
        # Used by _sync_positions_from_exchange to detect when the position
        # grows (additional fills) and TP/SL need to be re-placed.
        self._tp_sl_placed_size: Dict[str, float] = {}

        # Market states cache for momentum fade exit
        self._market_states: Dict[str, Any] = {}

        # Metrics
        self.metrics = ExecutionMetrics()
        
        # Background tasks
        self._position_monitor_task: Optional[asyncio.Task] = None
        
        # Configuration shortcuts
        self._exec_config = self._bot_config.services.execution_engine
        
        self._logger.info(
            "ExecutionEngine initialized: order_type=%s, max_slippage=%.2f%%",
            self._exec_config.order_type,
            self._exec_config.max_slippage_pct,
        )
    
    def update_market_states(self, states: Dict[str, Any]) -> None:
        """Update cached market states (called from bot orchestrator each scan)."""
        self._market_states = dict(states) if states else {}

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def _on_start(self) -> None:
        """Initialize service and subscribe to topics."""
        self._logger.info("Starting ExecutionEngine service")
        
        # Subscribe to trade intents (from risk manager)
        await self.subscribe(Topic.TRADE_INTENT, self._handle_signal)

        # Subscribe to regime changes (close positions on regime invalidation)
        await self.subscribe(Topic.REGIME, self._handle_regime_change)

        # Start background tasks
        self._position_monitor_task = asyncio.create_task(
            self._monitor_positions(),
            name="execution_position_monitor"
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
        for task in [self._position_monitor_task]:
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
        """No-op: position sync is handled exclusively by _monitor_positions().

        We deliberately avoid calling _sync_positions_from_exchange() here
        because the background _monitor_positions task already syncs every
        ~5 seconds.  Having two concurrent callers doubles exchange API
        traffic and widens the race-condition window for position state
        changes (e.g. a symbol appearing "closed" in one caller while
        still settling in the other).
        """
        pass
    
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
        
        # Execute order — mark symbol as settling so sync loop skips it
        symbol = signal["symbol"]
        self._settling_symbols.add(symbol)
        self._logger.debug("Settling started for %s", symbol)
        try:
            order_result = await self._execute_order(signal, signal_id)

            if order_result:
                # Enrich signal with TP/SL prices for notifications
                enriched_signal = dict(signal)
                entry_price = order_result.avg_price or signal["entry_price"]
                is_long = signal["direction"] == "long"

                # Recalculate TP/SL from fill price — always use fixed config values.
                # ATR-adaptive branch was removed: the ATR formula produced TP=4-8%
                # and SL=0.5-2%, far too wide for a 15m momentum scalper.
                # Config values (TP=1.6%, SL=1.0%) are data-driven from parameter sweep.
                sl_pct = self._bot_config.risk.stop_loss_pct / 100
                tp_pct = self._bot_config.risk.take_profit_pct / 100

                sl_price = entry_price * (1 - sl_pct) if is_long else entry_price * (1 + sl_pct)
                tp_price = entry_price * (1 + tp_pct) if is_long else entry_price * (1 - tp_pct)

                enriched_signal["tp_price"] = round(tp_price, 6)
                enriched_signal["sl_price"] = round(sl_price, 6)

                # Publish order event for notifications
                await self.publish(Topic.ORDERS, {
                    "event": "order_submitted",
                    "signal": enriched_signal,
                    "order": order_result.to_dict(),
                })

                # If filled, set TP/SL
                if order_result.status == OrderStatus.FILLED:
                    await self._handle_order_filled(signal, order_result)
                else:
                    # Track pending order with original signal for deferred fill handling
                    order_result.original_signal = signal
                    self.pending_orders[order_result.order_id] = order_result

        except Exception as e:
            self._logger.error(
                "Order execution failed for signal %s: %s",
                signal_id[:8],
                e,
                exc_info=True
            )
            await self._handle_order_error(signal, e)
        finally:
            self._settling_symbols.discard(symbol)
            self._logger.debug("Settling ended for %s", symbol)
    
    # Minimum age (minutes) before a position can be closed by regime change.
    # Prevents immediate regime-exit after entry when the confirmation counter
    # is in a transient state (e.g. after service restart).
    REGIME_GRACE_PERIOD_MINUTES: int = 20

    async def _handle_regime_change(self, message: Message) -> None:
        """Close positions whose entry regime no longer matches current regime.

        If a position was opened in TREND and regime flips to RANGE/CHAOS,
        close at market to free the slot for a better opportunity.

        A grace period (REGIME_GRACE_PERIOD_MINUTES) protects recently opened
        positions from being closed by transient regime readings, especially
        after a service restart where the confirmation counter resets.
        """
        payload = message.payload
        symbol = payload.get("symbol", "")
        new_regime = payload.get("regime", "")

        if symbol not in self.active_positions:
            return

        position = self.active_positions[symbol]

        # Only act on positions with known entry regime
        if not position.entry_regime:
            return

        # Only close if regime actually changed from entry
        if new_regime == position.entry_regime:
            return

        # Position is already closing — skip
        if position.status != PositionStatus.OPEN:
            return

        # Grace period: skip regime-close for very new positions
        age_minutes = (
            datetime.now(timezone.utc) - position.opened_at
        ).total_seconds() / 60.0
        if age_minutes < self.REGIME_GRACE_PERIOD_MINUTES:
            self._logger.info(
                "Regime change %s -> %s for %s ignored: position age %.1f min < %d min grace period",
                position.entry_regime,
                new_regime,
                symbol,
                age_minutes,
                self.REGIME_GRACE_PERIOD_MINUTES,
            )
            return

        self._logger.info(
            "Regime invalidation: %s changed %s -> %s, closing %s %s position",
            symbol,
            position.entry_regime,
            new_regime,
            position.side,
            symbol,
        )

        position.exit_reason = "regime_change"
        await self.close_position(symbol)

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

        # Ensure configured leverage is set before placing the order
        configured_leverage = getattr(self._bot_config.risk, "leverage", None)
        if configured_leverage:
            try:
                await self.client.update_leverage(symbol, int(configured_leverage))
                self._logger.debug("Leverage set to %dx for %s", configured_leverage, symbol)
            except Exception as e:
                self._logger.warning("Failed to set leverage for %s: %s", symbol, e)

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

                    # Check slippage - reject trade if excessive
                    if order.avg_price:
                        slippage_pct = self._record_slippage(entry_price, order.avg_price, is_buy)

                        if abs(slippage_pct) > self._exec_config.max_slippage_pct:
                            self._logger.error(
                                "REJECTING %s %s: slippage %.4f%% exceeds max %.2f%% — closing position",
                                side.upper(),
                                symbol,
                                slippage_pct,
                                self._exec_config.max_slippage_pct,
                            )

                            # Close the position immediately
                            try:
                                await self.client.place_order(
                                    symbol=symbol,
                                    is_buy=not is_buy,
                                    size=order.filled_size or size,
                                    price=None,
                                    order_type="market",
                                    reduce_only=True,
                                )
                                self._logger.info("Slippage-rejected position closed for %s", symbol)
                            except Exception as close_err:
                                self._logger.error(
                                    "Failed to close slippage-rejected position %s: %s",
                                    symbol,
                                    close_err,
                                )

                            # Defensive cleanup: cancel any orphan TP/SL orders that
                            # the sync loop may have placed during the race window.
                            try:
                                cancelled = await self.client.cancel_all_orders(symbol)
                                self._logger.info(
                                    "Orphan cleanup for slippage-rejected %s: %d orders cancelled",
                                    symbol, cancelled,
                                )
                            except Exception as cancel_err:
                                self._logger.warning(
                                    "Failed to cancel orphan orders for %s: %s",
                                    symbol, cancel_err,
                                )

                            # Publish rejection event
                            await self.publish(Topic.ORDERS, {
                                "event": "slippage_rejected",
                                "symbol": symbol,
                                "direction": direction,
                                "expected_price": entry_price,
                                "fill_price": order.avg_price,
                                "slippage_pct": round(slippage_pct, 4),
                                "max_slippage_pct": self._exec_config.max_slippage_pct,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            })

                            order.status = OrderStatus.REJECTED
                            self.metrics.orders_rejected += 1
                            self.metrics.orders_submitted += 1
                            self.metrics.last_execution_time = datetime.now(timezone.utc)
                            return None

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
    ) -> float:
        """
        Record slippage for metrics and return the slippage percentage.

        Args:
            expected_price: Expected execution price
            actual_price: Actual fill price
            is_buy: Whether this was a buy order

        Returns:
            Signed slippage percentage (positive = adverse slippage)
        """
        if is_buy:
            slippage_pct = ((actual_price - expected_price) / expected_price) * 100
        else:
            slippage_pct = ((expected_price - actual_price) / expected_price) * 100

        self.metrics.total_slippage_pct += abs(slippage_pct)

        if abs(slippage_pct) > self._exec_config.max_slippage_pct:
            self._logger.error(
                "Excessive slippage detected: %.4f%% (max: %.2f%%) - trade will be rejected",
                slippage_pct,
                self._exec_config.max_slippage_pct
            )
        else:
            self._logger.debug(
                "Slippage within limits: %.4f%% (max: %.2f%%)",
                slippage_pct,
                self._exec_config.max_slippage_pct
            )

        return slippage_pct
    
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
        
        # Capture entry momentum metrics for fade tracking
        _ms = self._market_states.get(symbol)
        _entry_rsi_slope = getattr(_ms, 'rsi_slope', 0.0) if _ms else 0.0
        _entry_ema_spread = getattr(_ms, 'ema_spread', 0.0) if _ms else 0.0

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
            entry_regime=signal.get("regime"),
            highest_price=entry_price,
            lowest_price=entry_price,
            entry_rsi_slope=_entry_rsi_slope,
            entry_ema_spread=_entry_ema_spread,
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

        # Notify risk manager so it tracks the position immediately
        await self.publish(Topic.FILLS, {
            "event": "position_opened",
            "symbol": symbol,
            "direction": direction,
            "size": float(size),
            "entry_price": float(entry_price),
            "notional": float(size * entry_price),
            "strategy": signal.get("strategy"),
        })

        # Set TP/SL orders
        await self._set_tp_sl(signal, order, position)

        # Record the size for which TP/SL were placed (for partial fill detection)
        self._tp_sl_placed_size[symbol] = float(size)

    async def _ensure_tp_sl_for_position(self, position: ExecutionPosition) -> None:
        """
        Ensure a position has TP and SL orders set.

        Called for positions discovered on exchange that may not have protection.
        Uses default percentages from config.

        Args:
            position: Position to protect
        """
        symbol = position.symbol
        is_long = position.side == "long"
        entry_price = position.entry_price
        size = position.size

        # Check if position already has TP/SL set
        if position.tp_order_id and position.sl_order_id:
            self._logger.debug(
                "%s already has TP/SL: tp=%s, sl=%s",
                symbol, position.tp_order_id, position.sl_order_id
            )
            return

        # Check for existing orders on exchange for this symbol
        try:
            open_orders = await self.client.get_open_orders()
            symbol_orders = [o for o in open_orders if o.get("symbol") == symbol]

            # If there are reduce-only orders, assume TP/SL are set
            reduce_only_orders = [o for o in symbol_orders if o.get("reduceOnly")]
            if len(reduce_only_orders) >= 2:
                self._logger.info(
                    "%s has %d reduce-only orders, assuming TP/SL present",
                    symbol, len(reduce_only_orders)
                )
                return

        except Exception as e:
            self._logger.warning("Could not check existing orders for %s: %s", symbol, e)

        # Calculate TP/SL using default percentages
        tp_pct = self._bot_config.risk.take_profit_pct / 100
        sl_pct = self._bot_config.risk.stop_loss_pct / 100

        if is_long:
            tp_price = entry_price * (1 + tp_pct)
            sl_price = entry_price * (1 - sl_pct)
        else:
            tp_price = entry_price * (1 - tp_pct)
            sl_price = entry_price * (1 + sl_pct)

        self._logger.info(
            "Setting TP/SL for existing position %s: TP=%.4f (%.1f%%), SL=%.4f (%.1f%%)",
            symbol, tp_price, tp_pct * 100, sl_price, sl_pct * 100
        )

        # Validate stop distance after rounding
        if not self._validate_stop_distance(entry_price, tp_price, sl_price, symbol):
            self._logger.error(
                "CLOSING %s: TP/SL distance collapsed after rounding — position unprotectable",
                symbol,
            )
            await self._send_alert(
                f"External position {symbol} unprotectable: TP/SL too small after rounding"
            )
            try:
                await self.client.place_order(
                    symbol=symbol,
                    is_buy=not is_long,
                    size=size,
                    price=None,
                    order_type="market",
                    reduce_only=True,
                )
            except Exception as e:
                self._logger.error("Failed to close unprotectable position %s: %s", symbol, e)
            return

        # Check if price has already passed TP level — trigger orders won't fire retroactively
        current_price = position.current_price
        tp_already_passed = (
            (is_long and current_price >= tp_price) or
            (not is_long and current_price <= tp_price)
        )
        sl_already_passed = (
            (is_long and current_price <= sl_price) or
            (not is_long and current_price >= sl_price)
        )

        if tp_already_passed or sl_already_passed:
            reason = "TP" if tp_already_passed else "SL"
            self._logger.warning(
                "%s: price %.4f already beyond %s level %.4f — closing at market",
                symbol, current_price, reason,
                tp_price if tp_already_passed else sl_price
            )
            try:
                await self.client.place_order(
                    symbol=symbol,
                    is_buy=not is_long,
                    size=size,
                    price=None,
                    order_type="market",
                    reduce_only=True,
                )
                self._logger.info("Immediate %s market close sent for %s", reason, symbol)
            except Exception as e:
                self._logger.error("Failed immediate %s close for %s: %s", reason, symbol, e)
            return

        # Place TP order with retry
        try:
            tp_result = await self._place_trigger_with_retry(
                symbol=symbol,
                is_buy=not is_long,
                size=size,
                trigger_price=tp_price,
                tpsl="tp",
            )
            position.tp_order_id = tp_result.get("orderId")
            position.tp_price = tp_price
            self._logger.info("TP trigger order placed for %s: %s @ %.4f", symbol, position.tp_order_id, tp_price)
        except Exception as e:
            self._logger.error("Failed to place TP trigger order for %s after retries: %s", symbol, e)

        # Place SL order with retry
        try:
            sl_result = await self._place_trigger_with_retry(
                symbol=symbol,
                is_buy=not is_long,
                size=size,
                trigger_price=sl_price,
                tpsl="sl",
            )
            position.sl_order_id = sl_result.get("orderId")
            position.sl_price = sl_price
            self._logger.info("SL trigger order placed for %s: %s @ %.4f", symbol, position.sl_order_id, sl_price)
        except Exception as e:
            self._logger.error("Failed to place SL order for %s after retries: %s", symbol, e)

        # Record size for partial fill detection
        self._tp_sl_placed_size[symbol] = float(size)

    def _validate_stop_distance(
        self,
        entry_price: float,
        tp_price: float,
        sl_price: float,
        symbol: str,
    ) -> bool:
        """
        Validate TP/SL distance after rounding is sufficient.

        Returns True if distances are acceptable, False if too small.
        """
        tp_rounded = self.client._round_price(tp_price)
        sl_rounded = self.client._round_price(sl_price)
        entry_rounded = self.client._round_price(entry_price)

        if entry_rounded <= 0:
            return False

        tp_dist = abs(tp_rounded - entry_rounded) / entry_rounded
        sl_dist = abs(sl_rounded - entry_rounded) / entry_rounded

        if tp_dist < MIN_STOP_DISTANCE_PCT:
            self._logger.warning(
                "TP distance too small after rounding for %s: "
                "entry=%.6f, tp=%.6f, dist=%.4f%%",
                symbol, entry_rounded, tp_rounded, tp_dist * 100,
            )
            return False

        if sl_dist < MIN_STOP_DISTANCE_PCT:
            self._logger.warning(
                "SL distance too small after rounding for %s: "
                "entry=%.6f, sl=%.6f, dist=%.4f%%",
                symbol, entry_rounded, sl_rounded, sl_dist * 100,
            )
            return False

        return True

    async def _place_trigger_with_retry(
        self,
        symbol: str,
        is_buy: bool,
        size: float,
        trigger_price: float,
        tpsl: str,
        max_retries: int = 3,
    ) -> Dict[str, Any]:
        """Place a trigger order with retry and exponential backoff."""
        for attempt in range(max_retries):
            try:
                result = await self.client.place_trigger_order(
                    symbol=symbol,
                    is_buy=is_buy,
                    size=size,
                    trigger_price=trigger_price,
                    limit_price=None,
                    tpsl=tpsl,
                    reduce_only=True,
                )
                return result
            except Exception as e:
                if attempt < max_retries - 1:
                    delay = 2 ** attempt  # 1s, 2s, 4s
                    self._logger.warning(
                        "%s placement attempt %d/%d failed for %s: %s. Retrying in %ds",
                        tpsl.upper(), attempt + 1, max_retries, symbol, e, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                self._logger.error(
                    "CRITICAL: Failed to place %s for %s after %d retries: %s",
                    tpsl.upper(), symbol, max_retries, e,
                )
                await self._send_alert(
                    f"CRITICAL: {tpsl.upper()} placement failed for {symbol} after {max_retries} retries: {e}"
                )
                raise

    async def _send_alert(self, message: str) -> None:
        """Send critical alert via notification bus."""
        self._logger.critical(message)
        try:
            await self.publish(Topic.ORDERS, {
                "event": "critical_alert",
                "message": message,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass  # Best-effort alert

    async def _set_tp_sl(
        self,
        signal: Dict[str, Any],
        order: Order,
        position: ExecutionPosition
    ) -> None:
        """
        Set take profit and stop loss orders using proper trigger orders.

        TP/SL are always calculated from config fixed percentages (stop_loss_pct,
        take_profit_pct). These values are data-driven from parameter sweep.
        ATR-adaptive branch was removed: it produced TP=4-8% and SL=0.5-2%,
        far too wide for a 15m momentum scalper.

        Both TP and SL are always calculated from the actual fill price.

        ATR data (entry_atr_pct) is still stored on the position for the
        trailing stop system, which uses it independently.

        Args:
            signal: Original signal with TP/SL levels
            order: Filled entry order
            position: ExecutionPosition to protect
        """
        symbol = signal["symbol"]
        is_long = signal["direction"] == "long"
        entry_price = order.avg_price or signal["entry_price"]
        size = position.size

        # --- Fixed TP/SL from config (always) ---
        sl_pct = self._bot_config.risk.stop_loss_pct / 100
        tp_pct = self._bot_config.risk.take_profit_pct / 100

        # Store ATR on position for trailing stop calculations (if available)
        atr_pct_raw = signal.get("atr_pct", 0)
        if atr_pct_raw and float(atr_pct_raw) > 0:
            position.entry_atr_pct = float(atr_pct_raw)

        # Calculate prices from fill price
        if is_long:
            sl_price = entry_price * (1 - sl_pct)
            tp_price = entry_price * (1 + tp_pct)
        else:
            sl_price = entry_price * (1 + sl_pct)
            tp_price = entry_price * (1 - tp_pct)

        self._logger.info(
            "Setting TP/SL for %s %s: entry=%.4f, TP=%.4f (%.1f%%), SL=%.4f (%.1f%%)",
            "LONG" if is_long else "SHORT",
            symbol,
            entry_price,
            tp_price,
            tp_pct * 100,
            sl_price,
            sl_pct * 100,
        )

        # Validate stop distance after rounding (prevents 0% SL on low-price assets)
        if not self._validate_stop_distance(entry_price, tp_price, sl_price, symbol):
            self._logger.error(
                "CLOSING %s: TP/SL distance collapsed after rounding — position unprotectable",
                symbol,
            )
            await self._send_alert(
                f"Trade rejected: {symbol} TP/SL too small after rounding (entry={entry_price:.6f})"
            )
            try:
                await self.client.place_order(
                    symbol=symbol,
                    is_buy=not is_long,
                    size=size,
                    price=None,
                    order_type="market",
                    reduce_only=True,
                )
            except Exception as e:
                self._logger.error("Failed to close unprotectable position %s: %s", symbol, e)
            return

        # Place TP order with retry
        try:
            tp_result = await self._place_trigger_with_retry(
                symbol=symbol,
                is_buy=not is_long,
                size=size,
                trigger_price=tp_price,
                tpsl="tp",
            )
            position.tp_order_id = tp_result.get("orderId")
            position.tp_price = tp_price
            self._logger.info("TP trigger order placed: %s @ %.4f", position.tp_order_id, tp_price)

        except Exception as e:
            self._logger.error("Failed to place TP trigger order after retries: %s", e)

        # Place SL order with retry
        try:
            sl_result = await self._place_trigger_with_retry(
                symbol=symbol,
                is_buy=not is_long,
                size=size,
                trigger_price=sl_price,
                tpsl="sl",
            )
            position.sl_order_id = sl_result.get("orderId")
            position.sl_price = sl_price
            self._logger.info("SL trigger order placed: %s @ %.4f", position.sl_order_id, sl_price)

        except Exception as e:
            self._logger.error("Failed to place SL trigger order after retries: %s", e)
    
    async def _update_tp_sl_for_size_change(
        self, position: ExecutionPosition
    ) -> None:
        """Cancel existing TP/SL and re-place them for the updated position size.

        Called when ``_sync_positions_from_exchange`` detects that the exchange
        position size has grown (additional fills arrived after TP/SL were
        already placed for a smaller partial fill).

        The new TP/SL are placed at the **same price levels** as before
        (the entry price may have changed slightly due to average fill, but
        the percentage distances remain the same).  Only the **size** on the
        trigger orders is updated.

        Args:
            position: The active position whose size has changed.
        """
        symbol = position.symbol
        is_long = position.side == "long"
        new_size = position.size

        # 1) Cancel old TP trigger
        if position.tp_order_id:
            try:
                await self.client.cancel_order(symbol, int(position.tp_order_id))
                self._logger.info(
                    "Cancelled old TP %s for %s (size change)",
                    position.tp_order_id, symbol,
                )
            except Exception as e:
                self._logger.warning(
                    "Failed to cancel old TP %s for %s: %s",
                    position.tp_order_id, symbol, e,
                )

        # 2) Cancel old SL trigger
        if position.sl_order_id:
            try:
                await self.client.cancel_order(symbol, int(position.sl_order_id))
                self._logger.info(
                    "Cancelled old SL %s for %s (size change)",
                    position.sl_order_id, symbol,
                )
            except Exception as e:
                self._logger.warning(
                    "Failed to cancel old SL %s for %s: %s",
                    position.sl_order_id, symbol, e,
                )

        # 3) Re-place TP at the same price, new size
        if position.tp_price:
            try:
                tp_result = await self._place_trigger_with_retry(
                    symbol=symbol,
                    is_buy=not is_long,
                    size=new_size,
                    trigger_price=position.tp_price,
                    tpsl="tp",
                )
                position.tp_order_id = tp_result.get("orderId")
                self._logger.info(
                    "TP re-placed for %s: size %.4f @ %.4f (order %s)",
                    symbol, new_size, position.tp_price, position.tp_order_id,
                )
            except Exception as e:
                self._logger.error(
                    "Failed to re-place TP for %s after size change: %s",
                    symbol, e,
                )

        # 4) Re-place SL at the same price, new size
        if position.sl_price:
            try:
                sl_result = await self._place_trigger_with_retry(
                    symbol=symbol,
                    is_buy=not is_long,
                    size=new_size,
                    trigger_price=position.sl_price,
                    tpsl="sl",
                )
                position.sl_order_id = sl_result.get("orderId")
                self._logger.info(
                    "SL re-placed for %s: size %.4f @ %.4f (order %s)",
                    symbol, new_size, position.sl_price, position.sl_order_id,
                )
            except Exception as e:
                self._logger.error(
                    "Failed to re-place SL for %s after size change: %s",
                    symbol, e,
                )

        # 5) Update tracking
        self._tp_sl_placed_size[symbol] = new_size

        tp_str = f"{position.tp_price:.4f}" if position.tp_price else "N/A"
        sl_str = f"{position.sl_price:.4f}" if position.sl_price else "N/A"
        await self._send_alert(
            f"TP/SL updated for {symbol}: size grew to {new_size:.4f} "
            f"(TP={tp_str}, SL={sl_str})"
        )

    # =========================================================================
    # Position Monitoring
    # =========================================================================

    async def _cancel_stale_orders(self) -> None:
        """Cancel pending limit orders that exceeded the timeout.

        Iterates over ``self.pending_orders`` and cancels any order whose
        age exceeds ``limit_timeout_seconds`` (default 60 s from config).
        Cancelled orders are removed from local tracking and metrics are
        updated.
        """
        timeout = timedelta(seconds=self._exec_config.limit_timeout_seconds)
        now = datetime.now(timezone.utc)

        stale_ids: List[str] = []
        for order_id, order in self.pending_orders.items():
            if order.submitted_at and (now - order.submitted_at) > timeout:
                stale_ids.append(order_id)

        for order_id in stale_ids:
            order = self.pending_orders[order_id]
            age_seconds = (
                (now - order.submitted_at).total_seconds()
                if order.submitted_at
                else 0
            )
            self._logger.warning(
                "Auto-cancelling stale %s order %s for %s "
                "(age: %.0fs, timeout: %.0fs)",
                order.order_type,
                order_id,
                order.symbol,
                age_seconds,
                timeout.total_seconds(),
            )
            try:
                await self.client.cancel_order(order.symbol, int(order_id))
                order.status = OrderStatus.CANCELLED
                self.metrics.orders_cancelled += 1
                self._logger.info(
                    "Stale order %s cancelled successfully", order_id
                )
            except Exception as e:
                self._logger.error(
                    "Failed to cancel stale order %s: %s", order_id, e
                )
            finally:
                self.pending_orders.pop(order_id, None)
                self._partial_fill_first_seen.pop(order_id, None)
                # Notify risk manager so it clears its pending intent
                await self.publish(Topic.ORDERS, {
                    "event": "order_cancelled",
                    "symbol": order.symbol,
                    "order_id": order_id,
                    "reason": "stale_timeout",
                })

    async def _poll_pending_order_fills(self) -> None:
        """Poll exchange for fills on pending limit orders.

        Checks if pending orders have disappeared from open orders,
        indicating they were either filled or cancelled externally.
        If a position exists for the symbol, treats it as a fill and
        triggers SL/TP placement.

        Partial fill handling:
        When an order is filled in multiple tranches, the exchange may
        report a partial position size before all fills arrive.  To avoid
        placing TP/SL on the wrong size, we wait up to
        ``PARTIAL_FILL_GRACE_SECONDS`` (10 s) after the order leaves the
        open-orders list.  During that grace window we keep polling: if
        the position size grows, we reset the timer.  Only when the size
        stabilises (or the grace period expires) do we treat the order as
        fully filled and place TP/SL.
        """
        PARTIAL_FILL_GRACE_SECONDS = 10

        if not self.pending_orders:
            return

        try:
            open_orders = await self.client.get_open_orders()
            open_order_ids = {str(o["orderId"]) for o in open_orders}
        except Exception as e:
            self._logger.warning("Failed to poll open orders: %s", e)
            return

        now = datetime.now(timezone.utc)

        for order_id, order in list(self.pending_orders.items()):
            if str(order_id) in open_order_ids:
                # Order still open — clear any stale partial-fill tracking
                self._partial_fill_first_seen.pop(order_id, None)
                continue

            # Skip symbols mid-open/close to avoid racing with _handle_signal
            if order.symbol in self._settling_symbols:
                self._logger.debug(
                    "Skipping fill poll for settling symbol %s", order.symbol
                )
                continue

            # Order no longer open -> filled or cancelled externally
            try:
                positions = await self.client.get_positions()
                pos = next(
                    (p for p in positions
                     if p["symbol"] == order.symbol and abs(p.get("size", 0)) > 0.0001),
                    None,
                )

                if pos:
                    exchange_size = abs(pos.get("size", 0))
                    expected_size = order.size  # Original intended size

                    # --- Partial fill grace period ---
                    # If exchange size is significantly less than expected,
                    # wait for remaining fills to arrive.
                    size_ratio = exchange_size / expected_size if expected_size > 0 else 1.0
                    is_partial = size_ratio < 0.95  # <95% of expected = partial

                    if is_partial:
                        if order_id not in self._partial_fill_first_seen:
                            # First time seeing this partial fill
                            self._partial_fill_first_seen[order_id] = (now, exchange_size)
                            self._logger.info(
                                "Partial fill detected for %s %s: "
                                "%.4f/%.4f (%.0f%%) — waiting for remaining fills "
                                "(grace: %ds)",
                                order_id, order.symbol,
                                exchange_size, expected_size,
                                size_ratio * 100,
                                PARTIAL_FILL_GRACE_SECONDS,
                            )
                            continue
                        else:
                            first_seen_time, last_size = self._partial_fill_first_seen[order_id]
                            elapsed = (now - first_seen_time).total_seconds()

                            if exchange_size > last_size:
                                # Size grew — additional fill arrived, reset timer
                                self._partial_fill_first_seen[order_id] = (now, exchange_size)
                                self._logger.info(
                                    "Additional fill for %s %s: "
                                    "%.4f -> %.4f (%.0f%%) — resetting grace timer",
                                    order_id, order.symbol,
                                    last_size, exchange_size,
                                    (exchange_size / expected_size * 100) if expected_size > 0 else 100,
                                )
                                continue

                            if elapsed < PARTIAL_FILL_GRACE_SECONDS:
                                # Still within grace period, keep waiting
                                self._logger.debug(
                                    "Partial fill %s %s: %.4f/%.4f — "
                                    "%.1fs / %ds grace remaining",
                                    order_id, order.symbol,
                                    exchange_size, expected_size,
                                    elapsed, PARTIAL_FILL_GRACE_SECONDS,
                                )
                                continue

                            # Grace period expired with partial fill
                            self._logger.warning(
                                "Partial fill grace expired for %s %s: "
                                "%.4f/%.4f (%.0f%%) — proceeding with actual size",
                                order_id, order.symbol,
                                exchange_size, expected_size,
                                size_ratio * 100,
                            )

                    # --- Fill confirmed (full or grace-expired partial) ---
                    self._partial_fill_first_seen.pop(order_id, None)

                    self._logger.info(
                        "Pending order %s for %s detected as FILLED via polling "
                        "(size: %.4f, expected: %.4f)",
                        order_id, order.symbol,
                        exchange_size, expected_size,
                    )
                    order.status = OrderStatus.FILLED
                    order.avg_price = pos.get("entryPrice", 0)
                    order.filled_size = exchange_size
                    order.filled_at = datetime.now(timezone.utc)

                    signal = order.original_signal or {
                        "symbol": order.symbol,
                        "direction": "long" if order.side == "buy" else "short",
                        "entry_price": order.avg_price,
                        "size": order.filled_size,
                        "strategy": order.strategy,
                    }
                    await self._handle_order_filled(signal, order)
                    self.pending_orders.pop(order_id, None)
                    self.metrics.orders_filled += 1
                else:
                    # No position -> order was cancelled externally
                    self._partial_fill_first_seen.pop(order_id, None)
                    self._logger.info(
                        "Pending order %s for %s cancelled externally",
                        order_id, order.symbol,
                    )
                    order.status = OrderStatus.CANCELLED
                    self.pending_orders.pop(order_id, None)
                    self.metrics.orders_cancelled += 1
                    await self.publish(Topic.ORDERS, {
                        "event": "order_cancelled",
                        "symbol": order.symbol,
                        "order_id": order_id,
                        "reason": "external_cancel",
                    })

            except Exception as e:
                self._logger.error(
                    "Error checking fill status for order %s: %s", order_id, e
                )

    async def _monitor_positions(self) -> None:
        """Background task to monitor positions and fills."""
        self._logger.info("Position monitoring started")

        while True:
            try:
                # Poll for limit order fills (detect filled orders and set TP/SL)
                await self._poll_pending_order_fills()

                # Cancel stale limit orders that exceeded timeout
                await self._cancel_stale_orders()

                # Sync positions from exchange
                await self._sync_positions_from_exchange()

                # Check for closed positions (SL/TP hit)
                await self._check_closed_positions()

                # Move SL to breakeven when profit threshold is reached
                await self._check_breakeven_stops()

                # Trail SL behind price (ATR-based, after breakeven)
                await self._check_trailing_stops()

                # Check for ROI-based exits (time-based take profit)
                await self._check_roi_exits()

                # Check for momentum fade exits
                await self._check_momentum_fade()

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
                entry_px = pos.get("entryPrice", 0)
                notional = abs(size) * entry_px

                # Filter dust positions (< $1 notional) to prevent ghost blocking
                if abs(size) < 0.0001 or notional < 1.0:
                    continue

                exchange_symbols.add(symbol)

                if symbol in self.active_positions:
                    # Update existing position
                    local_pos = self.active_positions[symbol]
                    local_pos.current_price = pos.get("markPrice", local_pos.entry_price)
                    local_pos.unrealized_pnl = pos.get("unrealizedPnl", 0)

                    # --- Detect position size growth (additional fills) ---
                    old_size = local_pos.size
                    new_size = abs(size)
                    local_pos.size = new_size

                    # Also update entry price from exchange (avg after multiple fills)
                    if pos.get("entryPrice", 0) > 0:
                        local_pos.entry_price = pos.get("entryPrice", 0)

                    tp_sl_size = self._tp_sl_placed_size.get(symbol, 0)
                    if (
                        new_size > old_size
                        and tp_sl_size > 0
                        and new_size > tp_sl_size * 1.02  # >2% growth = real fill, not float noise
                        and local_pos.status == PositionStatus.OPEN
                        and symbol not in self._settling_symbols
                    ):
                        self._logger.warning(
                            "Position size grew for %s: %.4f -> %.4f "
                            "(TP/SL were placed for %.4f) — re-placing TP/SL",
                            symbol, old_size, new_size, tp_sl_size,
                        )
                        await self._update_tp_sl_for_size_change(local_pos)

                else:
                    # New position (opened externally or before service start)
                    # Retry TP/SL on each sync until confirmed
                    if symbol in self._tp_sl_confirmed:
                        continue

                    # Skip symbols mid-open/close/rejection to avoid orphan TP/SL
                    if symbol in self._settling_symbols:
                        self._logger.debug(
                            "Skipping sync for settling symbol %s", symbol
                        )
                        continue

                    entry_px = pos.get("entryPrice", 0)
                    new_pos = ExecutionPosition(
                        symbol=symbol,
                        side=pos.get("side", "long"),
                        size=abs(size),
                        entry_price=entry_px,
                        current_price=pos.get("markPrice", 0),
                        unrealized_pnl=pos.get("unrealizedPnl", 0),
                        leverage=pos.get("leverage", 1),
                        status=PositionStatus.OPEN,
                        opened_at=datetime.now(timezone.utc),
                        entry_regime="trend",  # We only open in TREND
                        highest_price=entry_px,
                        lowest_price=entry_px,
                    )
                    self.active_positions[symbol] = new_pos
                    self._logger.info(
                        "Synced existing position: %s %s %.4f",
                        new_pos.side,
                        symbol,
                        abs(size)
                    )

                    # Check and set SL/TP for newly discovered positions
                    await self._ensure_tp_sl_for_position(new_pos)
                    # Mark TP/SL as confirmed if both were set
                    if new_pos.tp_order_id and new_pos.sl_order_id:
                        self._tp_sl_confirmed.add(symbol)

            # Check for closed positions (skip settling symbols to avoid spurious notifications)
            closed_symbols = set(self.active_positions.keys()) - exchange_symbols
            for symbol in closed_symbols:
                if symbol in self._settling_symbols:
                    self._logger.debug(
                        "Skipping close detection for settling symbol %s", symbol
                    )
                    continue
                if self.active_positions[symbol].status == PositionStatus.OPEN:
                    await self._handle_position_closed(symbol)

        except Exception as e:
            self._logger.error("Failed to sync positions: %s", e)
    
    async def _check_closed_positions(self) -> None:
        """Check for positions whose price crossed TP or SL.

        When a TP/SL level is breached, a market-close order is sent and the
        position is marked CLOSING so that subsequent iterations do not
        re-trigger the same close (preventing log spam).
        """
        for symbol, position in list(self.active_positions.items()):
            if position.status in (PositionStatus.CLOSED, PositionStatus.CLOSING):
                continue

            # Skip if a close order was already sent for this position
            if symbol in self._closing_positions:
                continue

            # Check if TP or SL was hit based on price
            if not (position.tp_price and position.sl_price):
                continue

            hit_reason: Optional[str] = None

            if position.side == "long":
                if position.current_price >= position.tp_price:
                    hit_reason = "take_profit"
                    self._logger.info(
                        "%s TP hit: %.2f >= %.2f",
                        symbol,
                        position.current_price,
                        position.tp_price,
                    )
                elif position.current_price <= position.sl_price:
                    hit_reason = "stop_loss"
                    self._logger.info(
                        "%s SL hit: %.2f <= %.2f",
                        symbol,
                        position.current_price,
                        position.sl_price,
                    )
            else:
                if position.current_price <= position.tp_price:
                    hit_reason = "take_profit"
                    self._logger.info(
                        "%s TP hit: %.2f <= %.2f",
                        symbol,
                        position.current_price,
                        position.tp_price,
                    )
                elif position.current_price >= position.sl_price:
                    hit_reason = "stop_loss"
                    self._logger.info(
                        "%s SL hit: %.2f >= %.2f",
                        symbol,
                        position.current_price,
                        position.sl_price,
                    )

            if hit_reason:
                # Guard: mark immediately so the next iteration won't re-fire
                self._closing_positions.add(symbol)
                position.status = PositionStatus.CLOSING
                position.exit_reason = hit_reason

                try:
                    await self.close_position(symbol)
                except Exception as e:
                    self._logger.error(
                        "Failed to close %s after %s: %s", symbol, hit_reason, e
                    )

    async def _check_breakeven_stops(self) -> None:
        """Move SL to entry price (breakeven) when unrealized P&L reaches threshold.

        For each OPEN position that has not yet been moved to breakeven:
        1. Calculate current P&L % using Decimal arithmetic.
        2. If P&L % >= BREAKEVEN_THRESHOLD_PCT:
           a. Cancel the old SL trigger on the exchange.
           b. Place a new SL trigger at the entry price.
           c. Mark the position as breakeven_activated.
        """
        from decimal import Decimal

        threshold = Decimal(str(BREAKEVEN_THRESHOLD_PCT))

        for symbol, position in list(self.active_positions.items()):
            if position.status != PositionStatus.OPEN:
                continue
            if position.breakeven_activated:
                continue
            if symbol in self._settling_symbols:
                continue
            if position.entry_price <= 0:
                continue

            # Calculate P&L % with Decimal precision
            entry = Decimal(str(position.entry_price))
            current = Decimal(str(position.current_price))

            if position.side == "long":
                pnl_pct = (current - entry) / entry * Decimal("100")
            else:
                pnl_pct = (entry - current) / entry * Decimal("100")

            if pnl_pct < threshold:
                continue

            # --- Breakeven triggered ---
            is_long = position.side == "long"
            # Offset above/below entry to cover round-trip exchange fees
            if is_long:
                new_sl_price = position.entry_price * (1 + BREAKEVEN_OFFSET_PCT / 100)
            else:
                new_sl_price = position.entry_price * (1 - BREAKEVEN_OFFSET_PCT / 100)

            self._logger.info(
                "Breakeven activated: %s %s — SL moved to entry %.4f (P&L %.2f%%)",
                "LONG" if is_long else "SHORT",
                symbol,
                new_sl_price,
                float(pnl_pct),
            )

            # 1) Cancel old SL trigger
            if position.sl_order_id:
                try:
                    await self.client.cancel_order(symbol, int(position.sl_order_id))
                    self._logger.debug(
                        "Cancelled old SL order %s for %s", position.sl_order_id, symbol,
                    )
                except Exception as e:
                    self._logger.warning(
                        "Failed to cancel old SL %s for %s: %s",
                        position.sl_order_id, symbol, e,
                    )

            # 2) Place new SL trigger at entry (breakeven)
            try:
                sl_result = await self._place_trigger_with_retry(
                    symbol=symbol,
                    is_buy=not is_long,
                    size=position.size,
                    trigger_price=new_sl_price,
                    tpsl="sl",
                )
                position.sl_order_id = sl_result.get("orderId")
                position.sl_price = new_sl_price
                position.breakeven_activated = True
                self._logger.info(
                    "Breakeven SL placed: %s @ %.4f (order %s)",
                    symbol, new_sl_price, position.sl_order_id,
                )
            except Exception as e:
                self._logger.error(
                    "Failed to place breakeven SL for %s: %s", symbol, e,
                )

    async def _check_trailing_stops(self) -> None:
        """ATR-based trailing stop.

        Activates only after breakeven has been triggered. Once active, the SL
        follows price at a distance of trailing_atr_mult x ATR from the peak
        (LONG) or trough (SHORT). The SL is only ever tightened, never loosened.

        Requirements for trailing to engage:
        - Position must be OPEN
        - Breakeven must already be activated
        - entry_atr_pct must be > 0 (ATR was available at entry)
        - current_price must be > 0
        """
        trailing_mult = getattr(
            getattr(self._bot_config, "stops", None),
            "trailing_atr_mult", 2.5
        )

        for symbol, position in list(self.active_positions.items()):
            if position.status != PositionStatus.OPEN:
                continue
            if not position.breakeven_activated:
                continue  # Trailing only after breakeven
            if position.entry_atr_pct <= 0:
                continue  # No ATR data, skip
            if symbol in self._settling_symbols:
                continue

            current = position.current_price
            if current <= 0:
                continue

            # Trailing distance = entry_price * (atr_pct / 100) * trailing_mult
            trail_distance = position.entry_price * (position.entry_atr_pct / 100) * trailing_mult

            if position.side == "long":
                # Update peak price
                if current > position.highest_price:
                    position.highest_price = current

                # Calculate trailing SL from peak
                new_sl = position.highest_price - trail_distance

                # Only tighten SL (never lower it for longs)
                if new_sl > position.sl_price:
                    if not position.trailing_active:
                        position.trailing_active = True
                        self._logger.info(
                            "TRAILING activated %s LONG: peak=%.4f, trail_sl=%.4f (was %.4f)",
                            symbol, position.highest_price, new_sl, position.sl_price,
                        )

                    # Update SL on exchange
                    try:
                        if position.sl_order_id:
                            await self.client.cancel_order(symbol, int(position.sl_order_id))

                        sl_result = await self._place_trigger_with_retry(
                            symbol=symbol,
                            is_buy=False,  # Close long = sell
                            size=position.size,
                            trigger_price=new_sl,
                            tpsl="sl",
                        )
                        position.sl_order_id = sl_result.get("orderId")
                        position.sl_price = new_sl
                        self._logger.info(
                            "TRAILING updated %s: new_sl=%.4f (peak=%.4f, trail=%.4f)",
                            symbol, new_sl, position.highest_price, trail_distance,
                        )
                    except Exception as e:
                        self._logger.error("Failed to update trailing SL for %s: %s", symbol, e)

            elif position.side == "short":
                # Update trough price
                if current < position.lowest_price:
                    position.lowest_price = current

                # Calculate trailing SL from trough
                new_sl = position.lowest_price + trail_distance

                # Only tighten SL (never raise it for shorts)
                if new_sl < position.sl_price:
                    if not position.trailing_active:
                        position.trailing_active = True
                        self._logger.info(
                            "TRAILING activated %s SHORT: trough=%.4f, trail_sl=%.4f (was %.4f)",
                            symbol, position.lowest_price, new_sl, position.sl_price,
                        )

                    # Update SL on exchange
                    try:
                        if position.sl_order_id:
                            await self.client.cancel_order(symbol, int(position.sl_order_id))

                        sl_result = await self._place_trigger_with_retry(
                            symbol=symbol,
                            is_buy=True,  # Close short = buy
                            size=position.size,
                            trigger_price=new_sl,
                            tpsl="sl",
                        )
                        position.sl_order_id = sl_result.get("orderId")
                        position.sl_price = new_sl
                        self._logger.info(
                            "TRAILING updated %s: new_sl=%.4f (trough=%.4f, trail=%.4f)",
                            symbol, new_sl, position.lowest_price, trail_distance,
                        )
                    except Exception as e:
                        self._logger.error("Failed to update trailing SL for %s: %s", symbol, e)

    async def should_exit_on_roi(self, position: ExecutionPosition) -> tuple[bool, float, float]:
        """
        Check if position should exit based on graduated ROI target.
        
        ROI targets decrease over time:
        - 0-30min: 3% profit target
        - 30-60min: 2% profit target  
        - 1-2h: 1.5% profit target
        - 2-4h: 1% profit target
        - 4-8h: 0.5% profit target
        - 8h+: Break-even (exit at any profit)
        
        Args:
            position: ExecutionPosition to check
            
        Returns:
            Tuple of (should_exit, current_roi_pct, target_roi_pct)
        """
        # Get ROI config from bot config
        roi_config: Dict[str, float] = {}
        try:
            if hasattr(self._bot_config, 'stops') and hasattr(self._bot_config.stops, 'minimal_roi'):
                roi_config = self._bot_config.stops.minimal_roi or {}
        except Exception:
            pass
        
        if not roi_config:
            return False, 0.0, 0.0  # ROI not configured
        
        # Need opened_at to calculate time in trade
        if not position.opened_at:
            return False, 0.0, 0.0
        
        # Calculate time in trade (minutes)
        now = datetime.now(timezone.utc)
        time_in_trade_seconds = (now - position.opened_at).total_seconds()
        time_in_trade_min = time_in_trade_seconds / 60
        
        # Find current ROI target based on time elapsed
        # ROI config keys are strings representing minutes
        target_roi = 0.0
        applicable_threshold = 0
        
        for time_threshold_str, roi_value in sorted(
            roi_config.items(),
            key=lambda x: int(x[0])  # Sort by time threshold
        ):
            time_threshold_min = int(time_threshold_str)
            
            if time_in_trade_min >= time_threshold_min:
                target_roi = float(roi_value)
                applicable_threshold = time_threshold_min
            else:
                break  # We've passed applicable thresholds
        
        # Calculate current ROI %
        if position.entry_price <= 0:
            return False, 0.0, target_roi
        
        if position.side == "long":
            current_roi_pct = (position.current_price - position.entry_price) / position.entry_price
        else:  # short
            current_roi_pct = (position.entry_price - position.current_price) / position.entry_price
        
        # Check if current ROI meets target
        if current_roi_pct >= target_roi:
            self._logger.info(
                "ROI target reached for %s: Current ROI %.2f%% >= Target %.2f%% "
                "(time in trade: %.1f min, threshold: %d min)",
                position.symbol,
                current_roi_pct * 100,
                target_roi * 100,
                time_in_trade_min,
                applicable_threshold
            )
            return True, current_roi_pct, target_roi
        
        return False, current_roi_pct, target_roi

    async def _check_roi_exits(self) -> None:
        """
        Check all active positions for ROI-based exits.
        
        This is called from _monitor_positions and checks if any position
        has reached its time-based ROI target.
        """
        for symbol, position in list(self.active_positions.items()):
            if position.status != PositionStatus.OPEN:
                continue
            if symbol in self._settling_symbols:
                continue

            try:
                should_exit, current_roi, target_roi = await self.should_exit_on_roi(position)
                
                if should_exit:
                    # Calculate time in trade for logging
                    time_in_trade_str = "unknown"
                    if position.opened_at:
                        time_in_trade_sec = (datetime.now(timezone.utc) - position.opened_at).total_seconds()
                        hours = int(time_in_trade_sec // 3600)
                        minutes = int((time_in_trade_sec % 3600) // 60)
                        time_in_trade_str = f"{hours}h {minutes}m"
                    
                    # Calculate PnL
                    pnl = position.unrealized_pnl
                    
                    self._logger.info(
                        "Closing trade via ROI: %s | Entry: $%.2f | "
                        "Exit: $%.2f | PnL: $%.2f (%.2f%%) | "
                        "Time in trade: %s | ROI target: %.2f%%",
                        symbol,
                        position.entry_price,
                        position.current_price,
                        pnl,
                        current_roi * 100,
                        time_in_trade_str,
                        target_roi * 100
                    )
                    
                    # Mark exit reason before closing
                    position.exit_reason = "roi_target"
                    
                    # Close position
                    await self.close_position(symbol)
                    
            except Exception as e:
                self._logger.error(
                    "Error checking ROI exit for %s: %s",
                    symbol, e
                )
    
    async def _check_momentum_fade(self) -> None:
        """
        Close positions where momentum has faded while still in profit.

        Logic:
        - Position must be OPEN (not closing/settling)
        - Age > min_age_minutes (grace period)
        - Profit > min_profit_pct
        - RSI slope has reversed or flattened:
            LONG: rsi_slope < +threshold
            SHORT: rsi_slope > -threshold
        """
        # Check if momentum exit is enabled
        momentum_cfg = getattr(self._bot_config, 'momentum_exit', None)
        if not momentum_cfg or not getattr(momentum_cfg, 'enabled', False):
            return

        min_age = momentum_cfg.min_age_minutes
        min_profit = momentum_cfg.min_profit_pct
        threshold = momentum_cfg.rsi_slope_threshold

        now = datetime.now(timezone.utc)

        for symbol, position in list(self.active_positions.items()):
            # Guard: only OPEN positions
            if position.status != PositionStatus.OPEN:
                continue

            # Guard: not already closing
            if symbol in self._closing_positions:
                continue

            # Guard: not settling
            if symbol in self._settling_symbols:
                continue

            # Guard: position age
            if not position.opened_at:
                continue
            age_minutes = (now - position.opened_at).total_seconds() / 60.0
            if age_minutes < min_age:
                continue

            # Guard: must be in profit
            if position.entry_price <= 0:
                continue
            if position.side == "long":
                profit_pct = ((position.current_price - position.entry_price) / position.entry_price) * 100
            else:
                profit_pct = ((position.entry_price - position.current_price) / position.entry_price) * 100

            if profit_pct < min_profit:
                continue

            # Get current RSI slope from market states
            market_state = self._market_states.get(symbol)
            if not market_state:
                continue

            current_rsi_slope = getattr(market_state, 'rsi_slope', None)
            if current_rsi_slope is None:
                continue

            # Check if momentum has faded
            faded = False
            if position.side == "long" and current_rsi_slope < threshold:
                faded = True
            elif position.side == "short" and current_rsi_slope > -threshold:
                faded = True

            if faded:
                self._logger.info(
                    "MOMENTUM FADE: %s %s | rsi_slope=%.2f (entry=%.2f) | "
                    "P&L=%.2f%% | age=%.1f min | closing",
                    position.side.upper(), symbol,
                    current_rsi_slope, position.entry_rsi_slope,
                    profit_pct, age_minutes,
                )
                position.exit_reason = "momentum_fade"
                await self.close_position(symbol)

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

        # Cancel remaining TP/SL orders (the one that didn't trigger)
        for order_id in [position.tp_order_id, position.sl_order_id]:
            if order_id:
                try:
                    await self.client.cancel_order(symbol, int(order_id))
                    self._logger.info("Cancelled residual order %s for %s", order_id, symbol)
                except Exception as e:
                    self._logger.debug("Could not cancel order %s (likely already filled): %s", order_id, e)

        self.metrics.positions_closed += 1

        self._logger.info(
            "Position closed: %s %s - PnL: %.2f",
            position.side,
            symbol,
            position.unrealized_pnl
        )

        # Calculate PnL percentage for notification
        pnl_pct = 0.0
        if position.entry_price > 0:
            if position.side == "long":
                pnl_pct = ((position.current_price - position.entry_price) / position.entry_price) * 100
            else:
                pnl_pct = ((position.entry_price - position.current_price) / position.entry_price) * 100

        # Publish fill/close event with flat fields for notifications
        await self.publish(Topic.FILLS, {
            "event": "position_closed",
            "symbol": symbol,
            "side": position.side,
            "entry_price": position.entry_price,
            "exit_price": position.current_price,
            "realized_pnl": position.unrealized_pnl,
            "pnl_pct": pnl_pct,
            "exit_reason": position.exit_reason,
            "position": position.to_dict(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        
        # Clean up tracking sets
        self._tp_sl_confirmed.discard(symbol)
        self._closing_positions.discard(symbol)
        self._settling_symbols.discard(symbol)  # Defensive: should already be cleared by _handle_signal finally
        self._tp_sl_placed_size.pop(symbol, None)

        # Clean up - remove from active after a delay
        # to allow fill processing
        await asyncio.sleep(1)
        if symbol in self.active_positions:
            del self.active_positions[symbol]
    
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
) -> ExecutionEngineService:
    """
    Create and configure an ExecutionEngineService.

    Args:
        bus: MessageBus instance
        config: Bot configuration
        client: Connected HyperliquidClient

    Returns:
        Configured ExecutionEngineService
    """
    return ExecutionEngineService(
        bus=bus,
        config=config,
        client=client,
    )
