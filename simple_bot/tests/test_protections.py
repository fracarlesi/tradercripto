"""
Tests for HLQuantBot Protection System
=======================================

Unit tests for the modular protection system that blocks trading
in adverse conditions.

Run:
    pytest simple_bot/tests/test_protections.py -v

"""

import asyncio
import json
import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from simple_bot.services.protections import (
    Protection,
    ProtectionResult,
    StoplossGuard,
    MaxDrawdownProtection,
    CooldownPeriodProtection,
    LowPerformanceProtection,
    ProtectionManager,
)


# =============================================================================
# Test Fixtures
# =============================================================================

@pytest.fixture
def mock_db():
    """Create a mock database."""
    db = AsyncMock()
    db.fetch = AsyncMock(return_value=[])
    db.fetchrow = AsyncMock(return_value=None)
    db.execute = AsyncMock()
    return db


@pytest.fixture
def mock_telegram():
    """Create a mock telegram service."""
    telegram = AsyncMock()
    telegram.send_custom_alert = AsyncMock()
    return telegram


# =============================================================================
# ProtectionResult Tests
# =============================================================================

class TestProtectionResult:
    """Tests for ProtectionResult dataclass."""

    def test_protection_result_not_protected(self):
        """Test inactive protection result."""
        result = ProtectionResult(
            is_protected=False,
            protection_name="StoplossGuard"
        )
        assert result.is_protected is False
        assert result.protection_name == "StoplossGuard"
        assert result.reason is None
        assert result.protected_until is None

    def test_protection_result_protected(self):
        """Test active protection result."""
        until = datetime.now(timezone.utc) + timedelta(hours=6)
        result = ProtectionResult(
            is_protected=True,
            protection_name="StoplossGuard",
            reason="3 stoplosses in 60 minutes",
            protected_until=until,
            trigger_details={"stoploss_count": 3}
        )
        
        assert result.is_protected is True
        assert result.reason == "3 stoplosses in 60 minutes"
        assert result.protected_until == until
        assert result.trigger_details["stoploss_count"] == 3

    def test_protection_result_to_dict(self):
        """Test serialization to dict."""
        until = datetime.now(timezone.utc) + timedelta(hours=6)
        result = ProtectionResult(
            is_protected=True,
            protection_name="MaxDrawdownProtection",
            reason="Drawdown 7.5% exceeds 5.0%",
            protected_until=until,
            trigger_details={"drawdown_pct": 7.5}
        )
        
        data = result.to_dict()
        assert data["is_protected"] is True
        assert data["protection_name"] == "MaxDrawdownProtection"
        assert data["reason"] == "Drawdown 7.5% exceeds 5.0%"
        assert data["trigger_details"]["drawdown_pct"] == 7.5


# =============================================================================
# StoplossGuard Tests
# =============================================================================

class TestStoplossGuard:
    """Tests for StoplossGuard protection."""

    @pytest.fixture
    def stoploss_guard(self):
        """Create StoplossGuard with default config."""
        config = {
            "name": "StoplossGuard",
            "lookback_period_min": 60,
            "stoploss_limit": 3,
            "stop_duration_min": 360,
        }
        return StoplossGuard(config)

    @pytest.mark.asyncio
    async def test_no_protection_on_zero_stoplosses(self, stoploss_guard, mock_db, mock_telegram):
        """No protection when no recent stoplosses."""
        mock_db.fetch.return_value = [{"sl_count": 0}]
        
        result = await stoploss_guard.check(mock_db, mock_telegram)
        
        assert result.is_protected is False
        mock_telegram.send_custom_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_protection_on_two_stoplosses(self, stoploss_guard, mock_db, mock_telegram):
        """No protection with only 2 stoplosses (threshold is 3)."""
        mock_db.fetch.return_value = [{"sl_count": 2}]
        
        result = await stoploss_guard.check(mock_db, mock_telegram)
        
        assert result.is_protected is False

    @pytest.mark.asyncio
    async def test_protection_triggers_on_three_stoplosses(self, stoploss_guard, mock_db, mock_telegram):
        """Protection triggers on 3+ stoplosses."""
        # No existing protection
        mock_db.fetchrow.return_value = None
        # 3 stoplosses found
        mock_db.fetch.return_value = [{"sl_count": 3}]
        
        result = await stoploss_guard.check(mock_db, mock_telegram)
        
        assert result.is_protected is True
        assert result.protection_name == "StoplossGuard"
        assert "3" in result.reason
        assert result.trigger_details["stoploss_count"] == 3
        
        # Should save protection to DB
        mock_db.execute.assert_called_once()
        
        # Should send Telegram alert
        mock_telegram.send_custom_alert.assert_called_once()

    @pytest.mark.asyncio
    async def test_existing_protection_returned(self, stoploss_guard, mock_db, mock_telegram):
        """Returns existing active protection."""
        until = datetime.now(timezone.utc) + timedelta(hours=4)
        mock_db.fetchrow.return_value = {
            "protected_until": until,
            "trigger_details": json.dumps({"stoploss_count": 3}),
        }
        
        result = await stoploss_guard.check(mock_db, mock_telegram)
        
        assert result.is_protected is True
        assert result.reason == "Active from previous trigger"
        # Should not query for new stoplosses or send new alert
        mock_telegram.send_custom_alert.assert_not_called()


# =============================================================================
# MaxDrawdownProtection Tests
# =============================================================================

class TestMaxDrawdownProtection:
    """Tests for MaxDrawdownProtection."""

    @pytest.fixture
    def drawdown_protection(self):
        """Create MaxDrawdownProtection with default config."""
        config = {
            "name": "MaxDrawdown",
            "lookback_period_min": 1440,  # 24h
            "max_drawdown_pct": 5.0,
            "stop_duration_min": 720,
        }
        return MaxDrawdownProtection(config)

    @pytest.mark.asyncio
    async def test_no_protection_under_threshold(self, drawdown_protection, mock_db, mock_telegram):
        """No protection when drawdown < 5%."""
        # No existing protection
        mock_db.fetchrow.return_value = None
        # Equity history with 3% drawdown (peak 100, current 97)
        mock_db.fetch.return_value = [
            {"equity": Decimal("100"), "timestamp": datetime.now(timezone.utc) - timedelta(hours=12)},
            {"equity": Decimal("98"), "timestamp": datetime.now(timezone.utc) - timedelta(hours=6)},
            {"equity": Decimal("97"), "timestamp": datetime.now(timezone.utc)},
        ]
        
        result = await drawdown_protection.check(mock_db, mock_telegram)
        
        assert result.is_protected is False

    @pytest.mark.asyncio
    async def test_protection_triggers_over_threshold(self, drawdown_protection, mock_db, mock_telegram):
        """Protection triggers when drawdown > 5%."""
        # No existing protection
        mock_db.fetchrow.return_value = None
        # Equity history with 7% drawdown (peak 100, current 93)
        mock_db.fetch.return_value = [
            {"equity": Decimal("100"), "timestamp": datetime.now(timezone.utc) - timedelta(hours=12)},
            {"equity": Decimal("95"), "timestamp": datetime.now(timezone.utc) - timedelta(hours=6)},
            {"equity": Decimal("93"), "timestamp": datetime.now(timezone.utc)},
        ]
        
        result = await drawdown_protection.check(mock_db, mock_telegram)
        
        assert result.is_protected is True
        assert result.protection_name == "MaxDrawdownProtection"
        assert "7.00%" in result.reason or "7.0%" in result.reason
        assert result.trigger_details["drawdown_pct"] == 7.0
        
        # Should send alert
        mock_telegram.send_custom_alert.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_protection_insufficient_data(self, drawdown_protection, mock_db, mock_telegram):
        """No protection when insufficient equity data."""
        mock_db.fetchrow.return_value = None
        mock_db.fetch.return_value = []  # No equity history
        
        result = await drawdown_protection.check(mock_db, mock_telegram)
        
        assert result.is_protected is False


# =============================================================================
# CooldownPeriodProtection Tests
# =============================================================================

class TestCooldownPeriodProtection:
    """Tests for CooldownPeriodProtection."""

    @pytest.fixture
    def cooldown_protection(self):
        """Create CooldownPeriodProtection with default config."""
        config = {
            "name": "CooldownPeriod",
            "cooldown_minutes": 5,
        }
        return CooldownPeriodProtection(config)

    @pytest.mark.asyncio
    async def test_no_protection_no_trades(self, cooldown_protection, mock_db, mock_telegram):
        """No protection when no previous trades."""
        mock_db.fetchrow.return_value = None
        
        result = await cooldown_protection.check(mock_db, mock_telegram)
        
        assert result.is_protected is False

    @pytest.mark.asyncio
    async def test_protection_within_cooldown(self, cooldown_protection, mock_db, mock_telegram):
        """Protection active when last trade was within cooldown period."""
        # Last trade 3 minutes ago (cooldown is 5 min)
        last_trade_time = datetime.now(timezone.utc) - timedelta(minutes=3)
        mock_db.fetchrow.return_value = {"entry_time": last_trade_time}
        
        result = await cooldown_protection.check(mock_db, mock_telegram)
        
        assert result.is_protected is True
        assert "3.0/5" in result.reason or "Cooldown" in result.reason
        assert result.trigger_details["cooldown_minutes"] == 5

    @pytest.mark.asyncio
    async def test_no_protection_after_cooldown(self, cooldown_protection, mock_db, mock_telegram):
        """No protection when cooldown has elapsed."""
        # Last trade 10 minutes ago (cooldown is 5 min)
        last_trade_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        mock_db.fetchrow.return_value = {"entry_time": last_trade_time}
        
        result = await cooldown_protection.check(mock_db, mock_telegram)
        
        assert result.is_protected is False


# =============================================================================
# LowPerformanceProtection Tests
# =============================================================================

class TestLowPerformanceProtection:
    """Tests for LowPerformanceProtection."""

    @pytest.fixture
    def low_performance_protection(self):
        """Create LowPerformanceProtection with default config."""
        config = {
            "name": "LowPerformance",
            "min_trades": 20,
            "min_win_rate": 0.30,
            "stop_duration_min": 1440,
        }
        return LowPerformanceProtection(config)

    @pytest.mark.asyncio
    async def test_no_protection_insufficient_trades(self, low_performance_protection, mock_db, mock_telegram):
        """No protection when less than min_trades."""
        mock_db.fetchrow.return_value = None
        # Only 10 trades (need 20)
        mock_db.fetch.return_value = [{"net_pnl": Decimal("-1")} for _ in range(10)]
        
        result = await low_performance_protection.check(mock_db, mock_telegram)
        
        assert result.is_protected is False

    @pytest.mark.asyncio
    async def test_no_protection_good_win_rate(self, low_performance_protection, mock_db, mock_telegram):
        """No protection when win rate >= 30%."""
        mock_db.fetchrow.return_value = None
        # 8 wins, 12 losses = 40% win rate (above 30%)
        trades = [{"net_pnl": Decimal("10")} for _ in range(8)]
        trades.extend([{"net_pnl": Decimal("-5")} for _ in range(12)])
        mock_db.fetch.return_value = trades
        
        result = await low_performance_protection.check(mock_db, mock_telegram)
        
        assert result.is_protected is False

    @pytest.mark.asyncio
    async def test_protection_triggers_low_win_rate(self, low_performance_protection, mock_db, mock_telegram):
        """Protection triggers when win rate < 30%."""
        mock_db.fetchrow.return_value = None
        # 4 wins, 16 losses = 20% win rate (below 30%)
        trades = [{"net_pnl": Decimal("10")} for _ in range(4)]
        trades.extend([{"net_pnl": Decimal("-5")} for _ in range(16)])
        mock_db.fetch.return_value = trades
        
        result = await low_performance_protection.check(mock_db, mock_telegram)
        
        assert result.is_protected is True
        assert result.protection_name == "LowPerformanceProtection"
        assert result.trigger_details["win_rate"] == 0.2
        assert result.trigger_details["winning_trades"] == 4
        assert result.trigger_details["total_trades"] == 20
        
        # Should send alert
        mock_telegram.send_custom_alert.assert_called_once()

    @pytest.mark.asyncio
    async def test_edge_case_exactly_threshold(self, low_performance_protection, mock_db, mock_telegram):
        """No protection at exactly 30% (threshold is <30%)."""
        mock_db.fetchrow.return_value = None
        # 6 wins, 14 losses = 30% win rate (exactly at threshold)
        trades = [{"net_pnl": Decimal("10")} for _ in range(6)]
        trades.extend([{"net_pnl": Decimal("-5")} for _ in range(14)])
        mock_db.fetch.return_value = trades
        
        result = await low_performance_protection.check(mock_db, mock_telegram)
        
        # 30% is NOT below 30%, so no protection
        assert result.is_protected is False


# =============================================================================
# ProtectionManager Tests
# =============================================================================

class TestProtectionManager:
    """Tests for ProtectionManager."""

    @pytest.fixture
    def manager_config(self):
        """Create config with all protections."""
        return {
            "protections": [
                {
                    "name": "StoplossGuard",
                    "lookback_period_min": 60,
                    "stoploss_limit": 3,
                    "stop_duration_min": 360,
                },
                {
                    "name": "MaxDrawdown",
                    "lookback_period_min": 1440,
                    "max_drawdown_pct": 5.0,
                    "stop_duration_min": 720,
                },
                {
                    "name": "CooldownPeriod",
                    "cooldown_minutes": 5,
                },
                {
                    "name": "LowPerformance",
                    "min_trades": 20,
                    "min_win_rate": 0.30,
                    "stop_duration_min": 1440,
                },
            ]
        }

    def test_initialization(self, manager_config, mock_db, mock_telegram):
        """Test manager initializes all protections."""
        manager = ProtectionManager(manager_config, mock_db, mock_telegram)
        
        assert len(manager.protections) == 4
        assert "StoplossGuard" in manager.protection_names
        assert "MaxDrawdownProtection" in manager.protection_names
        assert "CooldownPeriodProtection" in manager.protection_names
        assert "LowPerformanceProtection" in manager.protection_names

    def test_initialization_empty_config(self, mock_db, mock_telegram):
        """Test manager handles empty config."""
        manager = ProtectionManager({}, mock_db, mock_telegram)
        
        assert len(manager.protections) == 0

    def test_initialization_unknown_protection(self, mock_db, mock_telegram):
        """Test manager handles unknown protection name."""
        config = {
            "protections": [
                {"name": "UnknownProtection", "some_param": 123},
            ]
        }
        manager = ProtectionManager(config, mock_db, mock_telegram)
        
        # Unknown protection should be skipped
        assert len(manager.protections) == 0

    @pytest.mark.asyncio
    async def test_check_all_no_protections_active(self, manager_config, mock_db, mock_telegram):
        """Test check_all when no protections are triggered."""
        manager = ProtectionManager(manager_config, mock_db, mock_telegram)
        
        # Mock all protections returning not protected
        mock_db.fetchrow.return_value = None
        mock_db.fetch.side_effect = [
            [{"sl_count": 0}],  # StoplossGuard - no stoplosses
            [],  # MaxDrawdown - no equity data
            [],  # LowPerformance - no trades
        ]
        
        can_trade, result = await manager.check_all_protections()
        
        assert can_trade is True
        assert result is None

    @pytest.mark.asyncio
    async def test_check_all_one_protection_active(self, mock_db, mock_telegram):
        """Test check_all stops at first active protection."""
        # Only StoplossGuard configured
        config = {
            "protections": [
                {
                    "name": "StoplossGuard",
                    "lookback_period_min": 60,
                    "stoploss_limit": 3,
                    "stop_duration_min": 360,
                },
            ]
        }
        manager = ProtectionManager(config, mock_db, mock_telegram)
        
        # StoplossGuard triggers
        mock_db.fetchrow.return_value = None
        mock_db.fetch.return_value = [{"sl_count": 5}]
        
        can_trade, result = await manager.check_all_protections()
        
        assert can_trade is False
        assert result is not None
        assert result.protection_name == "StoplossGuard"

    @pytest.mark.asyncio
    async def test_get_active_protections(self, mock_db, mock_telegram):
        """Test getting all active protections."""
        config = {
            "protections": [
                {
                    "name": "StoplossGuard",
                    "lookback_period_min": 60,
                    "stoploss_limit": 3,
                    "stop_duration_min": 360,
                },
                {
                    "name": "CooldownPeriod",
                    "cooldown_minutes": 5,
                },
            ]
        }
        manager = ProtectionManager(config, mock_db, mock_telegram)
        
        # StoplossGuard triggers
        mock_db.fetchrow.return_value = None
        mock_db.fetch.return_value = [{"sl_count": 5}]
        
        active = await manager.get_active_protections()
        
        assert len(active) >= 1
        names = [p.protection_name for p in active]
        assert "StoplossGuard" in names

    @pytest.mark.asyncio
    async def test_clear_protection(self, manager_config, mock_db, mock_telegram):
        """Test manually clearing a protection."""
        manager = ProtectionManager(manager_config, mock_db, mock_telegram)
        
        success = await manager.clear_protection("StoplossGuard")
        
        assert success is True
        mock_db.execute.assert_called_once()

    def test_stats_property(self, manager_config, mock_db, mock_telegram):
        """Test stats property."""
        manager = ProtectionManager(manager_config, mock_db, mock_telegram)
        
        stats = manager.stats
        
        assert stats["configured_protections"] == 4
        assert len(stats["protection_names"]) == 4


# =============================================================================
# Protection Expiration Tests
# =============================================================================

class TestProtectionExpiration:
    """Tests for protection expiration logic."""

    @pytest.mark.asyncio
    async def test_protection_expires_correctly(self, mock_db, mock_telegram):
        """Protection expires after duration."""
        config = {
            "name": "StoplossGuard",
            "lookback_period_min": 60,
            "stoploss_limit": 3,
            "stop_duration_min": 1,  # 1 minute duration
        }
        guard = StoplossGuard(config)
        
        # Protection was triggered but expired 1 minute ago
        expired_time = datetime.now(timezone.utc) - timedelta(minutes=1)
        mock_db.fetchrow.return_value = {
            "protected_until": expired_time,
            "trigger_details": json.dumps({"stoploss_count": 3}),
        }
        
        # Since protected_until is in the past, it should return None
        # (the query has WHERE protected_until > NOW())
        mock_db.fetchrow.return_value = None
        mock_db.fetch.return_value = [{"sl_count": 0}]
        
        result = await guard.check(mock_db, mock_telegram)
        
        assert result.is_protected is False


# =============================================================================
# Integration Tests
# =============================================================================

class TestProtectionIntegration:
    """Integration tests for protection system."""

    @pytest.mark.asyncio
    async def test_multiple_protections_first_wins(self, mock_db, mock_telegram):
        """First triggered protection blocks further checks."""
        config = {
            "protections": [
                {
                    "name": "CooldownPeriod",
                    "cooldown_minutes": 5,
                },
                {
                    "name": "StoplossGuard",
                    "lookback_period_min": 60,
                    "stoploss_limit": 3,
                    "stop_duration_min": 360,
                },
            ]
        }
        manager = ProtectionManager(config, mock_db, mock_telegram)
        
        # CooldownPeriod triggers (last trade 2 min ago)
        last_trade_time = datetime.now(timezone.utc) - timedelta(minutes=2)
        mock_db.fetchrow.return_value = {"entry_time": last_trade_time}
        
        can_trade, result = await manager.check_all_protections()
        
        assert can_trade is False
        assert result.protection_name == "CooldownPeriodProtection"
        # StoplossGuard should not be checked since CooldownPeriod triggered first

    @pytest.mark.asyncio
    async def test_db_error_does_not_block_trading(self, mock_db, mock_telegram):
        """Database errors should not block trading."""
        config = {
            "protections": [
                {
                    "name": "StoplossGuard",
                    "lookback_period_min": 60,
                    "stoploss_limit": 3,
                    "stop_duration_min": 360,
                },
            ]
        }
        manager = ProtectionManager(config, mock_db, mock_telegram)
        
        # Database throws exception
        mock_db.fetch.side_effect = Exception("Database connection error")
        
        can_trade, result = await manager.check_all_protections()
        
        # Should allow trading on errors (fail-open)
        assert can_trade is True
        assert result is None
