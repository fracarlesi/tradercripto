"""Circuit breaker for emergency stop conditions with temporal kill-switch support."""

import asyncio
import logging
import sys
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, Callable, List, Coroutine, Any, Dict
from dataclasses import dataclass, field

from ..core.enums import AlertSeverity
from ..core.exceptions import CircuitBreakerTriggeredError
from ..config.settings import Settings
from .temporal_risk import TemporalRiskState, KillSwitchLevel, DrawdownBucketConfig


logger = logging.getLogger(__name__)


@dataclass
class CircuitBreakerState:
    """Current state of the circuit breaker."""
    is_triggered: bool = False
    trigger_reason: str = ""
    triggered_at: Optional[datetime] = None
    daily_pnl: Decimal = Decimal(0)
    daily_pnl_pct: Decimal = Decimal(0)
    total_drawdown_pct: Decimal = Decimal(0)
    peak_equity: Decimal = Decimal(0)
    current_equity: Decimal = Decimal(0)

    # Temporal kill-switch state (for cooldowns, not hard stops)
    temporal_kill_switch_active: bool = False
    active_kill_switch_level: Optional[KillSwitchLevel] = None
    cooldown_until: Optional[datetime] = None


class CircuitBreaker:
    """
    Circuit breaker that halts trading when risk limits are breached.

    Two-tier system:
    1. Temporal Kill-Switch (soft): Temporary cooldowns for rapid losses
       - Level 1: 0.7% in 30s -> 15min cooldown
       - Level 2: 2% in 10min -> 1h cooldown
       - Level 3: 4.5% in 1h -> 6h cooldown
       Automatic recovery after cooldown expires.

    2. Hard Circuit Breaker: Critical limits that halt the bot
       - Daily loss > 8-10% -> Exit process
       - Total drawdown > 40-50% -> Exit process
       Requires manual restart after review.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.risk_config = settings.risk

        # State
        self._state = CircuitBreakerState()
        self._daily_start_equity: Optional[Decimal] = None
        self._daily_reset_time: Optional[datetime] = None

        # Temporal risk state (kill-switch with cooldowns)
        self._temporal_state: Optional[TemporalRiskState] = None
        self._init_temporal_state()

        # Alert callbacks
        self._alert_callbacks: List[Callable[[str, AlertSeverity], Coroutine[Any, Any, None]]] = []

        # Lock for thread safety
        self._lock = asyncio.Lock()

    def _init_temporal_state(self):
        """Initialize temporal risk state from config or defaults."""
        # Check if temporal risk config exists in settings
        if hasattr(self.risk_config, 'temporal_risk') and self.risk_config.temporal_risk:
            temporal_cfg = self.risk_config.temporal_risk
            configs = []

            if hasattr(temporal_cfg, 'level_1') and temporal_cfg.level_1:
                configs.append({
                    "level": "level_1",
                    "window_seconds": temporal_cfg.level_1.window_seconds,
                    "max_drawdown_pct": temporal_cfg.level_1.max_drawdown_pct,
                    "cooldown_seconds": temporal_cfg.level_1.cooldown_seconds,
                })
            if hasattr(temporal_cfg, 'level_2') and temporal_cfg.level_2:
                configs.append({
                    "level": "level_2",
                    "window_seconds": temporal_cfg.level_2.window_seconds,
                    "max_drawdown_pct": temporal_cfg.level_2.max_drawdown_pct,
                    "cooldown_seconds": temporal_cfg.level_2.cooldown_seconds,
                })
            if hasattr(temporal_cfg, 'level_3') and temporal_cfg.level_3:
                configs.append({
                    "level": "level_3",
                    "window_seconds": temporal_cfg.level_3.window_seconds,
                    "max_drawdown_pct": temporal_cfg.level_3.max_drawdown_pct,
                    "cooldown_seconds": temporal_cfg.level_3.cooldown_seconds,
                })

            max_positions = getattr(self.risk_config, 'max_open_positions', 15)

            if configs:
                self._temporal_state = TemporalRiskState.create_from_config(
                    configs, max_open_positions=max_positions
                )
                logger.info(f"Temporal risk initialized with {len(configs)} buckets")
                return

        # Fall back to default Phase C configuration
        self._temporal_state = TemporalRiskState.create_default()
        logger.info("Temporal risk initialized with default Phase C config")

    def on_alert(self, callback: Callable[[str, AlertSeverity], Coroutine[Any, Any, None]]):
        """Register alert callback."""
        self._alert_callbacks.append(callback)

    async def _send_alert(self, message: str, severity: AlertSeverity):
        """Send alert to all registered callbacks."""
        for callback in self._alert_callbacks:
            try:
                await callback(message, severity)
            except Exception as e:
                logger.error(f"Alert callback failed: {e}")

    @property
    def is_triggered(self) -> bool:
        """Check if circuit breaker is triggered."""
        return self._state.is_triggered

    @property
    def state(self) -> CircuitBreakerState:
        """Get current state."""
        return self._state

    async def initialize(self, current_equity: Decimal):
        """Initialize circuit breaker with starting equity."""
        async with self._lock:
            now = datetime.now(timezone.utc)

            self._state.current_equity = current_equity
            self._state.peak_equity = current_equity
            self._daily_start_equity = current_equity
            self._daily_reset_time = now.replace(hour=0, minute=0, second=0, microsecond=0)

            logger.info(f"Circuit breaker initialized with equity: ${current_equity}")

    async def update(self, current_equity: Decimal) -> bool:
        """
        Update circuit breaker with current equity.

        Returns True if trading should continue, False if halted.
        Checks both hard limits (exit process) and temporal limits (cooldown).
        """
        if not self.risk_config.circuit_breaker_enabled:
            return True

        if self._state.is_triggered:
            return False

        async with self._lock:
            now = datetime.now(timezone.utc)

            # Check for daily reset
            await self._check_daily_reset(now, current_equity)

            # Update state
            self._state.current_equity = current_equity

            # Update peak equity
            if current_equity > self._state.peak_equity:
                self._state.peak_equity = current_equity

            # Calculate daily P&L
            if self._daily_start_equity and self._daily_start_equity > 0:
                self._state.daily_pnl = current_equity - self._daily_start_equity
                self._state.daily_pnl_pct = self._state.daily_pnl / self._daily_start_equity

            # Calculate total drawdown
            if self._state.peak_equity > 0:
                self._state.total_drawdown_pct = (
                    (self._state.peak_equity - current_equity) / self._state.peak_equity
                )

            # Check hard limits (will exit process if breached)
            await self._check_limits()

            # Update temporal kill-switch state
            await self._update_temporal_state(current_equity, now)

            # Return False if either hard circuit breaker OR temporal kill-switch is active
            return not self._state.is_triggered and not self._state.temporal_kill_switch_active

    async def _update_temporal_state(self, current_equity: Decimal, now: datetime):
        """Update temporal kill-switch state and check for triggers."""
        if self._temporal_state is None:
            return

        # Check if any cooldowns have expired
        old_active = self._state.temporal_kill_switch_active

        # Update temporal state with new equity
        trading_allowed = self._temporal_state.update(current_equity, now)

        # Update circuit breaker state
        self._state.temporal_kill_switch_active = not trading_allowed

        if not trading_allowed:
            active_bucket = self._temporal_state.get_active_bucket()
            if active_bucket:
                self._state.active_kill_switch_level = active_bucket.level
                self._state.cooldown_until = active_bucket.cooldown_until

                # Send alert if just triggered (wasn't active before)
                if not old_active:
                    await self._send_alert(
                        f"⚠️ KILL-SWITCH {active_bucket.level.value.upper()} TRIGGERED\n\n"
                        f"Drawdown: {active_bucket.current_drawdown_pct:.2%} "
                        f"(limit: {active_bucket.config.max_drawdown_pct:.2%})\n"
                        f"Window: {active_bucket.config.window_seconds}s\n"
                        f"Cooldown: {active_bucket.config.cooldown_seconds}s\n"
                        f"Resume at: {active_bucket.cooldown_until.isoformat() if active_bucket.cooldown_until else 'N/A'}\n\n"
                        f"Trading PAUSED (auto-resume after cooldown)",
                        AlertSeverity.WARNING
                    )
        else:
            # Cooldown expired
            if old_active:
                await self._send_alert(
                    f"✅ Kill-switch cooldown EXPIRED\n"
                    f"Trading can resume.",
                    AlertSeverity.INFO
                )
            self._state.active_kill_switch_level = None
            self._state.cooldown_until = None

    async def _check_daily_reset(self, now: datetime, current_equity: Decimal):
        """Check if daily metrics should be reset."""
        if self._daily_reset_time is None:
            self._daily_reset_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
            self._daily_start_equity = current_equity
            return

        # Reset at midnight UTC
        next_reset = self._daily_reset_time + timedelta(days=1)
        if now >= next_reset:
            logger.info(f"Daily reset - Previous daily P&L: {self._state.daily_pnl_pct:.2%}")
            self._daily_reset_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
            self._daily_start_equity = current_equity
            self._state.daily_pnl = Decimal(0)
            self._state.daily_pnl_pct = Decimal(0)

    async def _check_limits(self):
        """Check if any limits are breached."""
        # Check daily loss limit
        max_daily_loss = self.risk_config.max_daily_loss_pct
        if self._state.daily_pnl_pct <= -max_daily_loss:
            await self._trigger(
                f"Daily loss limit breached: {self._state.daily_pnl_pct:.2%} "
                f"(limit: -{max_daily_loss:.2%})"
            )
            return

        # Check total drawdown limit
        max_dd = self.risk_config.max_total_drawdown_pct
        if self._state.total_drawdown_pct >= max_dd:
            await self._trigger(
                f"Total drawdown limit breached: {self._state.total_drawdown_pct:.2%} "
                f"(limit: {max_dd:.2%})"
            )
            return

        # Warning thresholds (70% of limit)
        warning_threshold = Decimal("0.7")

        if self._state.daily_pnl_pct <= -max_daily_loss * warning_threshold:
            await self._send_alert(
                f"WARNING: Daily loss at {self._state.daily_pnl_pct:.2%} "
                f"(limit: -{max_daily_loss:.2%})",
                AlertSeverity.WARNING
            )

        if self._state.total_drawdown_pct >= max_dd * warning_threshold:
            await self._send_alert(
                f"WARNING: Total drawdown at {self._state.total_drawdown_pct:.2%} "
                f"(limit: {max_dd:.2%})",
                AlertSeverity.WARNING
            )

    async def _trigger(self, reason: str):
        """Trigger the circuit breaker and exit the process."""
        self._state.is_triggered = True
        self._state.trigger_reason = reason
        self._state.triggered_at = datetime.now(timezone.utc)

        logger.critical(f"CIRCUIT BREAKER TRIGGERED: {reason}")

        await self._send_alert(
            f"🚨 CIRCUIT BREAKER TRIGGERED 🚨\n\n"
            f"Reason: {reason}\n"
            f"Current equity: ${self._state.current_equity:.2f}\n"
            f"Daily P&L: {self._state.daily_pnl_pct:.2%}\n"
            f"Total DD: {self._state.total_drawdown_pct:.2%}\n\n"
            f"Trading HALTED. Process exiting.\n"
            f"Manual restart required after review.",
            AlertSeverity.EMERGENCY
        )

        # Give time for alerts to be sent
        await asyncio.sleep(2)

        # Exit with code 0 so Docker doesn't auto-restart
        # (restart: unless-stopped only restarts on non-zero exit)
        logger.critical("Exiting process - manual restart required after circuit breaker trigger")
        sys.exit(0)

    async def manual_trigger(self, reason: str = "Manual trigger"):
        """Manually trigger the circuit breaker."""
        async with self._lock:
            await self._trigger(f"MANUAL: {reason}")

    async def reset(self, new_equity: Optional[Decimal] = None):
        """
        Reset the circuit breaker (manual only).

        WARNING: This should only be called after manual review.
        """
        async with self._lock:
            logger.warning("Circuit breaker being reset manually")

            self._state = CircuitBreakerState()

            if new_equity:
                self._state.current_equity = new_equity
                self._state.peak_equity = new_equity
                self._daily_start_equity = new_equity

            await self._send_alert(
                "Circuit breaker has been manually reset. Trading can resume.",
                AlertSeverity.INFO
            )

    def check_can_trade(self) -> bool:
        """
        Check if trading is allowed.

        Raises CircuitBreakerTriggeredError if not.
        """
        if self._state.is_triggered:
            raise CircuitBreakerTriggeredError(
                "Circuit breaker is triggered - trading halted",
                reason=self._state.trigger_reason,
                triggered_at=self._state.triggered_at.isoformat() if self._state.triggered_at else None,
            )
        return True

    def get_risk_metrics(self) -> dict:
        """Get current risk metrics."""
        metrics = {
            "is_triggered": self._state.is_triggered,
            "trigger_reason": self._state.trigger_reason,
            "triggered_at": self._state.triggered_at.isoformat() if self._state.triggered_at else None,
            "daily_pnl": float(self._state.daily_pnl),
            "daily_pnl_pct": float(self._state.daily_pnl_pct),
            "total_drawdown_pct": float(self._state.total_drawdown_pct),
            "peak_equity": float(self._state.peak_equity),
            "current_equity": float(self._state.current_equity),
            # Temporal kill-switch metrics
            "temporal_kill_switch_active": self._state.temporal_kill_switch_active,
            "active_kill_switch_level": (
                self._state.active_kill_switch_level.value
                if self._state.active_kill_switch_level else None
            ),
            "cooldown_until": (
                self._state.cooldown_until.isoformat()
                if self._state.cooldown_until else None
            ),
        }

        # Add temporal state details if available
        if self._temporal_state:
            metrics["temporal_state"] = self._temporal_state.to_dict()

        return metrics

    # -------------------------------------------------------------------------
    # Temporal Kill-Switch Methods
    # -------------------------------------------------------------------------

    def can_trade(self) -> bool:
        """
        Check if trading is currently allowed.

        Returns False if either:
        - Hard circuit breaker is triggered (requires restart)
        - Temporal kill-switch is in cooldown (auto-recovers)
        """
        if self._state.is_triggered:
            return False
        if self._state.temporal_kill_switch_active:
            return False
        return True

    def can_open_position(self, current_position_count: int) -> bool:
        """
        Check if a new position can be opened.

        Returns False if:
        - Trading not allowed (circuit breaker or kill-switch)
        - Max positions reached
        """
        if not self.can_trade():
            return False

        if self._temporal_state:
            return self._temporal_state.can_open_position(current_position_count)

        return True

    @property
    def temporal_state(self) -> Optional[TemporalRiskState]:
        """Get temporal risk state."""
        return self._temporal_state

    @property
    def max_open_positions(self) -> int:
        """Get max allowed open positions."""
        if self._temporal_state:
            return self._temporal_state.max_open_positions
        return 15  # Default

    def get_cooldown_remaining(self) -> int:
        """Get remaining cooldown time in seconds (0 if not in cooldown)."""
        if not self._state.temporal_kill_switch_active:
            return 0
        if self._temporal_state:
            active = self._temporal_state.get_active_bucket()
            if active:
                return active.cooldown_remaining_seconds
        return 0

    def is_temporal_cooldown(self) -> bool:
        """Check if temporal kill-switch cooldown is active."""
        return self._state.temporal_kill_switch_active and not self._state.is_triggered

    def get_temporal_cooldown_remaining(self) -> int:
        """Alias for get_cooldown_remaining for clarity."""
        return self.get_cooldown_remaining()

    def get_temporal_status(self) -> Dict:
        """Get human-readable temporal kill-switch status."""
        status = {
            "can_trade": self.can_trade(),
            "temporal_kill_switch_active": self._state.temporal_kill_switch_active,
            "active_level": None,
            "cooldown_remaining_seconds": 0,
            "cooldown_until": None,
            "status_message": "Trading allowed",
        }

        if self._state.is_triggered:
            status["status_message"] = f"HARD STOP: {self._state.trigger_reason}"
            return status

        if self._state.temporal_kill_switch_active and self._temporal_state:
            active = self._temporal_state.get_active_bucket()
            if active:
                status["active_level"] = active.level.value
                status["cooldown_remaining_seconds"] = active.cooldown_remaining_seconds
                status["cooldown_until"] = (
                    active.cooldown_until.isoformat() if active.cooldown_until else None
                )
                status["status_message"] = (
                    f"PAUSED: {active.level.value} kill-switch, "
                    f"{active.cooldown_remaining_seconds}s remaining"
                )

        return status

    async def force_reset_temporal(self, current_equity: Optional[Decimal] = None):
        """
        Force reset all temporal kill-switch buckets.

        WARNING: Use only after manual review.
        """
        async with self._lock:
            if self._temporal_state:
                equity = current_equity or self._state.current_equity
                self._temporal_state.force_reset(equity)
                self._state.temporal_kill_switch_active = False
                self._state.active_kill_switch_level = None
                self._state.cooldown_until = None

                logger.warning("Temporal kill-switch FORCE RESET")
                await self._send_alert(
                    "Temporal kill-switch has been force reset. Trading can resume.",
                    AlertSeverity.WARNING
                )
