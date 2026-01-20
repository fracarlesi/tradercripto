"""
HLQuantBot Protection System
=============================

Modular protection system (inspired by Freqtrade) that automatically blocks
trading in adverse conditions. This is ADDITIVE to the Cooldown System.

Difference from Cooldown:
- Cooldown is REACTIVE: triggered after disaster (e.g., 3 consecutive stoplosses)
- Protections are PROACTIVE: prevent disaster by checking thresholds continuously

Available Protections:
- StoplossGuard: Block trading after X stoplosses in Y timeframe
- MaxDrawdownProtection: Block trading if drawdown exceeds threshold
- CooldownPeriodProtection: Enforce minimum time between trades
- LowPerformanceProtection: Block trading if win rate is too low

Configuration (trading.yaml):
    protections:
      - name: "StoplossGuard"
        lookback_period_min: 60
        stoploss_limit: 3
        stop_duration_min: 360

Author: HLQuantBot
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
import json
import logging

logger = logging.getLogger(__name__)


# =============================================================================
# Protection Result
# =============================================================================

@dataclass
class ProtectionResult:
    """Result of a protection check."""
    
    is_protected: bool  # True if trading should be blocked
    protection_name: str
    reason: Optional[str] = None
    protected_until: Optional[datetime] = None
    trigger_details: Optional[Dict[str, Any]] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "is_protected": self.is_protected,
            "protection_name": self.protection_name,
            "reason": self.reason,
            "protected_until": self.protected_until.isoformat() if self.protected_until else None,
            "trigger_details": self.trigger_details,
        }


# =============================================================================
# Base Protection Class
# =============================================================================

class Protection(ABC):
    """
    Base class for all protections.
    
    Subclasses must implement the check() method that evaluates whether
    trading should be blocked based on specific conditions.
    """
    
    def __init__(self, config: Dict[str, Any]) -> None:
        """
        Initialize protection with config.
        
        Args:
            config: Protection-specific configuration dictionary
        """
        self.config = config
        self.name = self.__class__.__name__
        self._logger = logging.getLogger(f"hlquantbot.protections.{self.name}")
    
    @abstractmethod
    async def check(self, db: Any, telegram: Any) -> ProtectionResult:
        """
        Check if this protection should trigger.
        
        Args:
            db: Database instance for querying trade history
            telegram: TelegramService for sending alerts
        
        Returns:
            ProtectionResult with is_protected=True if trading should be blocked
        """
        pass
    
    async def _get_active_protection(self, db: Any) -> Optional[ProtectionResult]:
        """
        Check if protection is already active from previous trigger.
        
        Args:
            db: Database instance
        
        Returns:
            ProtectionResult if active, None otherwise
        """
        try:
            row = await db.fetchrow(
                """
                SELECT protected_until, trigger_details
                FROM protections
                WHERE protection_name = $1
                AND protected_until > NOW()
                ORDER BY created_at DESC
                LIMIT 1
                """,
                self.name
            )
            
            if row:
                details = row["trigger_details"]
                if isinstance(details, str):
                    details = json.loads(details)
                
                return ProtectionResult(
                    is_protected=True,
                    protection_name=self.name,
                    reason="Active from previous trigger",
                    protected_until=row["protected_until"],
                    trigger_details=details or {},
                )
        except Exception as e:
            self._logger.warning(
                "Error checking active protection: %s", e
            )
        
        return None
    
    async def _save_protection(
        self,
        db: Any,
        protected_until: datetime,
        details: Dict[str, Any]
    ) -> None:
        """
        Save protection trigger to database.
        
        Args:
            db: Database instance
            protected_until: When the protection expires
            details: Trigger details to store
        """
        try:
            await db.execute(
                """
                INSERT INTO protections (protection_name, protected_until, trigger_details)
                VALUES ($1, $2, $3)
                """,
                self.name,
                protected_until,
                json.dumps(details)
            )
            self._logger.info(
                "Protection %s saved until %s",
                self.name,
                protected_until.isoformat()
            )
        except Exception as e:
            self._logger.error("Error saving protection: %s", e)


# =============================================================================
# Stoploss Guard Protection
# =============================================================================

class StoplossGuard(Protection):
    """
    Blocks trading after X stoplosses in Y timeframe.
    
    Configuration:
        {
            "name": "StoplossGuard",
            "lookback_period_min": 60,      # Check last 60 minutes
            "stoploss_limit": 3,            # If 3+ stoplosses...
            "stop_duration_min": 360        # ...block trading for 6 hours
        }
    
    Triggers: If 3+ stoploss within 1h, block trading for 6h
    """
    
    async def check(self, db: Any, telegram: Any) -> ProtectionResult:
        """Check for excessive stoplosses in lookback period."""
        lookback_min = self.config.get("lookback_period_min", 60)
        sl_limit = self.config.get("stoploss_limit", 3)
        stop_duration_min = self.config.get("stop_duration_min", 360)
        
        # Check existing protection
        existing = await self._get_active_protection(db)
        if existing:
            return existing
        
        # Count recent stoplosses
        lookback_time = datetime.now(timezone.utc) - timedelta(minutes=lookback_min)
        
        try:
            # Query trades table for stoploss exits
            # Match various stoploss indicators in exit_reason or notes
            rows = await db.fetch(
                """
                SELECT COUNT(*) as sl_count
                FROM trades
                WHERE (
                    LOWER(exit_reason) LIKE '%stop%'
                    OR LOWER(exit_reason) LIKE '%sl%'
                    OR LOWER(notes) LIKE '%stop_loss%'
                    OR LOWER(notes) LIKE '%sl %'
                    OR LOWER(notes) LIKE '%stoploss%'
                )
                AND exit_time >= $1
                AND exit_time IS NOT NULL
                """,
                lookback_time
            )
            
            sl_count = rows[0]["sl_count"] if rows else 0
            
        except Exception as e:
            self._logger.warning("Error counting stoplosses: %s", e)
            return ProtectionResult(is_protected=False, protection_name=self.name)
        
        if sl_count >= sl_limit:
            # Trigger protection
            protected_until = datetime.now(timezone.utc) + timedelta(minutes=stop_duration_min)
            
            trigger_details = {
                "stoploss_count": sl_count,
                "lookback_period_min": lookback_min,
                "threshold": sl_limit,
            }
            
            await self._save_protection(db, protected_until, trigger_details)
            
            # Send Telegram alert
            if telegram:
                try:
                    await telegram.send_custom_alert(
                        f"*STOPLOSS GUARD TRIGGERED*\n"
                        f"Detected {sl_count} stoplosses in last {lookback_min} minutes\n"
                        f"Trading blocked until {protected_until.strftime('%Y-%m-%d %H:%M UTC')}",
                        emoji="kill_switch"
                    )
                except Exception as e:
                    self._logger.warning("Failed to send Telegram alert: %s", e)
            
            self._logger.warning(
                "StoplossGuard triggered: %d stoplosses in %d min, blocked for %d min",
                sl_count, lookback_min, stop_duration_min
            )
            
            return ProtectionResult(
                is_protected=True,
                protection_name=self.name,
                reason=f"{sl_count} stoplosses in {lookback_min} minutes",
                protected_until=protected_until,
                trigger_details=trigger_details,
            )
        
        return ProtectionResult(is_protected=False, protection_name=self.name)


# =============================================================================
# Max Drawdown Protection
# =============================================================================

class MaxDrawdownProtection(Protection):
    """
    Blocks trading if drawdown exceeds threshold in timeframe.
    
    Configuration:
        {
            "name": "MaxDrawdown",
            "lookback_period_min": 1440,    # Check last 24 hours
            "max_drawdown_pct": 5.0,        # If drawdown > 5%...
            "stop_duration_min": 720        # ...block trading for 12 hours
        }
    """
    
    async def check(self, db: Any, telegram: Any) -> ProtectionResult:
        """Check for excessive drawdown in lookback period."""
        lookback_min = self.config.get("lookback_period_min", 1440)
        max_dd_pct = Decimal(str(self.config.get("max_drawdown_pct", 5.0)))
        stop_duration_min = self.config.get("stop_duration_min", 720)
        
        # Check existing protection
        existing = await self._get_active_protection(db)
        if existing:
            return existing
        
        # Calculate drawdown in lookback period from equity snapshots
        lookback_time = datetime.now(timezone.utc) - timedelta(minutes=lookback_min)
        
        try:
            # Try to get equity history from equity_snapshots table
            rows = await db.fetch(
                """
                SELECT equity, timestamp
                FROM equity_snapshots
                WHERE timestamp >= $1
                ORDER BY timestamp ASC
                """,
                lookback_time
            )
            
            if not rows or len(rows) < 2:
                # Fallback: get current equity from live_account
                account = await db.fetchrow(
                    "SELECT equity FROM live_account WHERE id = 1"
                )
                if not account:
                    return ProtectionResult(is_protected=False, protection_name=self.name)
                
                # Without equity history, we can't calculate drawdown
                return ProtectionResult(is_protected=False, protection_name=self.name)
            
            equities = [Decimal(str(row["equity"])) for row in rows]
            
        except Exception as e:
            self._logger.warning("Error fetching equity data: %s", e)
            return ProtectionResult(is_protected=False, protection_name=self.name)
        
        # Calculate max drawdown
        peak = equities[0]
        max_dd = Decimal("0")
        
        for equity in equities:
            if equity > peak:
                peak = equity
            
            if peak > 0:
                dd = ((peak - equity) / peak) * 100
                if dd > max_dd:
                    max_dd = dd
        
        current = equities[-1]
        current_dd = ((peak - current) / peak * 100) if peak > 0 else Decimal("0")
        
        if current_dd > max_dd_pct:
            # Trigger protection
            protected_until = datetime.now(timezone.utc) + timedelta(minutes=stop_duration_min)
            
            trigger_details = {
                "drawdown_pct": float(current_dd),
                "max_drawdown_pct_threshold": float(max_dd_pct),
                "peak_equity": float(peak),
                "current_equity": float(current),
            }
            
            await self._save_protection(db, protected_until, trigger_details)
            
            # Send Telegram alert
            if telegram:
                try:
                    await telegram.send_custom_alert(
                        f"*MAX DRAWDOWN PROTECTION TRIGGERED*\n"
                        f"Drawdown: {current_dd:.2f}% (threshold: {max_dd_pct}%)\n"
                        f"Peak: ${peak:.2f} -> Current: ${current:.2f}\n"
                        f"Trading blocked until {protected_until.strftime('%Y-%m-%d %H:%M UTC')}",
                        emoji="kill_switch"
                    )
                except Exception as e:
                    self._logger.warning("Failed to send Telegram alert: %s", e)
            
            self._logger.warning(
                "MaxDrawdownProtection triggered: %.2f%% DD (threshold: %.2f%%)",
                current_dd, max_dd_pct
            )
            
            return ProtectionResult(
                is_protected=True,
                protection_name=self.name,
                reason=f"Drawdown {current_dd:.2f}% exceeds {max_dd_pct}%",
                protected_until=protected_until,
                trigger_details=trigger_details,
            )
        
        return ProtectionResult(is_protected=False, protection_name=self.name)


# =============================================================================
# Cooldown Period Protection
# =============================================================================

class CooldownPeriodProtection(Protection):
    """
    Enforces minimum time between trades.
    
    Configuration:
        {
            "name": "CooldownPeriod",
            "cooldown_minutes": 5
        }
    
    Note: This is different from the RiskManager cooldown system.
    This is a simple timing constraint, while RiskManager cooldown
    is triggered by adverse conditions.
    """
    
    async def check(self, db: Any, telegram: Any) -> ProtectionResult:
        """Check if minimum time has passed since last trade."""
        cooldown_min = self.config.get("cooldown_minutes", 5)
        
        try:
            # Get last trade entry time
            row = await db.fetchrow(
                """
                SELECT entry_time
                FROM trades
                WHERE status IN ('open', 'closed')
                AND entry_time IS NOT NULL
                ORDER BY entry_time DESC
                LIMIT 1
                """
            )
            
            if not row or not row["entry_time"]:
                return ProtectionResult(is_protected=False, protection_name=self.name)
            
            last_trade_time = row["entry_time"]
            
            # Ensure timezone awareness
            if last_trade_time.tzinfo is None:
                last_trade_time = last_trade_time.replace(tzinfo=timezone.utc)
            
            time_since_last = (datetime.now(timezone.utc) - last_trade_time).total_seconds() / 60
            
        except Exception as e:
            self._logger.warning("Error checking last trade time: %s", e)
            return ProtectionResult(is_protected=False, protection_name=self.name)
        
        if time_since_last < cooldown_min:
            protected_until = last_trade_time + timedelta(minutes=cooldown_min)
            
            return ProtectionResult(
                is_protected=True,
                protection_name=self.name,
                reason=f"Cooldown: {time_since_last:.1f}/{cooldown_min} minutes elapsed",
                protected_until=protected_until,
                trigger_details={
                    "time_since_last_min": round(time_since_last, 2),
                    "cooldown_minutes": cooldown_min,
                    "last_trade_time": last_trade_time.isoformat(),
                },
            )
        
        return ProtectionResult(is_protected=False, protection_name=self.name)


# =============================================================================
# Low Performance Protection
# =============================================================================

class LowPerformanceProtection(Protection):
    """
    Blocks trading if win rate is too low on recent trades.
    
    Configuration:
        {
            "name": "LowPerformance",
            "min_trades": 20,           # Need at least 20 trades
            "min_win_rate": 0.30,       # If win rate < 30%...
            "stop_duration_min": 1440   # ...block trading for 24 hours
        }
    """
    
    async def check(self, db: Any, telegram: Any) -> ProtectionResult:
        """Check if recent performance is acceptable."""
        min_trades = self.config.get("min_trades", 20)
        min_win_rate = Decimal(str(self.config.get("min_win_rate", 0.30)))
        stop_duration_min = self.config.get("stop_duration_min", 1440)
        
        # Check existing protection
        existing = await self._get_active_protection(db)
        if existing:
            return existing
        
        try:
            # Get recent closed trades
            rows = await db.fetch(
                """
                SELECT net_pnl
                FROM trades
                WHERE status = 'closed'
                AND net_pnl IS NOT NULL
                ORDER BY exit_time DESC
                LIMIT $1
                """,
                min_trades
            )
            
            if len(rows) < min_trades:
                # Not enough trades to evaluate
                return ProtectionResult(is_protected=False, protection_name=self.name)
            
        except Exception as e:
            self._logger.warning("Error fetching recent trades: %s", e)
            return ProtectionResult(is_protected=False, protection_name=self.name)
        
        # Calculate win rate
        winning_trades = len([r for r in rows if Decimal(str(r["net_pnl"])) > 0])
        total_trades = len(rows)
        win_rate = Decimal(winning_trades) / Decimal(total_trades)
        
        if win_rate < min_win_rate:
            # Trigger protection
            protected_until = datetime.now(timezone.utc) + timedelta(minutes=stop_duration_min)
            
            trigger_details = {
                "win_rate": float(win_rate),
                "min_win_rate_threshold": float(min_win_rate),
                "winning_trades": winning_trades,
                "total_trades": total_trades,
            }
            
            await self._save_protection(db, protected_until, trigger_details)
            
            # Send Telegram alert
            if telegram:
                try:
                    await telegram.send_custom_alert(
                        f"*LOW PERFORMANCE PROTECTION TRIGGERED*\n"
                        f"Win rate: {win_rate:.1%} (threshold: {min_win_rate:.1%})\n"
                        f"Wins: {winning_trades}/{total_trades} trades\n"
                        f"Trading blocked until {protected_until.strftime('%Y-%m-%d %H:%M UTC')}",
                        emoji="kill_switch"
                    )
                except Exception as e:
                    self._logger.warning("Failed to send Telegram alert: %s", e)
            
            self._logger.warning(
                "LowPerformanceProtection triggered: %.1f%% win rate (threshold: %.1f%%)",
                win_rate * 100, min_win_rate * 100
            )
            
            return ProtectionResult(
                is_protected=True,
                protection_name=self.name,
                reason=f"Win rate {win_rate:.1%} below {min_win_rate:.1%}",
                protected_until=protected_until,
                trigger_details=trigger_details,
            )
        
        return ProtectionResult(is_protected=False, protection_name=self.name)


# =============================================================================
# Protection Manager
# =============================================================================

class ProtectionManager:
    """
    Manages multiple protections and checks them all.
    
    Usage:
        manager = ProtectionManager(config, db, telegram)
        can_trade, result = await manager.check_all_protections()
        
        if not can_trade:
            logger.warning(f"Trading blocked by {result.protection_name}")
    """
    
    # Mapping of protection names to classes
    PROTECTION_CLASSES = {
        "StoplossGuard": StoplossGuard,
        "MaxDrawdown": MaxDrawdownProtection,
        "CooldownPeriod": CooldownPeriodProtection,
        "LowPerformance": LowPerformanceProtection,
    }
    
    def __init__(
        self,
        config: Dict[str, Any],
        db: Any,
        telegram: Any = None,
    ) -> None:
        """
        Initialize ProtectionManager.
        
        Args:
            config: Full trading config dict (expects 'protections' key)
            db: Database instance
            telegram: TelegramService instance (optional)
        """
        self.config = config
        self.db = db
        self.telegram = telegram
        self.protections: List[Protection] = []
        self._logger = logging.getLogger("hlquantbot.protections.manager")
        
        # Initialize protections from config
        self._init_protections()
    
    def _init_protections(self) -> None:
        """Initialize protection instances from config."""
        protections_config = self.config.get("protections", [])
        
        for prot_conf in protections_config:
            name = prot_conf.get("name")
            
            if not name:
                self._logger.warning("Protection config missing 'name' field")
                continue
            
            prot_class = self.PROTECTION_CLASSES.get(name)
            
            if prot_class:
                try:
                    protection = prot_class(prot_conf)
                    self.protections.append(protection)
                    self._logger.info(
                        "Initialized protection: %s",
                        name
                    )
                except Exception as e:
                    self._logger.error(
                        "Failed to initialize protection %s: %s",
                        name, e
                    )
            else:
                self._logger.warning("Unknown protection type: %s", name)
        
        self._logger.info(
            "ProtectionManager initialized with %d protections",
            len(self.protections)
        )
    
    async def check_all_protections(self) -> Tuple[bool, Optional[ProtectionResult]]:
        """
        Check all protections sequentially.
        
        Returns:
            (can_trade, protection_result)
            can_trade=False if any protection is active
        """
        for protection in self.protections:
            try:
                result = await protection.check(self.db, self.telegram)
                
                if result.is_protected:
                    self._logger.warning(
                        "Trading blocked by %s: %s",
                        result.protection_name,
                        result.reason
                    )
                    return False, result
                    
            except Exception as e:
                self._logger.error(
                    "Error checking protection %s: %s",
                    protection.name, e
                )
                # Don't block trading on protection check errors
                continue
        
        return True, None
    
    async def get_active_protections(self) -> List[ProtectionResult]:
        """
        Get all currently active protections.
        
        Returns:
            List of active ProtectionResult objects
        """
        active = []
        
        for protection in self.protections:
            try:
                result = await protection.check(self.db, self.telegram)
                if result.is_protected:
                    active.append(result)
            except Exception as e:
                self._logger.error(
                    "Error checking protection %s: %s",
                    protection.name, e
                )
        
        return active
    
    async def clear_protection(self, protection_name: str) -> bool:
        """
        Manually clear a protection (admin override).
        
        Args:
            protection_name: Name of protection to clear
        
        Returns:
            True if cleared successfully
        """
        try:
            await self.db.execute(
                """
                UPDATE protections
                SET protected_until = NOW()
                WHERE protection_name = $1
                AND protected_until > NOW()
                """,
                protection_name
            )
            self._logger.info("Protection %s cleared manually", protection_name)
            return True
        except Exception as e:
            self._logger.error("Error clearing protection: %s", e)
            return False
    
    @property
    def protection_names(self) -> List[str]:
        """Get list of configured protection names."""
        return [p.name for p in self.protections]
    
    @property
    def stats(self) -> Dict[str, Any]:
        """Get protection manager statistics."""
        return {
            "configured_protections": len(self.protections),
            "protection_names": self.protection_names,
        }

    async def health_check(self) -> Dict[str, Any]:
        """
        Health check for monitoring system.

        Returns:
            Dictionary with health status and protection info
        """
        return {
            "status": "healthy",
            "protections_count": len(self.protections),
            "protections": self.protection_names,
        }
