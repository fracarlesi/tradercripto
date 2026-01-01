"""
HLQuantBot Kill Switch Service
===============================

Critical risk protection system - NON-NEGOTIABLE.

Monitors:
- Daily loss limits (2% default)
- Weekly loss limits (5% default)
- Maximum drawdown (15% default)

Actions:
- DAILY_PAUSE: Stop trading until tomorrow
- WEEKLY_PAUSE: Stop trading for 3 days
- STOPPED: Complete halt, manual intervention required

Author: Francesco Carlesi
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

from .base import BaseService
from .message_bus import MessageBus
from ..core.enums import Topic
from ..core.models import KillSwitchStatus, EquitySnapshot


logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class KillSwitchConfig:
    """Kill switch configuration."""

    enabled: bool = True

    # Daily protection
    daily_loss_pct: float = 2.0
    daily_loss_action: str = "pause_until_tomorrow"

    # Weekly protection
    weekly_loss_pct: float = 5.0
    weekly_loss_action: str = "pause_3_days"

    # Max drawdown
    max_drawdown_pct: float = 15.0
    max_drawdown_action: str = "stop_all"

    # Check interval
    check_interval_seconds: int = 60

    # Telegram alerts
    telegram_enabled: bool = True


# =============================================================================
# Kill Switch Event
# =============================================================================

@dataclass
class KillSwitchEvent:
    """Record of a kill switch trigger."""

    timestamp: datetime
    trigger_type: str  # "daily", "weekly", "max_drawdown"
    trigger_value: float
    threshold: float
    action: str
    equity: float
    message: str


# =============================================================================
# Kill Switch Service
# =============================================================================

class KillSwitchService(BaseService):
    """
    Kill switch service for catastrophic loss prevention.

    Monitors equity curve and triggers protective actions when limits hit.

    CRITICAL: This service must ALWAYS be running and functional.
    """

    def __init__(
        self,
        name: str = "kill_switch",
        bus: Optional[MessageBus] = None,
        db: Optional[Any] = None,
        config: Optional[KillSwitchConfig] = None,
    ) -> None:
        """Initialize KillSwitchService."""
        self._ks_config = config or KillSwitchConfig()

        super().__init__(
            name=name,
            bus=bus,
            db=db,
            loop_interval_seconds=self._ks_config.check_interval_seconds,
        )

        # Kill switch state (use _ks_status to avoid collision with BaseService._status)
        self._ks_status = KillSwitchStatus.OK
        self._resume_time: Optional[datetime] = None

        # Equity tracking
        self._current_equity: Decimal = Decimal("0")
        self._peak_equity: Decimal = Decimal("0")
        self._start_of_day_equity: Decimal = Decimal("0")
        self._start_of_week_equity: Decimal = Decimal("0")
        self._last_day_reset: Optional[datetime] = None
        self._last_week_reset: Optional[datetime] = None

        # Event history
        self._events: List[KillSwitchEvent] = []

        # Callback for trading halt
        self._halt_callback: Optional[callable] = None

        self._logger.info(
            "KillSwitchService initialized: daily=%.1f%%, weekly=%.1f%%, max_dd=%.1f%%",
            self._ks_config.daily_loss_pct,
            self._ks_config.weekly_loss_pct,
            self._ks_config.max_drawdown_pct,
        )

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def _on_start(self) -> None:
        """Initialize kill switch."""
        self._logger.info("Starting KillSwitchService...")

        # Subscribe to equity updates if available
        if self.bus:
            await self.subscribe(Topic.METRICS, self._handle_metrics)

        # Initialize reset times
        now = datetime.utcnow()
        self._last_day_reset = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self._last_week_reset = now - timedelta(days=now.weekday())
        self._last_week_reset = self._last_week_reset.replace(hour=0, minute=0, second=0, microsecond=0)

    async def _on_stop(self) -> None:
        """Cleanup."""
        self._logger.info("Stopping KillSwitchService...")

    async def _run_iteration(self) -> None:
        """Check kill switch conditions."""
        if not self._ks_config.enabled:
            return

        # Check if paused and can resume
        if self._ks_status in (KillSwitchStatus.DAILY_PAUSE, KillSwitchStatus.WEEKLY_PAUSE):
            if self._resume_time and datetime.utcnow() >= self._resume_time:
                await self._resume_trading()
                return

        # Check day/week reset
        await self._check_period_reset()

        # Check conditions
        await self._check_daily_loss()
        await self._check_weekly_loss()
        await self._check_max_drawdown()

    async def _health_check_impl(self) -> bool:
        """Kill switch must always be healthy."""
        return True

    # =========================================================================
    # Metrics Handling
    # =========================================================================

    async def _handle_metrics(self, metrics_data: Dict) -> None:
        """Handle incoming metrics (equity updates)."""
        if "equity" in metrics_data:
            await self.update_equity(Decimal(str(metrics_data["equity"])))

    async def update_equity(self, equity: Decimal) -> None:
        """
        Update current equity and check limits.

        Args:
            equity: Current account equity
        """
        self._current_equity = equity

        # Update peak
        if equity > self._peak_equity:
            self._peak_equity = equity

        # Initialize start values if needed
        if self._start_of_day_equity == 0:
            self._start_of_day_equity = equity
        if self._start_of_week_equity == 0:
            self._start_of_week_equity = equity

    # =========================================================================
    # Period Resets
    # =========================================================================

    async def _check_period_reset(self) -> None:
        """Check if daily/weekly periods should reset."""
        now = datetime.utcnow()

        # Daily reset at UTC midnight
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if self._last_day_reset and today_start > self._last_day_reset:
            self._start_of_day_equity = self._current_equity
            self._last_day_reset = today_start
            self._logger.info("Daily reset: new start equity = $%.2f", float(self._current_equity))

        # Weekly reset on Monday UTC midnight
        week_start = now - timedelta(days=now.weekday())
        week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
        if self._last_week_reset and week_start > self._last_week_reset:
            self._start_of_week_equity = self._current_equity
            self._last_week_reset = week_start
            self._logger.info("Weekly reset: new start equity = $%.2f", float(self._current_equity))

    # =========================================================================
    # Limit Checks
    # =========================================================================

    async def _check_daily_loss(self) -> None:
        """Check daily loss limit."""
        if self._ks_status != KillSwitchStatus.OK:
            return

        if self._start_of_day_equity <= 0:
            return

        daily_pnl_pct = (
            (self._current_equity - self._start_of_day_equity)
            / self._start_of_day_equity
        ) * 100

        if daily_pnl_pct < -Decimal(str(self._ks_config.daily_loss_pct)):
            await self._trigger_daily_pause(float(daily_pnl_pct))

    async def _check_weekly_loss(self) -> None:
        """Check weekly loss limit."""
        if self._ks_status not in (KillSwitchStatus.OK, KillSwitchStatus.DAILY_PAUSE):
            return

        if self._start_of_week_equity <= 0:
            return

        weekly_pnl_pct = (
            (self._current_equity - self._start_of_week_equity)
            / self._start_of_week_equity
        ) * 100

        if weekly_pnl_pct < -Decimal(str(self._ks_config.weekly_loss_pct)):
            await self._trigger_weekly_pause(float(weekly_pnl_pct))

    async def _check_max_drawdown(self) -> None:
        """Check maximum drawdown limit."""
        if self._peak_equity <= 0:
            return

        drawdown_pct = (
            (self._peak_equity - self._current_equity)
            / self._peak_equity
        ) * 100

        if drawdown_pct > Decimal(str(self._ks_config.max_drawdown_pct)):
            await self._trigger_max_drawdown(float(drawdown_pct))

    # =========================================================================
    # Triggers
    # =========================================================================

    async def _trigger_daily_pause(self, loss_pct: float) -> None:
        """Trigger daily pause."""
        self._ks_status = KillSwitchStatus.DAILY_PAUSE

        # Resume tomorrow at market open
        now = datetime.utcnow()
        tomorrow = now + timedelta(days=1)
        self._resume_time = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)

        event = KillSwitchEvent(
            timestamp=now,
            trigger_type="daily",
            trigger_value=loss_pct,
            threshold=self._ks_config.daily_loss_pct,
            action="pause_until_tomorrow",
            equity=float(self._current_equity),
            message=f"Daily loss {loss_pct:.2f}% exceeded limit {self._ks_config.daily_loss_pct}%",
        )
        self._events.append(event)

        await self._halt_trading()
        await self._send_alert(event)

        self._logger.critical(
            "KILL SWITCH - DAILY PAUSE: Loss %.2f%% > %.2f%%, resume at %s",
            loss_pct, self._ks_config.daily_loss_pct, self._resume_time
        )

    async def _trigger_weekly_pause(self, loss_pct: float) -> None:
        """Trigger weekly pause."""
        self._ks_status = KillSwitchStatus.WEEKLY_PAUSE

        # Resume in 3 days
        self._resume_time = datetime.utcnow() + timedelta(days=3)

        event = KillSwitchEvent(
            timestamp=datetime.utcnow(),
            trigger_type="weekly",
            trigger_value=loss_pct,
            threshold=self._ks_config.weekly_loss_pct,
            action="pause_3_days",
            equity=float(self._current_equity),
            message=f"Weekly loss {loss_pct:.2f}% exceeded limit {self._ks_config.weekly_loss_pct}%",
        )
        self._events.append(event)

        await self._halt_trading()
        await self._send_alert(event)

        self._logger.critical(
            "KILL SWITCH - WEEKLY PAUSE: Loss %.2f%% > %.2f%%, resume at %s",
            loss_pct, self._ks_config.weekly_loss_pct, self._resume_time
        )

    async def _trigger_max_drawdown(self, drawdown_pct: float) -> None:
        """Trigger max drawdown stop."""
        self._ks_status = KillSwitchStatus.STOPPED
        self._resume_time = None  # Manual intervention required

        event = KillSwitchEvent(
            timestamp=datetime.utcnow(),
            trigger_type="max_drawdown",
            trigger_value=drawdown_pct,
            threshold=self._ks_config.max_drawdown_pct,
            action="stop_all",
            equity=float(self._current_equity),
            message=f"Max drawdown {drawdown_pct:.2f}% exceeded limit {self._ks_config.max_drawdown_pct}%",
        )
        self._events.append(event)

        await self._halt_trading()
        await self._send_alert(event)

        self._logger.critical(
            "KILL SWITCH - STOPPED: Drawdown %.2f%% > %.2f%%, MANUAL INTERVENTION REQUIRED",
            drawdown_pct, self._ks_config.max_drawdown_pct
        )

    # =========================================================================
    # Actions
    # =========================================================================

    async def _halt_trading(self) -> None:
        """Halt all trading activity."""
        # Publish halt message
        if self.bus:
            await self.publish(Topic.RISK_ALERTS, {
                "type": "kill_switch",
                "status": self._ks_status.value,
                "timestamp": datetime.utcnow().isoformat(),
                "resume_time": self._resume_time.isoformat() if self._resume_time else None,
            })

        # Call halt callback if registered
        if self._halt_callback:
            try:
                await self._halt_callback()
            except Exception as e:
                self._logger.error("Error in halt callback: %s", e)

    async def _resume_trading(self) -> None:
        """Resume trading after pause."""
        old_status = self._ks_status
        self._ks_status = KillSwitchStatus.OK
        self._resume_time = None

        self._logger.info(
            "Kill switch resumed: %s -> OK",
            old_status.value,
        )

        if self.bus:
            await self.publish(Topic.RISK_ALERTS, {
                "type": "kill_switch_resume",
                "previous_status": old_status.value,
                "timestamp": datetime.utcnow().isoformat(),
            })

    async def _send_alert(self, event: KillSwitchEvent) -> None:
        """Send alert (Telegram, etc.)."""
        # In production, integrate with Telegram alerter
        self._logger.warning("ALERT: %s", event.message)

        # Store event in database if available
        if self.db:
            try:
                await self.db.execute(
                    """
                    INSERT INTO kill_switch_log (timestamp, trigger_type, trigger_value, action_taken)
                    VALUES ($1, $2, $3, $4)
                    """,
                    event.timestamp,
                    event.trigger_type,
                    event.trigger_value,
                    event.action,
                )
            except Exception as e:
                self._logger.error("Failed to log event: %s", e)

    # =========================================================================
    # Public API
    # =========================================================================

    def is_trading_allowed(self) -> bool:
        """Check if trading is currently allowed."""
        return self._ks_status == KillSwitchStatus.OK

    def get_status(self) -> KillSwitchStatus:
        """Get current kill switch status."""
        return self._ks_status

    def get_resume_time(self) -> Optional[datetime]:
        """Get time when trading will resume (if paused)."""
        return self._resume_time

    def set_halt_callback(self, callback: callable) -> None:
        """Set callback to call when trading is halted."""
        self._halt_callback = callback

    def manual_resume(self) -> bool:
        """
        Manually resume trading (after STOPPED status).

        Returns:
            True if resumed, False if not in STOPPED status
        """
        if self._ks_status == KillSwitchStatus.STOPPED:
            self._ks_status = KillSwitchStatus.OK
            self._resume_time = None
            self._logger.warning("Manual resume from STOPPED status")
            return True
        return False

    def get_current_drawdown(self) -> float:
        """Get current drawdown percentage."""
        if self._peak_equity <= 0:
            return 0.0
        return float(
            (self._peak_equity - self._current_equity)
            / self._peak_equity * 100
        )

    def get_daily_pnl_pct(self) -> float:
        """Get current daily P&L percentage."""
        if self._start_of_day_equity <= 0:
            return 0.0
        return float(
            (self._current_equity - self._start_of_day_equity)
            / self._start_of_day_equity * 100
        )

    def get_weekly_pnl_pct(self) -> float:
        """Get current weekly P&L percentage."""
        if self._start_of_week_equity <= 0:
            return 0.0
        return float(
            (self._current_equity - self._start_of_week_equity)
            / self._start_of_week_equity * 100
        )

    @property
    def metrics(self) -> Dict[str, Any]:
        """Get service metrics."""
        return {
            "status": self._ks_status.value,
            "is_trading_allowed": self.is_trading_allowed(),
            "current_equity": float(self._current_equity),
            "peak_equity": float(self._peak_equity),
            "drawdown_pct": self.get_current_drawdown(),
            "daily_pnl_pct": self.get_daily_pnl_pct(),
            "weekly_pnl_pct": self.get_weekly_pnl_pct(),
            "resume_time": self._resume_time.isoformat() if self._resume_time else None,
            "events_count": len(self._events),
        }


# =============================================================================
# Factory
# =============================================================================

def create_kill_switch(
    bus: Optional[MessageBus] = None,
    db: Optional[Any] = None,
    config: Optional[KillSwitchConfig] = None,
) -> KillSwitchService:
    """Factory function to create KillSwitchService."""
    return KillSwitchService(
        name="kill_switch",
        bus=bus,
        db=db,
        config=config,
    )
