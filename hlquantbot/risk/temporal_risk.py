"""Temporal risk management with kill-switch buckets for HFT trading."""

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from enum import Enum
from typing import Optional, List, Deque, Dict

logger = logging.getLogger(__name__)


class KillSwitchLevel(str, Enum):
    """Kill-switch severity levels."""
    LEVEL_1 = "level_1"    # 30s window, 0.7% DD, 15min cooldown
    LEVEL_2 = "level_2"    # 10min window, 2% DD, 1h cooldown
    LEVEL_3 = "level_3"    # 1h window, 4.5% DD, 6h cooldown
    DAILY = "daily"        # 24h window, 8-10% DD, end of day
    TOTAL = "total"        # All-time, 40-50% DD, manual restart


@dataclass
class EquitySample:
    """Single equity sample with timestamp for temporal tracking."""
    timestamp: datetime
    equity: Decimal

    def __post_init__(self):
        if self.timestamp.tzinfo is None:
            self.timestamp = self.timestamp.replace(tzinfo=timezone.utc)


@dataclass
class DrawdownBucketConfig:
    """Configuration for a single drawdown bucket."""
    level: KillSwitchLevel
    window_seconds: int          # Time window to track (30, 600, 3600, etc.)
    max_drawdown_pct: Decimal    # Max allowed drawdown (0.007, 0.02, 0.045, etc.)
    cooldown_seconds: int        # Cooldown duration after trigger

    @property
    def window_timedelta(self) -> timedelta:
        return timedelta(seconds=self.window_seconds)

    @property
    def cooldown_timedelta(self) -> timedelta:
        return timedelta(seconds=self.cooldown_seconds)


@dataclass
class DrawdownBucket:
    """
    Bucket for tracking drawdown in a specific time window.

    Maintains a deque of equity samples and calculates:
    - Peak equity within the window
    - Current drawdown from peak
    - Trigger status with cooldown
    """
    config: DrawdownBucketConfig

    # Rolling samples within the window
    samples: Deque[EquitySample] = field(default_factory=deque)

    # Tracking state
    window_peak: Decimal = Decimal(0)
    current_equity: Decimal = Decimal(0)
    current_drawdown_pct: Decimal = Decimal(0)

    # Trigger state
    is_triggered: bool = False
    triggered_at: Optional[datetime] = None
    cooldown_until: Optional[datetime] = None
    trigger_count: int = 0  # Number of times triggered today

    @property
    def level(self) -> KillSwitchLevel:
        return self.config.level

    @property
    def is_in_cooldown(self) -> bool:
        """Check if currently in cooldown period."""
        if self.cooldown_until is None:
            return False
        now = datetime.now(timezone.utc)
        return now < self.cooldown_until

    @property
    def cooldown_remaining_seconds(self) -> int:
        """Get remaining cooldown time in seconds."""
        if not self.is_in_cooldown or self.cooldown_until is None:
            return 0
        now = datetime.now(timezone.utc)
        remaining = (self.cooldown_until - now).total_seconds()
        return max(0, int(remaining))

    def add_sample(self, equity: Decimal, timestamp: Optional[datetime] = None) -> bool:
        """
        Add a new equity sample and update calculations.

        Returns True if still within limits, False if limit breached.
        """
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        elif timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)

        sample = EquitySample(timestamp=timestamp, equity=equity)
        self.samples.append(sample)
        self.current_equity = equity

        # Clean old samples outside the window
        self._cleanup_old_samples(timestamp)

        # Recalculate peak and drawdown
        self._recalculate_metrics()

        # Check if limit breached (only if not already in cooldown)
        if not self.is_in_cooldown:
            if self.current_drawdown_pct >= self.config.max_drawdown_pct:
                self._trigger(timestamp)
                return False

        return True

    def _cleanup_old_samples(self, now: datetime):
        """Remove samples older than the window duration."""
        cutoff = now - self.config.window_timedelta
        while self.samples and self.samples[0].timestamp < cutoff:
            self.samples.popleft()

    def _recalculate_metrics(self):
        """Recalculate peak equity and drawdown from samples."""
        if not self.samples:
            self.window_peak = self.current_equity
            self.current_drawdown_pct = Decimal(0)
            return

        # Find peak within the window
        self.window_peak = max(s.equity for s in self.samples)

        # Calculate drawdown from peak
        if self.window_peak > 0:
            self.current_drawdown_pct = (
                (self.window_peak - self.current_equity) / self.window_peak
            )
        else:
            self.current_drawdown_pct = Decimal(0)

    def _trigger(self, timestamp: datetime):
        """Trigger the kill-switch for this bucket."""
        self.is_triggered = True
        self.triggered_at = timestamp
        self.cooldown_until = timestamp + self.config.cooldown_timedelta
        self.trigger_count += 1

        logger.warning(
            f"Kill-switch {self.level.value} TRIGGERED: "
            f"DD {self.current_drawdown_pct:.2%} >= {self.config.max_drawdown_pct:.2%} "
            f"in {self.config.window_seconds}s window. "
            f"Cooldown until {self.cooldown_until.isoformat()}"
        )

    def check_cooldown_expired(self) -> bool:
        """
        Check and clear cooldown if expired.

        Returns True if cooldown just expired, False otherwise.
        """
        if not self.is_triggered:
            return False

        if not self.is_in_cooldown:
            # Cooldown expired - reset trigger state
            self.is_triggered = False
            self.triggered_at = None
            self.cooldown_until = None

            # Reset peak to current equity to start fresh
            self.window_peak = self.current_equity
            self.samples.clear()

            logger.info(
                f"Kill-switch {self.level.value} cooldown EXPIRED. "
                f"Trading can resume."
            )
            return True

        return False

    def reset_daily(self, current_equity: Decimal):
        """Reset daily counters (called at midnight UTC)."""
        self.trigger_count = 0
        # Don't reset trigger state if currently in cooldown
        if not self.is_in_cooldown:
            self.samples.clear()
            self.window_peak = current_equity
            self.current_equity = current_equity
            self.current_drawdown_pct = Decimal(0)

    def to_dict(self) -> Dict:
        """Convert to dictionary for API/logging."""
        return {
            "level": self.level.value,
            "window_seconds": self.config.window_seconds,
            "max_drawdown_pct": float(self.config.max_drawdown_pct),
            "cooldown_seconds": self.config.cooldown_seconds,
            "window_peak": float(self.window_peak),
            "current_equity": float(self.current_equity),
            "current_drawdown_pct": float(self.current_drawdown_pct),
            "is_triggered": self.is_triggered,
            "is_in_cooldown": self.is_in_cooldown,
            "cooldown_remaining_seconds": self.cooldown_remaining_seconds,
            "triggered_at": self.triggered_at.isoformat() if self.triggered_at else None,
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None,
            "trigger_count": self.trigger_count,
            "sample_count": len(self.samples),
        }


@dataclass
class TemporalRiskState:
    """
    Complete temporal risk management state.

    Manages multiple DrawdownBuckets with different time windows
    and provides a unified interface for kill-switch checks.
    """
    buckets: List[DrawdownBucket] = field(default_factory=list)

    # Overall state
    is_trading_allowed: bool = True
    active_kill_switch: Optional[KillSwitchLevel] = None

    # Position limits
    max_open_positions: int = 15

    # Tracking
    last_update: Optional[datetime] = None
    total_trigger_count: int = 0

    @classmethod
    def create_default(cls) -> "TemporalRiskState":
        """Create with default Phase C configuration."""
        configs = [
            DrawdownBucketConfig(
                level=KillSwitchLevel.LEVEL_1,
                window_seconds=30,
                max_drawdown_pct=Decimal("0.007"),  # 0.7%
                cooldown_seconds=900,  # 15 minutes
            ),
            DrawdownBucketConfig(
                level=KillSwitchLevel.LEVEL_2,
                window_seconds=600,  # 10 minutes
                max_drawdown_pct=Decimal("0.02"),  # 2%
                cooldown_seconds=3600,  # 1 hour
            ),
            DrawdownBucketConfig(
                level=KillSwitchLevel.LEVEL_3,
                window_seconds=3600,  # 1 hour
                max_drawdown_pct=Decimal("0.045"),  # 4.5%
                cooldown_seconds=21600,  # 6 hours
            ),
        ]

        buckets = [DrawdownBucket(config=cfg) for cfg in configs]
        return cls(buckets=buckets)

    @classmethod
    def create_from_config(
        cls,
        level_configs: List[Dict],
        max_open_positions: int = 15
    ) -> "TemporalRiskState":
        """
        Create from configuration dictionary.

        level_configs should be a list of dicts with:
        - level: str (level_1, level_2, level_3)
        - window_seconds: int
        - max_drawdown_pct: float
        - cooldown_seconds: int
        """
        buckets = []
        for cfg in level_configs:
            bucket_config = DrawdownBucketConfig(
                level=KillSwitchLevel(cfg["level"]),
                window_seconds=cfg["window_seconds"],
                max_drawdown_pct=Decimal(str(cfg["max_drawdown_pct"])),
                cooldown_seconds=cfg["cooldown_seconds"],
            )
            buckets.append(DrawdownBucket(config=bucket_config))

        return cls(buckets=buckets, max_open_positions=max_open_positions)

    def update(self, current_equity: Decimal, timestamp: Optional[datetime] = None) -> bool:
        """
        Update all buckets with new equity value.

        Returns True if trading is allowed, False if any kill-switch is active.
        """
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)

        self.last_update = timestamp

        # First, check if any cooldowns have expired
        for bucket in self.buckets:
            if bucket.check_cooldown_expired():
                # A cooldown expired - recalculate overall state
                pass

        # Update all buckets
        all_ok = True
        triggered_level = None

        for bucket in self.buckets:
            if not bucket.add_sample(current_equity, timestamp):
                all_ok = False
                if triggered_level is None:
                    triggered_level = bucket.level
                    self.total_trigger_count += 1

        # Update overall state
        self._update_trading_state()

        return self.is_trading_allowed

    def _update_trading_state(self):
        """Update overall trading allowed state based on all buckets."""
        # Check if any bucket is in cooldown
        for bucket in self.buckets:
            if bucket.is_in_cooldown:
                self.is_trading_allowed = False
                self.active_kill_switch = bucket.level
                return

        # No active kill-switches
        self.is_trading_allowed = True
        self.active_kill_switch = None

    def can_trade(self) -> bool:
        """Check if trading is currently allowed."""
        self._update_trading_state()
        return self.is_trading_allowed

    def can_open_position(self, current_position_count: int) -> bool:
        """Check if a new position can be opened."""
        if not self.can_trade():
            return False
        return current_position_count < self.max_open_positions

    def get_active_bucket(self) -> Optional[DrawdownBucket]:
        """Get the bucket that's currently blocking trading, if any."""
        for bucket in self.buckets:
            if bucket.is_in_cooldown:
                return bucket
        return None

    def get_most_stressed_bucket(self) -> Optional[DrawdownBucket]:
        """Get the bucket closest to its limit (highest % of limit used)."""
        if not self.buckets:
            return None

        max_stress = Decimal(0)
        most_stressed = None

        for bucket in self.buckets:
            if bucket.config.max_drawdown_pct > 0:
                stress = bucket.current_drawdown_pct / bucket.config.max_drawdown_pct
                if stress > max_stress:
                    max_stress = stress
                    most_stressed = bucket

        return most_stressed

    def reset_daily(self, current_equity: Decimal):
        """Reset all daily counters (called at midnight UTC)."""
        for bucket in self.buckets:
            bucket.reset_daily(current_equity)
        self.total_trigger_count = 0
        logger.info("Temporal risk state daily reset completed")

    def force_reset(self, current_equity: Decimal):
        """Force reset all buckets (manual intervention only)."""
        for bucket in self.buckets:
            bucket.samples.clear()
            bucket.window_peak = current_equity
            bucket.current_equity = current_equity
            bucket.current_drawdown_pct = Decimal(0)
            bucket.is_triggered = False
            bucket.triggered_at = None
            bucket.cooldown_until = None

        self.is_trading_allowed = True
        self.active_kill_switch = None
        logger.warning("Temporal risk state FORCE RESET - all buckets cleared")

    def to_dict(self) -> Dict:
        """Convert to dictionary for API/logging."""
        return {
            "is_trading_allowed": self.is_trading_allowed,
            "active_kill_switch": self.active_kill_switch.value if self.active_kill_switch else None,
            "max_open_positions": self.max_open_positions,
            "total_trigger_count": self.total_trigger_count,
            "last_update": self.last_update.isoformat() if self.last_update else None,
            "buckets": [b.to_dict() for b in self.buckets],
        }

    def get_status_summary(self) -> str:
        """Get human-readable status summary."""
        if self.is_trading_allowed:
            stressed = self.get_most_stressed_bucket()
            if stressed:
                pct_used = (
                    stressed.current_drawdown_pct / stressed.config.max_drawdown_pct * 100
                )
                return (
                    f"Trading ALLOWED. "
                    f"Most stressed: {stressed.level.value} at {pct_used:.1f}% of limit"
                )
            return "Trading ALLOWED. All buckets nominal."

        active = self.get_active_bucket()
        if active:
            return (
                f"Trading BLOCKED by {active.level.value}. "
                f"Cooldown: {active.cooldown_remaining_seconds}s remaining"
            )
        return "Trading BLOCKED (unknown reason)"
