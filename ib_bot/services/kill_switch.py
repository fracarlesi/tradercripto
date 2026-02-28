"""
Kill Switch Service (Simplified)
=================================

Simple safety circuit breaker for futures:
- 2 consecutive stops → halt for the day
- Daily loss > $1000 → halt
- TP resets consecutive stop counter
- Auto-reset next trading day
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from .base import BaseService
from .message_bus import MessageBus
from ..config.loader import RiskConfig
from ..core.enums import KillSwitchStatus, Topic

logger = logging.getLogger(__name__)


class KillSwitchService(BaseService):
    """Simple kill switch: consecutive stops + daily loss limit."""

    def __init__(
        self,
        config: RiskConfig,
        bus: Optional[MessageBus] = None,
    ) -> None:
        super().__init__(name="kill_switch", bus=bus, loop_interval_seconds=30.0)
        self._config = config
        self._status = KillSwitchStatus.ACTIVE
        self._consecutive_stops: int = 0
        self._daily_loss_usd = Decimal("0")
        self._halt_reason: str = ""

    async def _on_start(self) -> None:
        self._logger.info(
            "KillSwitch started: halt at %d consecutive stops or $%.0f daily loss",
            self._config.consecutive_stops_halt,
            float(self._config.max_daily_loss_usd),
        )

    async def _on_stop(self) -> None:
        pass

    def record_trade_result(self, pnl_usd: Decimal, is_stop: bool) -> None:
        """Record a trade result and check halt conditions.

        Args:
            pnl_usd: P&L in USD
            is_stop: Whether exit was a stop loss
        """
        if pnl_usd < 0:
            self._daily_loss_usd += abs(pnl_usd)

        if is_stop:
            self._consecutive_stops += 1
        else:
            # TP resets consecutive stop counter
            self._consecutive_stops = 0

        # Check halt conditions
        if self._consecutive_stops >= self._config.consecutive_stops_halt:
            self._halt(
                f"{self._consecutive_stops} consecutive stops "
                f"(limit: {self._config.consecutive_stops_halt})"
            )
        elif self._daily_loss_usd >= self._config.max_daily_loss_usd:
            self._halt(
                f"Daily loss ${float(self._daily_loss_usd):.0f} "
                f">= limit ${float(self._config.max_daily_loss_usd):.0f}"
            )

    def _halt(self, reason: str) -> None:
        """Halt trading."""
        self._status = KillSwitchStatus.HALTED
        self._halt_reason = reason
        self._logger.critical("KILL SWITCH HALTED: %s", reason)

    def reset_daily(self) -> None:
        """Reset for new trading day."""
        self._status = KillSwitchStatus.ACTIVE
        self._consecutive_stops = 0
        self._daily_loss_usd = Decimal("0")
        self._halt_reason = ""
        self._logger.info("Kill switch reset for new trading day")

    @property
    def is_trading_allowed(self) -> bool:
        return self._status == KillSwitchStatus.ACTIVE

    @property
    def status(self) -> KillSwitchStatus:
        return self._status

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    @property
    def metrics(self) -> dict:
        return {
            "status": self._status.value,
            "trading_allowed": self.is_trading_allowed,
            "consecutive_stops": self._consecutive_stops,
            "daily_loss_usd": float(self._daily_loss_usd),
            "halt_reason": self._halt_reason,
        }
