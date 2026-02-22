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

from .base import HealthStatus, ServiceStatus

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
        """Check for excessive stoplosses. Stubbed - no trades table."""
        # Check existing protection from protections table
        existing = await self._get_active_protection(db)
        if existing:
            return existing

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
        """Check for excessive drawdown. Stubbed - no equity_snapshots table."""
        # Check existing protection from protections table
        existing = await self._get_active_protection(db)
        if existing:
            return existing

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
        """Check if minimum time has passed since last trade. Stubbed - no trades table."""
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
        """Check if recent performance is acceptable. Stubbed - no trades table."""
        # Check existing protection from protections table
        existing = await self._get_active_protection(db)
        if existing:
            return existing

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

    async def health_check(self) -> HealthStatus:
        """
        Health check for monitoring system.

        Returns:
            HealthStatus with check results
        """
        return HealthStatus(
            healthy=True,
            status=ServiceStatus.RUNNING,
            message="OK",
            details={
                "protections_count": len(self.protections),
                "protections": self.protection_names,
            },
        )
