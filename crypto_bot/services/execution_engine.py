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
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

from .base import BaseService
from .message_bus import Message, MessageBus, Topic
from ..api.exceptions import OrderRejectedError
from ..flag_trader.open_sidecar import delete_open_sidecar, write_open_sidecar

# For type checking we want the real classes; at runtime fall back to Any
# so the module still imports if the optional deps are missing.
if TYPE_CHECKING:
    from crypto_bot.api.hyperliquid import HyperliquidClient
    from crypto_bot.config.loader import BotConfig, BotExecutionConfig
else:
    try:
        from crypto_bot.api.hyperliquid import HyperliquidClient
        from crypto_bot.config.loader import BotConfig, BotExecutionConfig
    except ImportError:
        HyperliquidClient = Any
        BotConfig = Any
        BotExecutionConfig = Any


logger = logging.getLogger(__name__)

# Minimum acceptable distance between entry and TP/SL after rounding.
# If rounding collapses the distance below this (e.g. low-price assets),
# the trade is rejected to prevent instant SL triggers.
MIN_STOP_DISTANCE_PCT = 0.001  # 0.1%

def _ensure_aware(dt: datetime) -> datetime:
    """Return a timezone-aware datetime; assume UTC if naive."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _exit_reason_to_v2(legacy: Optional[str]) -> Optional[str]:
    """Map legacy exit_reason -> STAGE A v2 enum {tp, sl, expiry, manual}."""
    if not legacy:
        return None
    s = str(legacy).lower()
    if s in ("take_profit", "tp"):
        return "tp"
    if s in ("stop_loss", "sl", "trailing_stop", "violation_exit"):
        return "sl"
    if s in ("timeout", "regime_change", "max_hold", "expiry", "regime_exit"):
        return "expiry"
    if s in ("manual", "external_close"):
        return "manual"
    return None


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
    entry_mode: str = "taker"  # "taker" or "maker" (post-only)
    model_tp_pct: Optional[float] = None   # FLAG-Trader TP head output — preserved for deferred fills
    model_sl_pct: Optional[float] = None   # FLAG-Trader SL head output — preserved for deferred fills
    reprice_count: int = 0
    last_reprice_at: Optional[datetime] = None

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
            "entry_mode": self.entry_mode,
            "reprice_count": self.reprice_count,
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

    # R-based exit system
    one_r_pct: float = 0.0          # 1R as percentage (model_sl_pct or config fallback)
    one_r_price: float = 0.0        # 1R as absolute price distance from entry
    peak_r_multiple: float = 0.0    # Highest R-multiple reached during position life
    current_r_multiple: float = 0.0 # Current R-multiple (updated each monitor cycle)
    last_trail_r: float = 0.0       # Last R-level at which trailing SL was updated
    entry_reason: str = ""           # Why position was opened (e.g. "squeeze_fire")
    entry_confidence: float = 0.0    # LLM confidence at entry time
    entry_trigger_details: str = ""  # Details about the trigger (e.g. squeeze params)

    # STAGE A forecast-mode fields (predict-and-place)
    expiry_at: Optional[datetime] = None
    k_candles: int = 0
    predicted_tp_pct: Optional[float] = None
    predicted_sl_pct: Optional[float] = None
    trade_id: Optional[str] = None
    # True when exit_reason had to be inferred via the distance-based
    # heuristic fallback because the HL fills API query returned no usable
    # result. Propagated to the FILLS event payload + sidecar for downstream
    # label-quality analysis.
    exit_reason_inferred_via_fallback: bool = False

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
            "entry_reason": self.entry_reason,
            "entry_confidence": self.entry_confidence,
            "entry_trigger_details": self.entry_trigger_details,
            "one_r_pct": self.one_r_pct,
            "one_r_price": self.one_r_price,
            "peak_r_multiple": self.peak_r_multiple,
            "current_r_multiple": self.current_r_multiple,
            "last_trail_r": self.last_trail_r,
            "trade_id": self.trade_id,
            "exit_reason_inferred_via_fallback": self.exit_reason_inferred_via_fallback,
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
    # Maker order metrics
    maker_orders_submitted: int = 0
    maker_orders_filled: int = 0
    maker_orders_repriced: int = 0
    maker_orders_timed_out: int = 0
    maker_avg_fill_time_seconds: float = 0.0
    
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
            "maker_orders_submitted": self.maker_orders_submitted,
            "maker_orders_filled": self.maker_orders_filled,
            "maker_orders_repriced": self.maker_orders_repriced,
            "maker_orders_timed_out": self.maker_orders_timed_out,
            "maker_fill_rate": (
                self.maker_orders_filled / self.maker_orders_submitted
                if self.maker_orders_submitted > 0 else 0.0
            ),
            "maker_avg_fill_time_seconds": round(self.maker_avg_fill_time_seconds, 2),
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
        config: BotConfig,
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

        # Daily trade tracker for notification stats
        self._daily_closed: List[Dict[str, Any]] = []
        self._daily_date: str = ""

        # Metrics
        self.metrics = ExecutionMetrics()
        
        # Background tasks
        self._position_monitor_task: Optional[asyncio.Task] = None

        # Stray trigger-order cleanup scheduling (Bug B fix).
        # Residual reduce-only TP/SL trigger orders from previous (pre-LLM-only)
        # deploys can fire on the exchange and close positions before our own
        # min_hold timer elapses.  We reconcile periodically from the monitor
        # loop using this interval.
        self._stray_trigger_cleanup_interval_s: float = 1800.0  # 30 min
        self._last_stray_trigger_cleanup: float = 0.0


        # Configuration shortcuts — _bot_config is a BotConfig (Pydantic).
        # All fields are guaranteed by the model schema, so no defensive
        # getattr() is needed for execution_engine / stops top-level access.
        self._exec_config: BotExecutionConfig = self._bot_config.services.execution_engine

        max_hold_h = self._bot_config.stops.max_hold_hours
        max_hold_label = "disabled" if max_hold_h <= 0 else f"{max_hold_h:.1f}h"
        self._logger.info(
            "ExecutionEngine initialized: order_type=%s, max_slippage=%.2f%%, max_hold=%s",
            self._exec_config.order_type,
            self._exec_config.max_slippage_pct,
            max_hold_label,
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

        # STAGE A: regime-change exits removed (predict-and-place execution).

        # SAFETY: Cancel orphan non-reduce-only orders from previous instances
        # before syncing positions or starting the monitor loop.
        await self._cancel_orphan_orders_on_startup()

        # Sync existing positions from exchange
        await self._sync_positions_from_exchange()

        # SAFETY (Bug B): cancel stray reduce-only trigger orders that do NOT
        # correspond to any tracked position.  These are residue from previous
        # (pre-LLM-only) deploys and can fire on the exchange, bypassing our
        # min_hold / FLAG-Trader exit authority.
        await self._cleanup_stray_trigger_orders()
        self._last_stray_trigger_cleanup = asyncio.get_event_loop().time()

        # Start background tasks AFTER initial sync completes
        self._position_monitor_task = asyncio.create_task(
            self._monitor_positions(),
            name="execution_position_monitor"
        )

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
    
    async def _cancel_orphan_orders_on_startup(self) -> None:
        """Cancel non-reduce-only orders left by previous bot instances.

        At startup, the exchange may have residual limit orders from a
        crashed/restarted instance.  If these fill during the restart gap,
        they create "ghost" positions the bot never intended to open.

        This method cancels ALL non-reduce-only open orders.  Reduce-only
        orders (TP/SL) are preserved — they protect existing positions.
        """
        try:
            open_orders = await self.client.get_open_orders()
            orphan_orders = [
                o for o in open_orders if not o.get("reduceOnly", False)
            ]
            if not orphan_orders:
                self._logger.info("Startup: no orphan orders found")
                return

            cancelled = 0
            for order in orphan_orders:
                oid = order.get("orderId")
                if oid is None:
                    continue
                symbol = order.get("symbol", "?")
                side = order.get("side", "?")
                price = order.get("price", 0)
                try:
                    await self.client.cancel_order(symbol, int(oid))
                    cancelled += 1
                    self._logger.warning(
                        "Startup: cancelled orphan order %s (%s %s @ %.4f)",
                        oid, side, symbol, price,
                    )
                except Exception as e:
                    self._logger.error(
                        "Startup: failed to cancel orphan order %s for %s: %s",
                        oid, symbol, e,
                    )

            self._logger.info(
                "Startup orphan cleanup: cancelled %d/%d non-reduce-only orders",
                cancelled, len(orphan_orders),
            )
            if cancelled > 0:
                await self._send_alert(
                    f"Startup: cancelled {cancelled} orphan orders from previous instance"
                )

        except Exception as e:
            self._logger.error("Startup orphan order cleanup failed: %s", e)

    async def _cleanup_stray_trigger_orders(self) -> None:
        """Cancel reduce-only TP/SL trigger orders left behind by previous deploys.

        Bug B root cause: a pre-LLM-only deploy left reduce-only trigger orders
        (Stop Market / Take Profit Market) on the exchange.  In LLM-only exit
        mode the bot does NOT place protective TP/SL, so when such a residual
        trigger fires, the position is closed on-exchange before our min_hold
        timer elapses.  ``_check_position_sync`` then reactively cleans up,
        but the close itself was unintended.

        This method:
        - Fetches current open orders via ``client.get_open_orders()``.
        - Selects only ``reduceOnly=True`` orders whose ``orderType`` looks
          like a trigger order (Stop / Take Profit / Trigger / TP / SL).
        - For each, checks whether the symbol has a tracked active position
          AND the order id matches the tracked ``tp_order_id`` / ``sl_order_id``.
          If YES → leave it alone (tracked, legitimate).
          If NO  → cancel it (stray).
        - Skips symbols in ``_settling_symbols`` / ``_closing_positions`` to
          avoid racing a partial sync state.
        - Never touches non-reduce-only orders (handled by the orphan cleanup).

        Idempotent and defensive: any failure is logged but never raised.
        """
        try:
            open_orders = await self.client.get_open_orders()
        except Exception as e:
            self._logger.error("Stray trigger cleanup: get_open_orders failed: %s", e)
            return

        if not open_orders:
            self._logger.debug("Stray trigger cleanup: no open orders")
            return

        trigger_markers = ("stop", "take profit", "trigger", "tp", "sl")

        def _is_trigger(order: Dict[str, Any]) -> bool:
            if not order.get("reduceOnly", False):
                return False
            otype = str(order.get("orderType", "")).lower()
            if not otype:
                return False
            return any(m in otype for m in trigger_markers)

        candidates = [o for o in open_orders if _is_trigger(o)]
        if not candidates:
            self._logger.debug("Stray trigger cleanup: no reduce-only trigger orders")
            return

        cancelled = 0
        skipped_tracked = 0
        skipped_settling = 0

        for order in candidates:
            symbol = order.get("symbol")
            oid = order.get("orderId")
            if symbol is None or oid is None:
                continue

            # Defensive: never touch a symbol mid-open/close/rejection.
            if symbol in self._settling_symbols or symbol in self._closing_positions:
                skipped_settling += 1
                continue

            # Tracked?  Compare as strings because ExecutionPosition stores
            # tp_order_id/sl_order_id as Optional[str] while the SDK returns
            # order ids as int.
            tracked_position = self.active_positions.get(symbol)
            if tracked_position is not None:
                oid_str = str(oid)
                tracked_ids = {
                    str(tracked_position.tp_order_id) if tracked_position.tp_order_id else None,
                    str(tracked_position.sl_order_id) if tracked_position.sl_order_id else None,
                }
                tracked_ids.discard(None)
                if oid_str in tracked_ids:
                    skipped_tracked += 1
                    continue
                # Symbol IS tracked but this trigger id is unknown.  Duplicate
                # cleanup is handled by _ensure_tp_sl_for_position — do NOT
                # double-cancel here to avoid racing that reconciler.
                skipped_tracked += 1
                continue

            # Not tracked → cancel.
            side = order.get("side", "?")
            trigger_price = order.get("triggerPx", order.get("price", 0))
            otype = order.get("orderType", "?")
            try:
                await self.client.cancel_order(symbol, int(oid))
                cancelled += 1
                self._logger.warning(
                    "Stray trigger cleanup: cancelled %s order %s "
                    "(symbol=%s side=%s trigger_price=%s) — no tracked position",
                    otype, oid, symbol, side, trigger_price,
                )
            except Exception as e:
                self._logger.error(
                    "Stray trigger cleanup: failed to cancel order %s for %s: %s",
                    oid, symbol, e,
                )

        self._logger.info(
            "Stray trigger cleanup: examined=%d cancelled=%d skipped_tracked=%d skipped_settling=%d",
            len(candidates), cancelled, skipped_tracked, skipped_settling,
        )
        if cancelled > 0:
            try:
                await self._send_alert(
                    f"Cancelled {cancelled} stray reduce-only trigger orders "
                    "(no tracked position)"
                )
            except Exception:
                pass

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

                # Use model-predicted TP/SL (FLAG-Trader heads), fall back to config
                model_sl = signal.get("model_sl_pct", 0)
                model_tp = signal.get("model_tp_pct", 0)
                sl_pct = (model_sl if model_sl and model_sl > 0 else self._bot_config.risk.stop_loss_pct) / 100
                tp_pct = (model_tp if model_tp and model_tp > 0 else self._bot_config.risk.take_profit_pct) / 100

                # Only compute TP/SL prices if percentages are non-zero
                if sl_pct > 0 or tp_pct > 0:
                    sl_price = entry_price * (1 - sl_pct) if is_long else entry_price * (1 + sl_pct)
                    tp_price = entry_price * (1 + tp_pct) if is_long else entry_price * (1 - tp_pct)
                    enriched_signal["tp_price"] = round(tp_price, 6)
                    enriched_signal["sl_price"] = round(sl_price, 6)
                else:
                    enriched_signal["tp_price"] = None  # LLM-only exit mode
                    enriched_signal["sl_price"] = None

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
                    if order_result.order_id is None:
                        self._logger.warning(
                            "Pending order returned with no order_id; cannot track (signal=%s)",
                            signal_id[:8],
                        )
                    else:
                        order_result.original_signal = signal
                        order_result.model_tp_pct = signal.get("model_tp_pct")
                        order_result.model_sl_pct = signal.get("model_sl_pct")
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
            "ORDER SENT | cid=%s | %s %s | %.4f %s @ %s | strategy=%s",
            signal.get("correlation_id") or "-",
            order_type.upper(),
            side.upper(),
            size,
            symbol,
            f"{entry_price:.2f}" if order_type == "limit" else "MARKET",
            strategy,
        )

        # Pre-flight spread check — skip if bid-ask spread > max_spread_pct
        # to avoid the open→slippage-reject→close cycle that burns double fees.
        max_spread = self._exec_config.max_spread_pct
        try:
            spread = await self._get_spread(symbol)
            if spread > max_spread:
                self._logger.warning(
                    "SKIPPING %s %s: spread %.4f%% exceeds max_spread_pct %.2f%% — avoiding fee burn",
                    side.upper(), symbol, spread, max_spread,
                )
                self.metrics.orders_rejected += 1
                return None
        except Exception as e:
            self._logger.warning("Pre-flight spread check failed for %s: %s", symbol, e)

        # Set leverage: prefer dynamic per-signal value, fall back to config
        signal_leverage = signal.get("leverage_used")
        configured_leverage = getattr(self._bot_config.risk, "leverage", None)
        leverage = int(signal_leverage) if signal_leverage else (int(configured_leverage) if configured_leverage else None)
        if leverage:
            try:
                await self.client.update_leverage(symbol, leverage)
                self._logger.debug("Leverage set to %dx for %s", leverage, symbol)
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
        elif self._exec_config.entry_mode == "maker" and not order.reduce_only:
            # Maker (post-only) entry order
            result = await self._place_maker_order(order, is_buy)
        else:
            # Taker limit order (default — crosses spread for immediate fill)
            result = await self._place_taker_fallback(order, is_buy)

        return result

    async def _place_maker_order(self, order: Order, is_buy: bool) -> Dict[str, Any]:
        """Place a post-only (maker) order at the best bid/ask.

        Uses ``time_in_force="Alo"`` which guarantees maker execution or
        rejection.  Falls back to taker if the orderbook is empty or the
        ALO order is rejected (would cross the spread).
        """
        try:
            orderbook = await self.client.get_orderbook(order.symbol, depth=1)
        except Exception as e:
            self._logger.warning(
                "MAKER: Failed to fetch orderbook for %s: %s — falling back to taker",
                order.symbol, e,
            )
            return await self._place_taker_fallback(order, is_buy)

        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])

        if not bids or not asks:
            self._logger.warning(
                "MAKER: Empty orderbook for %s — falling back to taker",
                order.symbol,
            )
            return await self._place_taker_fallback(order, is_buy)

        maker_price = bids[0][0] if is_buy else asks[0][0]

        order.entry_mode = "maker"
        order.price = maker_price
        self.metrics.maker_orders_submitted += 1

        self._logger.info(
            "MAKER: Placing %s post-only order for %s: %.4f @ %.6f (best %s)",
            "BUY" if is_buy else "SELL",
            order.symbol,
            order.size,
            maker_price,
            "bid" if is_buy else "ask",
        )

        try:
            result = await self.client.place_order(
                symbol=order.symbol,
                is_buy=is_buy,
                size=order.size,
                price=maker_price,
                order_type="limit",
                reduce_only=False,
                time_in_force="Alo",
            )
        except OrderRejectedError:
            self._logger.warning(
                "MAKER: ALO rejected for %s (would cross spread) — falling back to taker",
                order.symbol,
            )
            return await self._place_taker_fallback(order, is_buy)

        if not result.get("success") and not result.get("orderId"):
            self._logger.warning(
                "MAKER: ALO failed for %s — falling back to taker",
                order.symbol,
            )
            return await self._place_taker_fallback(order, is_buy)

        return result

    async def _place_taker_fallback(self, order: Order, is_buy: bool) -> Dict[str, Any]:
        """Place a taker limit order that crosses the spread for immediate fill.

        This is the original limit-order behaviour extracted into a reusable
        method so that maker orders can fall back to it.
        """
        price = order.price
        if price:
            price *= 1.001 if is_buy else 0.999  # 0.1% offset to cross spread
        order.entry_mode = "taker"
        return await self.client.place_order(
            symbol=order.symbol,
            is_buy=is_buy,
            size=order.size,
            price=price,
            order_type="limit",
            reduce_only=order.reduce_only,
            time_in_force="Gtc",
        )

    async def _reprice_maker_orders(self) -> None:
        """Reprice pending maker orders when the best bid/ask has moved.

        Called every monitor loop cycle (5 s).  For each pending maker
        order whose reprice interval has elapsed, fetches the current
        orderbook and re-posts the order at the new best price if it
        has moved by more than 0.01%.
        """
        if not self.pending_orders:
            return

        now = datetime.now(timezone.utc)
        reprice_interval = timedelta(
            seconds=self._exec_config.maker_reprice_interval_seconds
        )
        max_reprices = self._exec_config.maker_max_reprices

        for order_id, order in list(self.pending_orders.items()):
            if order.entry_mode != "maker":
                continue

            # Check reprice count limit
            if order.reprice_count >= max_reprices:
                continue

            # Check interval (first reprice uses submitted_at)
            last_action = order.last_reprice_at or order.submitted_at
            if last_action and (now - last_action) < reprice_interval:
                continue

            is_buy = order.side == "buy"

            try:
                orderbook = await self.client.get_orderbook(order.symbol, depth=1)
            except Exception as e:
                self._logger.warning(
                    "MAKER REPRICE: Failed to fetch orderbook for %s: %s",
                    order.symbol, e,
                )
                continue

            bids = orderbook.get("bids", [])
            asks = orderbook.get("asks", [])
            if not bids or not asks:
                continue

            new_price = bids[0][0] if is_buy else asks[0][0]

            # Only reprice if price moved > 0.01%
            if order.price and abs(new_price - order.price) / order.price < 0.0001:
                continue

            self._logger.info(
                "MAKER REPRICE: %s %s price moved %.6f -> %.6f (reprice %d/%d)",
                order.symbol,
                "BUY" if is_buy else "SELL",
                order.price,
                new_price,
                order.reprice_count + 1,
                max_reprices,
            )

            try:
                await self.client.cancel_order(order.symbol, int(order_id))
            except Exception as e:
                self._logger.warning(
                    "MAKER REPRICE: Failed to cancel order %s: %s", order_id, e
                )
                continue

            try:
                result = await self.client.place_order(
                    symbol=order.symbol,
                    is_buy=is_buy,
                    size=order.size,
                    price=new_price,
                    order_type="limit",
                    reduce_only=False,
                    time_in_force="Alo",
                )
            except OrderRejectedError:
                self._logger.warning(
                    "MAKER REPRICE: ALO rejected for %s — falling back to taker",
                    order.symbol,
                )
                self.pending_orders.pop(order_id, None)
                try:
                    await self._place_taker_fallback(order, is_buy)
                except Exception as e:
                    self._logger.error(
                        "MAKER REPRICE: Taker fallback also failed for %s: %s",
                        order.symbol, e,
                    )
                continue
            except Exception as e:
                self._logger.error(
                    "MAKER REPRICE: Failed to re-place order for %s: %s",
                    order.symbol, e,
                )
                self.pending_orders.pop(order_id, None)
                continue

            new_order_id = str(result.get("orderId", ""))
            if not new_order_id or not result.get("success"):
                self._logger.warning(
                    "MAKER REPRICE: ALO rejected for %s after reprice — "
                    "falling back to taker",
                    order.symbol,
                )
                self.pending_orders.pop(order_id, None)
                try:
                    await self._place_taker_fallback(order, is_buy)
                except Exception as e:
                    self._logger.error(
                        "MAKER REPRICE: Taker fallback also failed for %s: %s",
                        order.symbol, e,
                    )
                continue

            # Update tracking: remove old, add new order ID
            order.price = new_price
            order.reprice_count += 1
            order.last_reprice_at = now
            self.metrics.maker_orders_repriced += 1

            self.pending_orders.pop(order_id, None)
            self.pending_orders[new_order_id] = order

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

        # Calculate 1R (risk unit) from model-predicted SL or config fallback
        model_sl = signal.get("model_sl_pct", 0)
        cfg_sl = getattr(self._bot_config.risk, "stop_loss_pct", 0)
        one_r_pct = float(model_sl if model_sl and model_sl > 0 else cfg_sl)
        one_r_price = entry_price * (one_r_pct / 100) if one_r_pct > 0 else 0.0

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
            entry_reason=signal.get("entry_reason", ""),
            entry_confidence=float(signal.get("entry_confidence", 0.0)),
            entry_trigger_details=signal.get("entry_trigger_details", ""),
            one_r_pct=one_r_pct,
            one_r_price=one_r_price,
            predicted_tp_pct=signal.get("model_tp_pct"),
            predicted_sl_pct=signal.get("model_sl_pct"),
            trade_id=signal.get("trade_id"),
        )

        # STAGE A: derive expiry_at from forecast config when not provided.
        # The TradeIntent pydantic model does not yet carry expiry_at, so we
        # compute it here at fill time.
        forecast_cfg = getattr(self._bot_config, "forecast", None)
        k_candles_cfg = int(getattr(forecast_cfg, "k_candles", 34) or 34)
        candle_period_min = int(getattr(forecast_cfg, "candle_period_minutes", 15) or 15)
        signal_expiry = signal.get("expiry_at")
        if signal_expiry is not None:
            position.expiry_at = (
                signal_expiry if isinstance(signal_expiry, datetime)
                else datetime.fromisoformat(str(signal_expiry))
            )
        else:
            position.expiry_at = datetime.now(timezone.utc) + timedelta(
                minutes=k_candles_cfg * candle_period_min
            )
        position.k_candles = int(signal.get("k_candles") or k_candles_cfg)
        
        self.active_positions[symbol] = position
        self.metrics.positions_opened += 1

        self._logger.info(
            "FILL RECEIVED | cid=%s | %s %s | %.4f @ %.4f | strategy=%s",
            signal.get("correlation_id") or "-",
            direction.upper(),
            symbol,
            float(size),
            float(entry_price),
            signal.get("strategy"),
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
            "entry_reason": signal.get("entry_reason", ""),
            "entry_confidence": float(signal.get("entry_confidence", 0.0)),
            "entry_trigger_details": signal.get("entry_trigger_details", ""),
            "correlation_id": signal.get("correlation_id"),
        })

        # Set TP/SL orders
        await self._set_tp_sl(signal, order, position)

        # Record the size for which TP/SL were placed (for partial fill detection)
        self._tp_sl_placed_size[symbol] = float(size)

        # STAGE A: write open-trade forecast sidecar so the dashboard can
        # overlay predicted TP/SL for in-flight positions. Best-effort; a
        # failure here must not break the trading flow.
        try:
            tp_pct = signal.get("model_tp_pct")
            sl_pct = signal.get("model_sl_pct")
            tp_price_hint: Optional[float] = None
            sl_price_hint: Optional[float] = None
            if position.tp_price is not None:
                tp_price_hint = float(position.tp_price)
            elif tp_pct:
                sign = 1 if direction == "long" else -1
                tp_price_hint = float(entry_price) * (1 + sign * float(tp_pct) / 100)
            if position.sl_price is not None:
                sl_price_hint = float(position.sl_price)
            elif sl_pct:
                sign = -1 if direction == "long" else 1
                sl_price_hint = float(entry_price) * (1 + sign * float(sl_pct) / 100)
            write_open_sidecar(
                trade_id=str(position.trade_id) if position.trade_id else "",
                symbol=symbol,
                side=direction,
                entry_price=float(entry_price),
                predicted_tp_price=tp_price_hint,
                predicted_sl_price=sl_price_hint,
                predicted_tp_pct=float(tp_pct) if tp_pct else None,
                predicted_sl_pct=float(sl_pct) if sl_pct else None,
                opened_at=position.opened_at,
                expiry_at=position.expiry_at,
            )
        except Exception:
            self._logger.exception("failed to write open forecast sidecar for %s", symbol)

    async def _ensure_tp_sl_for_position(self, position: ExecutionPosition) -> None:
        """
        Ensure a position has exactly 1 TP and 1 SL order on the exchange.

        Called for positions discovered on exchange that may not have protection.
        If duplicates are found, cancels the extras before confirming.
        Uses default percentages from config when no orders exist.

        Args:
            position: Position to protect
        """
        # LLM-only exit mode: skip TP/SL enforcement
        cfg_sl = getattr(self._bot_config.risk, "stop_loss_pct", 0)
        cfg_tp = getattr(self._bot_config.risk, "take_profit_pct", 0)
        if cfg_sl == 0 and cfg_tp == 0:
            return

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
            reduce_only_orders = [o for o in symbol_orders if o.get("reduceOnly")]

            if len(reduce_only_orders) >= 2:
                # Reconcile: keep exactly 1 TP + 1 SL, cancel duplicates
                tp_orders, sl_orders = self._classify_tp_sl_orders(
                    reduce_only_orders, position
                )

                # Cancel duplicate TPs (keep the one closest to config TP%)
                tp_pct = self._bot_config.risk.take_profit_pct / 100
                expected_tp = entry_price * (1 - tp_pct) if not is_long else entry_price * (1 + tp_pct)
                tp_orders.sort(key=lambda o: abs(float(o.get("price", 0)) - expected_tp))
                for dup in tp_orders[1:]:
                    await self._cancel_duplicate_order(dup, symbol, "TP")

                # Cancel duplicate SLs (keep the one closest to config SL%)
                sl_pct = self._bot_config.risk.stop_loss_pct / 100
                expected_sl = entry_price * (1 + sl_pct) if not is_long else entry_price * (1 - sl_pct)
                sl_orders.sort(key=lambda o: abs(float(o.get("price", 0)) - expected_sl))
                for dup in sl_orders[1:]:
                    await self._cancel_duplicate_order(dup, symbol, "SL")

                # Recover prices from the kept orders
                kept_tp = tp_orders[0] if tp_orders else None
                kept_sl = sl_orders[0] if sl_orders else None
                if kept_tp:
                    position.tp_price = float(kept_tp.get("price", 0))
                    position.tp_order_id = str(kept_tp.get("orderId", ""))
                if kept_sl:
                    position.sl_price = float(kept_sl.get("price", 0))
                    position.sl_order_id = str(kept_sl.get("orderId", ""))

                total_cancelled = max(0, len(tp_orders) - 1) + max(0, len(sl_orders) - 1)
                if total_cancelled > 0:
                    self._logger.warning(
                        "%s: reconciled TP/SL — cancelled %d duplicate orders, "
                        "kept TP=%.6f SL=%.6f",
                        symbol, total_cancelled,
                        position.tp_price or 0, position.sl_price or 0,
                    )
                    await self._send_alert(
                        f"TP/SL reconciled for {symbol}: cancelled {total_cancelled} duplicates"
                    )
                else:
                    self._logger.info(
                        "%s has 2 reduce-only orders — recovered TP=%.6f SL=%.6f",
                        symbol, position.tp_price or 0, position.sl_price or 0,
                    )

                if kept_tp and kept_sl:
                    self._tp_sl_confirmed.add(symbol)
                    return

            elif len(reduce_only_orders) == 1:
                # Recover the single order's price and place ONLY the missing
                # side (TP or SL). Previously this branch fell through to the
                # fresh-place code below, which re-placed BOTH orders and
                # silently duplicated whichever side already existed on the
                # exchange. See PR fix/duplicate-orders-fall-through.
                single = reduce_only_orders[0]
                single_price = float(single.get("limitPx", single.get("price", 0)))
                tp_orders, sl_orders = self._classify_tp_sl_orders(
                    reduce_only_orders, position
                )

                # Default percentages — both > 0 here because the function is
                # gated on `tp_sl_enforced` (see top of method).
                tp_pct = self._bot_config.risk.take_profit_pct / 100
                sl_pct = self._bot_config.risk.stop_loss_pct / 100

                missing_side: str = ""  # "tp" or "sl"
                missing_price: float = 0.0
                paired_tp: float = 0.0
                paired_sl: float = 0.0
                if tp_orders:
                    # Existing order is TP -> need to place SL
                    position.tp_price = single_price
                    position.tp_order_id = str(single.get("orderId", ""))
                    self._logger.info(
                        "%s: recovered single TP order price=%.6f, will place missing SL",
                        symbol, single_price,
                    )
                    missing_side = "sl"
                    missing_price = (
                        entry_price * (1 - sl_pct) if is_long
                        else entry_price * (1 + sl_pct)
                    )
                    paired_tp = single_price
                    paired_sl = missing_price
                elif sl_orders:
                    # Existing order is SL -> need to place TP
                    position.sl_price = single_price
                    position.sl_order_id = str(single.get("orderId", ""))
                    self._logger.info(
                        "%s: recovered single SL order price=%.6f, will place missing TP",
                        symbol, single_price,
                    )
                    missing_side = "tp"
                    missing_price = (
                        entry_price * (1 + tp_pct) if is_long
                        else entry_price * (1 - tp_pct)
                    )
                    paired_tp = missing_price
                    paired_sl = single_price
                else:
                    # _classify_tp_sl_orders returned both empty — defensive
                    # fallback. Treat as "no orders" and let the fresh-place
                    # code below run (this matches pre-fix behaviour for an
                    # edge case that should not occur in practice).
                    self._logger.warning(
                        "%s: single reduce-only order could not be classified "
                        "as TP or SL — falling through to fresh placement",
                        symbol,
                    )
                    missing_side = ""

                if missing_side:
                    # Validate the *paired* TP/SL distances before placing.
                    if not self._validate_stop_distance(
                        entry_price, paired_tp, paired_sl, symbol
                    ):
                        self._logger.error(
                            "%s: missing %s distance invalid after rounding — "
                            "leaving lone existing order in place, NOT duplicating",
                            symbol, missing_side.upper(),
                        )
                        return

                    try:
                        if missing_side == "tp":
                            result = await self._place_trigger_with_retry(
                                symbol=symbol,
                                is_buy=not is_long,
                                size=size,
                                trigger_price=missing_price,
                                tpsl="tp",
                                limit_price=missing_price,
                            )
                            position.tp_order_id = result.get("orderId")
                            position.tp_price = missing_price
                            self._logger.info(
                                "%s: placed missing TP @ %.4f (%s)",
                                symbol, missing_price, position.tp_order_id,
                            )
                        else:
                            result = await self._place_trigger_with_retry(
                                symbol=symbol,
                                is_buy=not is_long,
                                size=size,
                                trigger_price=missing_price,
                                tpsl="sl",
                            )
                            position.sl_order_id = result.get("orderId")
                            position.sl_price = missing_price
                            self._logger.info(
                                "%s: placed missing SL @ %.4f (%s)",
                                symbol, missing_price, position.sl_order_id,
                            )
                        self._tp_sl_confirmed.add(symbol)
                        self._tp_sl_placed_size[symbol] = float(size)
                    except Exception as e:
                        self._logger.error(
                            "%s: failed to place missing %s after retries: %s — "
                            "NOT falling through to avoid duplicating existing order",
                            symbol, missing_side.upper(), e,
                        )
                    # IMPORTANT: return regardless of success to prevent the
                    # fresh-place code below from duplicating the existing order.
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

        # Place TP order with retry (limit order for maker fee savings)
        try:
            tp_result = await self._place_trigger_with_retry(
                symbol=symbol,
                is_buy=not is_long,
                size=size,
                trigger_price=tp_price,
                tpsl="tp",
                limit_price=tp_price,
            )
            position.tp_order_id = tp_result.get("orderId")
            position.tp_price = tp_price
            self._logger.info("TP limit trigger placed for %s: %s @ %.4f (maker fee)", symbol, position.tp_order_id, tp_price)
        except Exception as e:
            self._logger.error("Failed to place TP trigger order for %s after retries: %s", symbol, e)

        # Place SL order with retry (market order for guaranteed execution)
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

    def _classify_tp_sl_orders(
        self,
        reduce_only_orders: List[Dict],
        position: ExecutionPosition,
    ) -> tuple:
        """Classify reduce-only orders into TP and SL lists.

        For a **long** position: orders with price > entry are TP, < entry are SL.
        For a **short** position: orders with price < entry are TP, > entry are SL.

        Returns:
            Tuple of (tp_orders, sl_orders)
        """
        entry = position.entry_price
        is_long = position.side == "long"
        tp_orders: List[Dict] = []
        sl_orders: List[Dict] = []

        for o in reduce_only_orders:
            price = float(o.get("limitPx", o.get("price", 0)))
            if is_long:
                if price >= entry:
                    tp_orders.append(o)
                else:
                    sl_orders.append(o)
            else:
                if price <= entry:
                    tp_orders.append(o)
                else:
                    sl_orders.append(o)

        return tp_orders, sl_orders

    async def _cancel_duplicate_order(
        self, order: Dict, symbol: str, label: str
    ) -> None:
        """Cancel a single duplicate reduce-only order."""
        oid = order.get("orderId")
        if oid is None:
            return
        price = float(order.get("price", order.get("limitPx", 0)))
        try:
            await self.client.cancel_order(symbol, int(oid))
            self._logger.warning(
                "Cancelled duplicate %s order %s for %s @ %.6f",
                label, oid, symbol, price,
            )
        except Exception as e:
            self._logger.error(
                "Failed to cancel duplicate %s order %s for %s: %s",
                label, oid, symbol, e,
            )

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
        limit_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Place a trigger order with retry and exponential backoff.

        Args:
            limit_price: If set, the trigger order executes as a limit order
                at this price (maker fee 0.02%) instead of market (taker 0.05%).
                Used for TP orders to save on fees.
        """
        for attempt in range(max_retries):
            try:
                result = await self.client.place_trigger_order(
                    symbol=symbol,
                    is_buy=is_buy,
                    size=size,
                    trigger_price=trigger_price,
                    limit_price=limit_price,
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
        # Unreachable: loop either returns or raises. Kept to satisfy type checker.
        raise RuntimeError("_place_trigger_with_retry exited without result")

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

        When stop_loss_pct and take_profit_pct are both 0, TP/SL placement is
        skipped entirely — exit management is delegated to the LLM eval loop.

        TP/SL are always calculated from config fixed percentages (stop_loss_pct,
        take_profit_pct). These values are data-driven from parameter sweep.
        ATR-adaptive branch was removed: it produced TP=4-8% and SL=0.5-2%,
        far too wide for a 15m momentum scalper.

        Both TP and SL are always calculated from the actual fill price.
        Model-predicted TP/SL (from FLAG-Trader heads) take priority.
        The LLM 60s eval loop runs in parallel and can close before TP/SL hit.

        ATR data (entry_atr_pct) is still stored on the position for the
        trailing stop system, which uses it independently.

        Args:
            signal: Original signal with TP/SL levels
            order: Filled entry order
            position: ExecutionPosition to protect
        """
        # Use model-predicted TP/SL (from FLAG-Trader heads)
        model_sl = signal.get("model_sl_pct", 0)
        model_tp = signal.get("model_tp_pct", 0)
        cfg_sl = getattr(self._bot_config.risk, "stop_loss_pct", 0)
        cfg_tp = getattr(self._bot_config.risk, "take_profit_pct", 0)

        sl_pct_raw = model_sl if model_sl and model_sl > 0 else cfg_sl
        tp_pct_raw = model_tp if model_tp and model_tp > 0 else cfg_tp

        if sl_pct_raw == 0 and tp_pct_raw == 0:
            self._logger.info(
                "TP/SL SKIPPED for %s: no model predictions and config values are 0",
                signal["symbol"],
            )
            return

        symbol = signal["symbol"]
        is_long = signal["direction"] == "long"
        entry_price = order.avg_price or signal["entry_price"]
        size = position.size

        # --- Cancel existing reduce-only orders to avoid orphans on partial fills ---
        try:
            open_orders = await self.client.get_open_orders()
            existing_reduce = [
                o for o in open_orders
                if o.get("symbol") == symbol and o.get("reduceOnly")
            ]
            for ro in existing_reduce:
                try:
                    await self.client.cancel_order(symbol, int(ro["orderId"]))
                    self._logger.info(
                        "Cancelled existing reduce-only order %s for %s before new TP/SL",
                        ro["orderId"], symbol,
                    )
                except Exception as e:
                    self._logger.warning(
                        "Failed to cancel reduce-only order %s for %s: %s",
                        ro["orderId"], symbol, e,
                    )
        except Exception as e:
            self._logger.warning("Could not fetch open orders to clean up %s: %s", symbol, e)

        sl_pct = sl_pct_raw / 100
        tp_pct = tp_pct_raw / 100

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

        # Place TP order with retry (limit order for maker fee savings)
        try:
            tp_result = await self._place_trigger_with_retry(
                symbol=symbol,
                is_buy=not is_long,
                size=size,
                trigger_price=tp_price,
                tpsl="tp",
                limit_price=tp_price,
            )
            position.tp_order_id = tp_result.get("orderId")
            position.tp_price = tp_price
            self._logger.info("TP limit trigger placed: %s @ %.4f (maker fee)", position.tp_order_id, tp_price)

        except Exception as e:
            self._logger.error("Failed to place TP trigger order after retries: %s", e)
            await self._send_alert(
                f"⚠️ CRITICAL: TP placement failed for {symbol} "
                f"{'LONG' if is_long else 'SHORT'} @ {entry_price:.4f}. "
                f"Position has NO take-profit protection! Error: {e}"
            )

        # Place SL order with retry (market order for guaranteed execution)
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
            await self._send_alert(
                f"⚠️ CRITICAL: SL placement failed for {symbol} "
                f"{'LONG' if is_long else 'SHORT'} @ {entry_price:.4f}. "
                f"Position has NO stop-loss protection! Error: {e}"
            )
    
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

        # 3) Re-place TP at the same price, new size (limit order for maker fee)
        if position.tp_price:
            try:
                tp_result = await self._place_trigger_with_retry(
                    symbol=symbol,
                    is_buy=not is_long,
                    size=new_size,
                    trigger_price=position.tp_price,
                    tpsl="tp",
                    limit_price=position.tp_price,
                )
                position.tp_order_id = tp_result.get("orderId")
                self._logger.info(
                    "TP limit re-placed for %s: size %.4f @ %.4f (order %s, maker fee)",
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
                if order.entry_mode == "maker":
                    self.metrics.maker_orders_timed_out += 1
                self._logger.info(
                    "Stale %s order %s cancelled successfully",
                    order.entry_mode.upper(), order_id,
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
                        "model_tp_pct": order.model_tp_pct,
                        "model_sl_pct": order.model_sl_pct,
                    }
                    await self._handle_order_filled(signal, order)
                    self.pending_orders.pop(order_id, None)
                    self.metrics.orders_filled += 1
                    if order.entry_mode == "maker":
                        self.metrics.maker_orders_filled += 1
                        if order.submitted_at:
                            fill_secs = (now - order.submitted_at).total_seconds()
                            prev = self.metrics.maker_avg_fill_time_seconds
                            n = self.metrics.maker_orders_filled
                            self.metrics.maker_avg_fill_time_seconds = (
                                prev * (n - 1) + fill_secs
                            ) / n
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
        """STAGE A: predict-and-place monitor loop.

        Only three exit checks remain:
          1. TP filled  (detected via _check_closed_positions / sync)
          2. SL filled  (detected via _check_closed_positions / sync)
          3. now >= expiry_at -> close_position(reason="expiry")

        All R-based, breakeven, trailing, ROI, momentum-fade and regime-exit
        logic has been disabled. Maker reprice + stale-order cleanup are
        retained because they belong to the entry-side execution path.
        """
        self._logger.info("Position monitoring started (STAGE A: predict-and-place)")
        while True:
            try:
                # Entry-side housekeeping (not exit logic).
                await self._poll_pending_order_fills()
                await self._reprice_maker_orders()
                await self._cancel_stale_orders()

                # Exit detection — TP/SL fills.
                await self._sync_positions_from_exchange()
                await self._check_closed_positions()

                # Hard expiry: K-candle timeout.
                await self._check_expiry_exits()

                # Periodic stray reduce-only trigger cleanup (defensive).
                now_mono = asyncio.get_event_loop().time()
                if (now_mono - self._last_stray_trigger_cleanup
                        >= self._stray_trigger_cleanup_interval_s):
                    await self._cleanup_stray_trigger_orders()
                    self._last_stray_trigger_cleanup = now_mono

                await asyncio.sleep(5)
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

            # LLM-only exit mode: when both SL% and TP% are 0, the FLAG-Trader
            # agent is the sole exit authority and no protective TP/SL orders are
            # placed on the exchange. In that mode, the "missing TP/SL protection"
            # warning is misleading log spam (and the re-attempt call is a no-op).
            # Gate both on `tp_sl_enforced` so operators only see warnings when
            # TP/SL placement is actually expected. See incident FET 2026-04-06.
            cfg_sl = getattr(self._bot_config.risk, "stop_loss_pct", 0)
            cfg_tp = getattr(self._bot_config.risk, "take_profit_pct", 0)
            tp_sl_enforced = not (cfg_sl == 0 and cfg_tp == 0)

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

                    # Re-attempt TP/SL if initial placement failed
                    if symbol in self._tp_sl_confirmed:
                        pass  # Already confirmed via exchange orders, skip re-check
                    elif (
                        tp_sl_enforced
                        and (local_pos.tp_price is None or local_pos.sl_price is None)
                        and local_pos.status == PositionStatus.OPEN
                        and symbol not in self._settling_symbols
                    ):
                        self._logger.warning(
                            "Position %s missing TP/SL protection (tp=%s, sl=%s), re-attempting...",
                            symbol, local_pos.tp_price, local_pos.sl_price,
                        )
                        await self._ensure_tp_sl_for_position(local_pos)

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
                    # Recover real open time from fills API, or fall back to
                    # 1 hour ago if unavailable.
                    synced_opened_at = await self._get_position_open_time(symbol)

                    # Derive expiry_at for synced positions so they respect K-candle timeout
                    forecast_cfg = getattr(self._bot_config, "forecast", None)
                    k_candles_cfg = int(getattr(forecast_cfg, "k_candles", 34) or 34)
                    candle_period_min = int(getattr(forecast_cfg, "candle_period_minutes", 15) or 15)
                    expiry_at = synced_opened_at + timedelta(minutes=k_candles_cfg * candle_period_min)

                    new_pos = ExecutionPosition(
                        symbol=symbol,
                        side=pos.get("side", "long"),
                        size=abs(size),
                        entry_price=entry_px,
                        current_price=pos.get("markPrice", 0),
                        unrealized_pnl=pos.get("unrealizedPnl", 0),
                        leverage=pos.get("leverage", 1),
                        status=PositionStatus.OPEN,
                        opened_at=synced_opened_at,
                        expiry_at=expiry_at,
                        entry_regime="trend",  # We only open in TREND
                        highest_price=entry_px,
                        lowest_price=entry_px,
                    )
                    self.active_positions[symbol] = new_pos
                    self._logger.info(
                        "Synced existing position: %s %s %.4f (opened_at set to %s — estimated, pre-restart)",
                        new_pos.side,
                        symbol,
                        abs(size),
                        synced_opened_at.isoformat(),
                    )
                    self._logger.info(
                        "Synced position %s: derived expiry_at=%s from opened_at=%s + %d candles",
                        symbol, expiry_at.isoformat(), synced_opened_at.isoformat(), k_candles_cfg,
                    )

                    # Check and set SL/TP for newly discovered positions
                    # Skip entirely in LLM-only exit mode — the call is a no-op
                    # in that configuration and would only waste CPU.
                    if tp_sl_enforced:
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
                if self.active_positions[symbol].status in (PositionStatus.OPEN, PositionStatus.CLOSING):
                    await self._handle_position_closed(symbol)

        except Exception as e:
            self._logger.error("Failed to sync positions: %s", e)
    
    async def _get_position_open_time(self, symbol: str) -> datetime:
        """Recover the real open time for a position from the fills API.

        Scans recent fills for the most recent *opening* fill on ``symbol``
        (direction contains "Open").  Falls back to ``now - 1 hour``
        if no matching fill is found — this assumes the position is recent
        rather than ancient, avoiding premature force-close of new positions.
        """
        fallback = datetime.now(timezone.utc) - timedelta(hours=1)
        try:
            fills = await self.client.get_fills(limit=2000)
            # Filter to opening fills for this symbol, oldest first
            open_fills = [
                f for f in fills
                if f.get("symbol") == symbol
                and isinstance(f.get("dir"), str)
                and "Open" in f["dir"]
            ]
            if open_fills:
                # The fills API returns newest-first; pick the most recent
                latest = max(open_fills, key=lambda f: f.get("time") or fallback)
                real_time = latest.get("time")
                if real_time:
                    real_time = _ensure_aware(real_time)
                    self._logger.info(
                        "Recovered real opened_at for %s: %s (from fill %s)",
                        symbol, real_time.isoformat(), latest.get("fillId"),
                    )
                    return real_time

            self._logger.warning(
                "No opening fill found for %s — using fallback opened_at=%s",
                symbol, fallback.isoformat(),
            )
        except Exception as e:
            self._logger.warning(
                "Failed to recover opened_at for %s: %s — using fallback",
                symbol, e,
            )
        return fallback

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

            # Resolve TP/SL prices, falling back to config-based values
            # when server-side TP/SL placement failed.
            tp_price = position.tp_price
            sl_price = position.sl_price

            if not tp_price or not sl_price:
                if not position.entry_price:
                    continue
                tp_pct = self._bot_config.risk.take_profit_pct / 100
                sl_pct = self._bot_config.risk.stop_loss_pct / 100
                # LLM-only exit mode: skip price-crossing checks when TP/SL = 0
                if tp_pct == 0 and sl_pct == 0:
                    continue
                if position.side == "long":
                    tp_price = tp_price or round(position.entry_price * (1 + tp_pct), 6)
                    sl_price = sl_price or round(position.entry_price * (1 - sl_pct), 6)
                else:
                    tp_price = tp_price or round(position.entry_price * (1 - tp_pct), 6)
                    sl_price = sl_price or round(position.entry_price * (1 + sl_pct), 6)
                self._logger.warning(
                    "%s TP/SL fallback: tp=%.6f sl=%.6f (from config, entry=%.6f)",
                    symbol, tp_price, sl_price, position.entry_price,
                )

            hit_reason: Optional[str] = None

            if position.side == "long":
                if position.current_price >= tp_price:
                    hit_reason = "take_profit"
                    self._logger.info(
                        "%s TP hit: %.2f >= %.2f",
                        symbol,
                        position.current_price,
                        tp_price,
                    )
                elif position.current_price <= sl_price:
                    hit_reason = "stop_loss"
                    self._logger.info(
                        "%s SL hit: %.2f <= %.2f",
                        symbol,
                        position.current_price,
                        sl_price,
                    )
            else:
                if position.current_price <= tp_price:
                    hit_reason = "take_profit"
                    self._logger.info(
                        "%s TP hit: %.2f <= %.2f",
                        symbol,
                        position.current_price,
                        tp_price,
                    )
                elif position.current_price >= sl_price:
                    hit_reason = "stop_loss"
                    self._logger.info(
                        "%s SL hit: %.2f >= %.2f",
                        symbol,
                        position.current_price,
                        sl_price,
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
                    # CRITICAL: Remove from _closing_positions so next iteration retries
                    self._closing_positions.discard(symbol)
                    position.status = PositionStatus.OPEN

    async def _check_expiry_exits(self) -> None:
        """STAGE A: close any open position whose K-candle expiry has elapsed."""
        forecast_cfg = getattr(self._bot_config, "forecast", None)
        offset_bps = float(getattr(forecast_cfg, "expiry_limit_offset_bps", 5))
        _timeout_sec = float(getattr(forecast_cfg, "expiry_limit_timeout_sec", 10))
        _ = (offset_bps, _timeout_sec)  # reserved for limit-then-market path

        now = datetime.now(timezone.utc)
        for symbol, position in list(self.active_positions.items()):
            if position.status not in (PositionStatus.OPEN,):
                continue
            if symbol in self._closing_positions or symbol in self._settling_symbols:
                continue
            if position.expiry_at is None:
                self._logger.warning(
                    "STALE POSITION | %s has no expiry_at — cannot enforce K-candle timeout. "
                    "Position opened_at=%s, age=%.1fh",
                    symbol,
                    position.opened_at.isoformat() if position.opened_at else "unknown",
                    (now - _ensure_aware(position.opened_at)).total_seconds() / 3600 if position.opened_at else -1,
                )
                continue
            expiry = _ensure_aware(position.expiry_at)
            if now < expiry:
                continue

            self._logger.info(
                "%s expiry hit (now=%s >= expiry_at=%s) - closing on K-candle timeout",
                symbol, now.isoformat(), expiry.isoformat(),
            )
            self._closing_positions.add(symbol)
            position.status = PositionStatus.CLOSING
            position.exit_reason = "expiry"
            try:
                await self.close_position(symbol)
            except Exception as e:
                self._logger.error("Failed to close %s on expiry: %s", symbol, e)
                self._closing_positions.discard(symbol)
                position.status = PositionStatus.OPEN

        # Health check: flag positions open longer than 2x max expiry window
        k_candles_cfg = int(getattr(forecast_cfg, "k_candles", 34) or 34)
        candle_period_min = int(getattr(forecast_cfg, "candle_period_minutes", 15) or 15)
        max_age_minutes = k_candles_cfg * candle_period_min * 2
        for symbol, position in list(self.active_positions.items()):
            if position.status != PositionStatus.OPEN:
                continue
            if not position.opened_at:
                continue
            age_minutes = (now - _ensure_aware(position.opened_at)).total_seconds() / 60
            if age_minutes > max_age_minutes:
                self._logger.error(
                    "STALE POSITION ALERT | %s open for %.1fh (max expected: %.1fh) — investigate!",
                    symbol, age_minutes / 60, max_age_minutes / 60,
                )

    @staticmethod
    async def _infer_exit_reason_from_fills(
        client: Any,
        position: ExecutionPosition,
        logger: logging.Logger,
        price_tolerance: float = 0.003,
    ) -> Optional[str]:
        """
        Infer the real exit_reason for a closed position by querying the
        Hyperliquid fills API and matching the closing fill price against
        the stored tp_price / sl_price.

        This replaces the legacy distance-to-mark heuristic which was
        unreliable whenever the polled mark rebounded toward the opposite
        trigger between the actual intrabar fill and the 5s detection cycle.

        Match logic:
          1. Fetch recent user fills (~50).
          2. Keep only fills for this coin whose ``dir`` field starts with
             "Close" (HL SDK labels: "Close Long" / "Close Short") and whose
             timestamp is >= position.opened_at. If opened_at is missing,
             fall back to the most recent close fill for the symbol.
          3. If multiple close fills match (partial fills of a single
             trigger), aggregate by volume-weighted average price.
          4. Compare the resulting fill price to tp_price and sl_price
             using relative tolerance (default 0.3%). Return the closer
             matching trigger.
          5. If no close fill is found, or both tp/sl are within tolerance
             of each other, return None so the caller can fall back to the
             legacy heuristic (flagged).

        Args:
            position: Closed ExecutionPosition with tp_price & sl_price set.
            price_tolerance: Relative tolerance for matching fill.px to
                tp_price / sl_price (fraction of entry_price).

        Returns:
            "take_profit", "stop_loss", or None if undetermined.
        """
        if not (position.tp_price and position.sl_price and position.entry_price):
            return None

        try:
            fills = await client.get_fills(limit=50)
        except Exception as e:
            logger.debug(
                "fills API error while inferring exit_reason for %s: %s",
                position.symbol, e,
            )
            return None

        if not fills:
            return None

        opened_at = position.opened_at
        close_fills: list[dict] = []
        for f in fills:
            if f.get("symbol") != position.symbol:
                continue
            dir_str = str(f.get("dir") or "")
            if not dir_str.startswith("Close"):
                continue
            ft = f.get("time")
            if opened_at is not None and isinstance(ft, datetime):
                # Normalize tz: get_fills uses naive datetime from fromtimestamp.
                ft_cmp = ft.replace(tzinfo=timezone.utc) if ft.tzinfo is None else ft
                if ft_cmp < opened_at:
                    continue
            close_fills.append(f)

        if not close_fills:
            return None

        # Aggregate: volume-weighted average price across partial fills.
        total_size = 0.0
        total_notional = 0.0
        for f in close_fills:
            px = float(f.get("price") or 0.0)
            sz = float(f.get("size") or 0.0)
            if px > 0 and sz > 0:
                total_notional += px * sz
                total_size += sz
        if total_size <= 0:
            return None
        vwap = total_notional / total_size

        entry = float(position.entry_price)
        tp = float(position.tp_price)
        sl = float(position.sl_price)
        tol_abs = entry * price_tolerance

        tp_diff = abs(vwap - tp)
        sl_diff = abs(vwap - sl)

        # Require at least one side to be within tolerance, else bail out.
        if tp_diff > tol_abs and sl_diff > tol_abs:
            logger.debug(
                "%s fills vwap=%.4f matches neither tp=%.4f nor sl=%.4f within tol=%.4f",
                position.symbol, vwap, tp, sl, tol_abs,
            )
            return None

        # If both are within tolerance (tp_price ~= sl_price — shouldn't
        # happen in practice), bail to fallback.
        if tp_diff <= tol_abs and sl_diff <= tol_abs and abs(tp_diff - sl_diff) < 1e-9:
            return None

        result = "take_profit" if tp_diff < sl_diff else "stop_loss"
        logger.info(
            "%s exit_reason=%s inferred from HL fills (vwap=%.4f, tp=%.4f, sl=%.4f, n_fills=%d)",
            position.symbol, result, vwap, tp, sl, len(close_fills),
        )
        return result

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

        # STAGE A: remove the open forecast sidecar (dashboard live overlay).
        # Best-effort — tolerate missing files and any unlink errors.
        try:
            if position.trade_id:
                delete_open_sidecar(str(position.trade_id))
        except Exception:
            self._logger.exception("failed to delete open forecast sidecar for %s", symbol)

        # Infer exit_reason from exit price when not set (exchange-side TP/SL)
        if not position.exit_reason:
            exit_px = position.current_price
            if position.tp_price and position.sl_price:
                # PRIMARY: query HL fills API and match the closing fill price
                # against stored tp_price / sl_price. This avoids the
                # distance-to-mark heuristic bug where intrabar SL triggers
                # are mislabeled as TP because the polled mark rebounded
                # toward TP by detection time (~5s polling window).
                inferred = await self._infer_exit_reason_from_fills(self.client, position, self._logger)
                if inferred is not None:
                    position.exit_reason = inferred
                else:
                    # FALLBACK: legacy distance-based heuristic. Flag it so
                    # downstream analysis can exclude these labels from
                    # accuracy-sensitive stats.
                    tp_dist = abs(exit_px - position.tp_price) / position.entry_price if position.entry_price else float("inf")
                    sl_dist = abs(exit_px - position.sl_price) / position.entry_price if position.entry_price else float("inf")
                    if tp_dist < sl_dist:
                        position.exit_reason = "take_profit"
                    else:
                        position.exit_reason = "stop_loss"
                    position.exit_reason_inferred_via_fallback = True
                    self._logger.warning(
                        "%s exit_reason inferred via distance fallback (fills API returned no match): %s (exit_px=%.4f, tp=%.4f, sl=%.4f)",
                        position.symbol, position.exit_reason, float(exit_px), float(position.tp_price), float(position.sl_price),
                    )
            elif position.tp_price:
                position.exit_reason = "take_profit"
            elif position.sl_price:
                position.exit_reason = "stop_loss"
            else:
                # Last resort: infer from config TP/SL thresholds vs close price
                try:
                    tp_pct = getattr(getattr(self._bot_config, 'risk', None), 'take_profit_pct', 2.5) / 100
                    sl_pct = getattr(getattr(self._bot_config, 'risk', None), 'stop_loss_pct', 1.0) / 100
                    is_long = position.side == "long"
                    entry = float(position.entry_price) if position.entry_price else 0
                    exit_px_f = float(position.current_price) if position.current_price else entry

                    # LLM-only mode detection: no TP/SL enforcement configured AND
                    # neither a runtime tp_price nor sl_price was ever set on the
                    # position. In that regime the fallback ternary would collapse
                    # to "stop_loss" because implied_tp == implied_sl == entry,
                    # mislabeling every winning trade. Emit a neutral
                    # "external_close" label and let downstream consumers bucket
                    # win/loss from realized PnL sign instead.
                    # LLM-only mode = risk config disables both TP and SL
                    # enforcement. We intentionally do NOT also treat "both
                    # tp_price and sl_price None" as LLM-only, because in
                    # normal mode a position whose exchange-side TP/SL
                    # orders have not yet been materialized also has those
                    # fields None, and we still want the config-threshold
                    # inference path for it.
                    llm_only_mode = (tp_pct == 0 and sl_pct == 0)

                    if llm_only_mode:
                        position.exit_reason = "external_close"
                        pnl_sign = "win" if (position.unrealized_pnl or 0) > 0 else (
                            "loss" if (position.unrealized_pnl or 0) < 0 else "flat"
                        )
                        self._logger.info(
                            "%s exit_reason=external_close (LLM-only mode, pnl_sign=%s, exit_px=%.4f, entry=%.4f)",
                            position.symbol, pnl_sign, exit_px_f, entry,
                        )
                    elif entry > 0:
                        implied_tp = entry * (1 + tp_pct) if is_long else entry * (1 - tp_pct)
                        implied_sl = entry * (1 - sl_pct) if is_long else entry * (1 + sl_pct)
                        tp_dist = abs(exit_px_f - implied_tp)
                        sl_dist = abs(exit_px_f - implied_sl)
                        if tp_dist == sl_dist:
                            # Degenerate tie — use realized PnL sign instead of
                            # defaulting to stop_loss (which mislabels winners).
                            pnl = position.unrealized_pnl or 0
                            position.exit_reason = "take_profit" if pnl > 0 else (
                                "stop_loss" if pnl < 0 else "external_close"
                            )
                        else:
                            position.exit_reason = "take_profit" if tp_dist < sl_dist else "stop_loss"
                        self._logger.info(
                            "%s exit_reason inferred from config thresholds: %s (exit_px=%.4f, implied_tp=%.4f, implied_sl=%.4f)",
                            position.symbol, position.exit_reason, exit_px_f, implied_tp, implied_sl,
                        )
                    else:
                        position.exit_reason = "unknown"
                except Exception as e:
                    self._logger.warning("Failed to infer exit_reason for %s: %s", position.symbol, e)
                    position.exit_reason = "unknown"

        # Cancel remaining TP/SL orders (the one that didn't trigger)
        for order_id in [position.tp_order_id, position.sl_order_id]:
            if order_id:
                try:
                    await self.client.cancel_order(symbol, int(order_id))
                    self._logger.info("Cancelled residual order %s for %s", order_id, symbol)
                except Exception as e:
                    self._logger.debug("Could not cancel order %s (likely already filled): %s", order_id, e)

        self.metrics.positions_closed += 1

        # Fetch real P&L and fees from exchange fills
        closed_pnl = position.unrealized_pnl  # fallback
        total_fee = 0.0
        taker_fees = 0.0
        maker_fees = 0.0
        try:
            fills = await self.client.get_fills(limit=50)
            # Find fills for this symbol (recent first) to get closedPnl and fee
            symbol_fills = [f for f in fills if f["symbol"] == symbol]
            if symbol_fills:
                # The closing fill has closedPnl != 0
                close_fills = [f for f in symbol_fills if f.get("closedPnl", 0) != 0]
                if close_fills:
                    closed_pnl = close_fills[0]["closedPnl"]
                # Sum fees by maker/taker for this symbol's recent fills (entry + exit)
                for f in symbol_fills[:4]:
                    fee = abs(f.get("fee", 0))
                    if f.get("is_taker", True):
                        taker_fees += fee
                    else:
                        maker_fees += fee
                total_fee = taker_fees + maker_fees
        except Exception as e:
            self._logger.debug("Could not fetch fills for fee tracking: %s", e)

        net_pnl = closed_pnl - total_fee

        self._logger.info(
            "Position closed: %s %s - Gross: $%.2f, Fee: $%.4f (maker: $%.4f, taker: $%.4f), Net: $%.2f [%s]",
            position.side,
            symbol,
            closed_pnl,
            total_fee,
            maker_fees,
            taker_fees,
            net_pnl,
            position.exit_reason or "unknown",
        )

        # Calculate PnL percentage for notification
        pnl_pct = 0.0
        if position.entry_price > 0:
            if position.side == "long":
                pnl_pct = ((position.current_price - position.entry_price) / position.entry_price) * 100
            else:
                pnl_pct = ((position.entry_price - position.current_price) / position.entry_price) * 100

        # Track daily closed trades (auto-reset on date change)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._daily_date != today:
            self._daily_closed.clear()
            self._daily_date = today
        self._daily_closed.append({
            "symbol": symbol,
            "pnl": net_pnl,
            "pnl_pct": pnl_pct,
            "is_win": net_pnl > 0,
        })
        daily_wins = sum(1 for t in self._daily_closed if t["is_win"])
        daily_trades = len(self._daily_closed)
        daily_pnl = sum(t["pnl"] for t in self._daily_closed)

        # Fetch current equity for notifications
        current_equity = 0.0
        try:
            state = await self.client._run_sync(
                lambda: self.client._i().user_state(self.client._addr())
            )
            current_equity = float(state.get("marginSummary", {}).get("accountValue", 0))
        except Exception:
            pass

        # Publish fill/close event with flat fields for notifications
        await self.publish(Topic.FILLS, {
            "event": "position_closed",
            "symbol": symbol,
            "side": position.side,
            "entry_price": position.entry_price,
            "exit_price": position.current_price,
            "realized_pnl": net_pnl,
            "gross_pnl": closed_pnl,
            "fee": total_fee,
            "pnl_pct": pnl_pct,
            "exit_reason_v2": _exit_reason_to_v2(position.exit_reason),
            "exit_reason_inferred_via_fallback": position.exit_reason_inferred_via_fallback,
            "trade_id": position.trade_id,
            "position": position.to_dict(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "daily_wins": daily_wins,
            "daily_trades": daily_trades,
            "daily_pnl": daily_pnl,
            "equity": current_equity,
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
    
    async def close_position(
        self,
        symbol: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Close a specific position.

        STAGE A (predict-and-place): no min_hold gate. Exit triggers are TP fill,
        SL fill, expiry, or explicit manual close — all unconditional.

        Args:
            symbol: Symbol to close

        Returns:
            Order result or None if no position.
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
    config: BotConfig,
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
