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

    async def _on_start(self) -> None:
        if self.bus:
            await self.subscribe(Topic.ORDER, self._handle_trade_intent)
        self._logger.info("ExecutionEngine started")

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

    @property
    def has_active_trades(self) -> bool:
        return len(self._active_trades) > 0
