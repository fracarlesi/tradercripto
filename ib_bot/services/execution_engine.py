"""
Execution Engine Service
========================

Places bracket orders via IB and manages position lifecycle.
Entry + TP + SL as OCA group — IB cancels the other when one fills.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from ib_insync import Fill, Trade

from .base import BaseService
from .ib_client import IBClient
from .message_bus import MessageBus
from .risk_manager import RiskManager
from .kill_switch import KillSwitchService
from ..core.enums import Direction, Topic
from ..core.models import TradeIntent, Position

logger = logging.getLogger(__name__)


class ExecutionEngine(BaseService):
    """Executes trades via IB bracket orders."""

    def __init__(
        self,
        ib_client: IBClient,
        risk_manager: RiskManager,
        kill_switch: KillSwitchService,
        bus: Optional[MessageBus] = None,
    ) -> None:
        super().__init__(name="execution_engine", bus=bus, loop_interval_seconds=5.0)
        self._ib_client = ib_client
        self._risk_manager = risk_manager
        self._kill_switch = kill_switch
        self._active_trades: Dict[str, Any] = {}
        self._stopped_directions: Dict[str, set] = {}  # symbol -> {Direction.LONG, ...}
        # order_id -> status string (Submitted, Filled, Cancelled, etc.)
        self._order_statuses: Dict[int, str] = {}

    async def _on_start(self) -> None:
        if self.bus:
            await self.subscribe(Topic.ORDER, self._handle_trade_intent)

        # Register IB event callbacks
        self._ib_client.on_order_status(self._handle_order_status)
        self._ib_client.on_fill(self._handle_fill)
        self._ib_client.on_error(self._handle_ib_error)

        self._logger.info("ExecutionEngine started — IB event handlers registered")

    async def _on_stop(self) -> None:
        self._logger.info("ExecutionEngine stopped")

    async def _handle_trade_intent(self, msg: Any) -> None:
        """Handle incoming trade intent from risk manager."""
        payload = msg.payload if hasattr(msg, "payload") else msg
        intent = TradeIntent(**payload) if isinstance(payload, dict) else payload

        # Safety checks
        if not self._kill_switch.is_trading_allowed:
            self._logger.info("Trade blocked by kill switch")
            return

        if not self._risk_manager.is_trading_allowed:
            self._logger.info("Trade blocked by risk manager")
            return

        # Check no-reentry after stop
        symbol = intent.setup.symbol
        direction = intent.setup.direction
        stopped = self._stopped_directions.get(symbol, set())
        if direction in stopped:
            self._logger.info(
                "No re-entry: %s %s already stopped out today",
                direction.value, symbol,
            )
            return

        # Place bracket order
        try:
            trades = await self._ib_client.place_bracket_order(
                symbol=symbol,
                direction=direction,
                contracts=intent.contracts,
                entry_price=intent.setup.entry_price,
                stop_price=intent.setup.stop_price,
                target_price=intent.setup.target_price,
            )

            self._active_trades[symbol] = {
                "trades": trades,
                "intent": intent,
                "entry_time": datetime.now(timezone.utc),
            }

            # Publish order event
            await self.publish(Topic.ORDER, {
                "type": "bracket_placed",
                "symbol": symbol,
                "direction": direction.value,
                "contracts": intent.contracts,
                "entry": float(intent.setup.entry_price),
                "stop": float(intent.setup.stop_price),
                "target": float(intent.setup.target_price),
                "risk_usd": float(intent.risk_usd),
            })

            self._logger.info(
                "Bracket order placed: %s %s x%d",
                direction.value, symbol, intent.contracts,
            )

        except Exception as e:
            self._logger.error("Failed to place order: %s", e, exc_info=True)

    # =========================================================================
    # IB Event Handlers (called from ib_client sync callbacks)
    # =========================================================================

    def _handle_order_status(self, trade: Trade) -> None:
        """Track order status transitions and publish to bus."""
        order_id = trade.order.orderId
        new_status = trade.orderStatus.status
        old_status = self._order_statuses.get(order_id)

        if old_status == new_status:
            return  # no transition

        self._order_statuses[order_id] = new_status
        symbol = trade.contract.symbol if trade.contract else "?"

        self._logger.info(
            "Order %d (%s) status: %s → %s",
            order_id, symbol, old_status or "NEW", new_status,
        )

        # Publish status change to bus (fire-and-forget from sync context)
        if self.bus:
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.publish(Topic.ORDER_STATUS, {
                    "order_id": order_id,
                    "symbol": symbol,
                    "old_status": old_status,
                    "new_status": new_status,
                    "filled": float(trade.orderStatus.filled),
                    "remaining": float(trade.orderStatus.remaining),
                }))
            except RuntimeError:
                pass  # no running loop

    def _handle_fill(self, trade: Trade, fill: Fill) -> None:
        """Process a fill event — detect TP/SL exits and record P&L."""
        exec_info = fill.execution
        comm_info = fill.commissionReport
        symbol = trade.contract.symbol if trade.contract else "?"
        order_id = trade.order.orderId

        realized_pnl = Decimal(str(comm_info.realizedPNL)) if comm_info and comm_info.realizedPNL else Decimal("0")
        commission = Decimal(str(comm_info.commission)) if comm_info and comm_info.commission else Decimal("0")

        self._logger.info(
            "Fill detected: order=%d %s %s qty=%s @ %.2f | pnl=%.2f commission=%.2f",
            order_id, exec_info.side, symbol,
            exec_info.shares, exec_info.price,
            float(realized_pnl), float(commission),
        )

        # Publish fill to bus
        if self.bus:
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.publish(Topic.FILL, {
                    "order_id": order_id,
                    "symbol": symbol,
                    "side": exec_info.side,
                    "qty": float(exec_info.shares),
                    "price": exec_info.price,
                    "realized_pnl": float(realized_pnl),
                    "commission": float(commission),
                    "exec_id": exec_info.execId,
                }))
            except RuntimeError:
                pass

        # Detect if this fill closes our tracked position
        if symbol in self._active_trades:
            entry_info = self._active_trades[symbol]
            intent: TradeIntent = entry_info["intent"]
            entry_trades: List[Trade] = entry_info.get("trades", [])

            # Check if this is a TP or SL fill (exit side)
            is_exit = self._is_exit_fill(intent.setup.direction, exec_info.side)
            if is_exit and trade.orderStatus.remaining == 0:
                is_stop = self._is_stop_loss_fill(
                    trade, intent.setup.stop_price, intent.setup.direction,
                )
                net_pnl = realized_pnl - commission
                self._logger.info(
                    "Position closed: %s %s | pnl=%.2f (stop=%s)",
                    intent.setup.direction.value, symbol,
                    float(net_pnl), is_stop,
                )
                self.record_exit(symbol, net_pnl, is_stop)

    def _handle_ib_error(self, req_id: int, error_code: int, error_string: str) -> None:
        """Handle IB errors that may affect orders."""
        # Order-related error codes
        if 200 <= error_code < 400:
            self._logger.warning(
                "IB order error [%d] reqId=%d: %s", error_code, req_id, error_string,
            )
        # Informational codes (e.g., 2104 = market data farm connected)
        elif error_code >= 2000:
            self._logger.debug(
                "IB info [%d] reqId=%d: %s", error_code, req_id, error_string,
            )

    @staticmethod
    def _is_exit_fill(direction: Direction, fill_side: str) -> bool:
        """Check if a fill is on the exit side of the position."""
        if direction == Direction.LONG:
            return fill_side.upper() in ("SLD", "SELL")
        return fill_side.upper() in ("BOT", "BUY")

    @staticmethod
    def _is_stop_loss_fill(
        trade: Trade,
        stop_price: Decimal,
        direction: Direction,
    ) -> bool:
        """Heuristic: if the fill is a stop order type or price is at/beyond SL."""
        order_type = trade.order.orderType.upper() if trade.order.orderType else ""
        if "STP" in order_type or "STOP" in order_type:
            return True
        fill_price = Decimal(str(trade.orderStatus.avgFillPrice)) if trade.orderStatus.avgFillPrice else None
        if fill_price is None:
            return False
        if direction == Direction.LONG:
            return fill_price <= stop_price
        return fill_price >= stop_price

    def record_exit(
        self, symbol: str, pnl_usd: Decimal, is_stop: bool
    ) -> None:
        """Record a trade exit (called when fill detected).

        Args:
            symbol: Contract symbol
            pnl_usd: Realized P&L
            is_stop: Whether exit was stop loss
        """
        self._risk_manager.record_fill(pnl_usd, is_stop)
        self._kill_switch.record_trade_result(pnl_usd, is_stop)

        if is_stop and symbol in self._active_trades:
            intent = self._active_trades[symbol]["intent"]
            if symbol not in self._stopped_directions:
                self._stopped_directions[symbol] = set()
            self._stopped_directions[symbol].add(intent.setup.direction)

        self._active_trades.pop(symbol, None)

    async def flatten_all(self) -> None:
        """Flatten all positions (EOD)."""
        await self._ib_client.flatten_all()
        self._active_trades.clear()
        self._logger.info("All positions flattened (EOD)")

    def reset_daily(self) -> None:
        """Reset daily state."""
        self._stopped_directions.clear()
        self._active_trades.clear()
        self._order_statuses.clear()

    @property
    def has_active_trades(self) -> bool:
        return len(self._active_trades) > 0
