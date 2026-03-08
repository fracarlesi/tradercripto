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
    async def check(self) -> ProtectionResult:
        """
        Check if this protection should trigger.

        Returns:
            ProtectionResult with is_protected=True if trading should be blocked
        """
        pass


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
    
    async def check(self) -> ProtectionResult:
        """Check for excessive stoplosses. Stubbed - no DB."""
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
    
    async def check(self) -> ProtectionResult:
        """Check for excessive drawdown. Stubbed - no DB."""
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
    
    async def check(self) -> ProtectionResult:
        """Check if minimum time has passed since last trade. Stubbed - no DB."""
        return ProtectionResult(is_protected=False, protection_name=self.name)


# =============================================================================
# Low Performance Protection
# =============================================================================

class LowPerformanceProtection(Protection):
    """
    Blocks trading when economic performance is poor.

    Uses profit factor (gross_wins / gross_losses) rather than win rate,
    because a strategy can have low win rate but high R:R and still be profitable.

    Configuration:
        {
            "name": "LowPerformance",
            "min_trades": 20,               # Need 20+ trades before judging
            "max_profit_factor": 0.90,      # Block if PF < 0.90
            "require_negative_pnl": true,   # Only block if net_pnl < 0
            "stop_duration_min": 1440       # Pause 24h
        }
    """

    def __init__(self, config: Dict[str, Any], performance_monitor: Any = None) -> None:
        super().__init__(config)
        self._perf_monitor = performance_monitor
        self._blocked_until: Optional[datetime] = None

    async def check(self) -> ProtectionResult:
        """Check if recent performance warrants a trading pause."""
        now = datetime.now(timezone.utc)

        # If currently blocked, check expiry
        if self._blocked_until and now < self._blocked_until:
            remaining = (self._blocked_until - now).total_seconds() / 60
            return ProtectionResult(
                is_protected=True,
                protection_name=self.name,
                reason=f"Low performance pause ({remaining:.0f}min remaining)",
                protected_until=self._blocked_until,
            )
        self._blocked_until = None

        # No performance monitor → can't evaluate
        if not self._perf_monitor:
            return ProtectionResult(is_protected=False, protection_name=self.name)

        trades = getattr(self._perf_monitor, "_trades", [])
        min_trades = self.config.get("min_trades", 20)
        if len(trades) < min_trades:
            return ProtectionResult(is_protected=False, protection_name=self.name)

        # Calculate economic metrics
        gross_wins = sum(t.realized_pnl for t in trades if t.realized_pnl > 0)
        gross_losses = abs(sum(t.realized_pnl for t in trades if t.realized_pnl < 0))
        net_pnl = sum(t.realized_pnl for t in trades)

        profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf")

        max_pf = self.config.get("max_profit_factor", 0.90)
        require_neg = self.config.get("require_negative_pnl", True)

        if profit_factor < max_pf and (not require_neg or net_pnl < 0):
            duration_min = self.config.get("stop_duration_min", 1440)
            self._blocked_until = now + timedelta(minutes=duration_min)
            reason = (
                f"PF={profit_factor:.2f} < {max_pf} with net_pnl=${net_pnl:.2f} "
                f"over {len(trades)} trades → pausing {duration_min}min"
            )
            self._logger.warning("LowPerformance triggered: %s", reason)
            return ProtectionResult(
                is_protected=True,
                protection_name=self.name,
                reason=reason,
                protected_until=self._blocked_until,
            )

        return ProtectionResult(is_protected=False, protection_name=self.name)


# =============================================================================
# Protection Manager
# =============================================================================

class ProtectionManager:
    """
    Manages multiple protections and checks them all.
    
    Usage:
        manager = ProtectionManager(config, telegram)
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
        telegram: Any = None,
        performance_monitor: Any = None,
    ) -> None:
        """
        Initialize ProtectionManager.

        Args:
            config: Full trading config dict (expects 'protections' key)
            telegram: TelegramService instance (optional)
            performance_monitor: PerformanceMonitorService for LowPerformance checks
        """
        self.config = config
        self.telegram = telegram
        self._performance_monitor = performance_monitor
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
                    if prot_class is LowPerformanceProtection:
                        protection = prot_class(prot_conf, performance_monitor=self._performance_monitor)
                    else:
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
                result = await protection.check()

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
                result = await protection.check()
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
        self._logger.info("Protection %s cleared manually", protection_name)
        return True
    
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
