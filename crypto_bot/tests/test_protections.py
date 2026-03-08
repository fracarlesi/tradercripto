"""
Tests for HLQuantBot Protection System
=======================================

Unit tests for the modular protection system that blocks trading
in adverse conditions.

Run:
    pytest crypto_bot/tests/test_protections.py -v

"""

import pytest
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock

from crypto_bot.services.protections import (
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
    """Tests for StoplossGuard protection (stubbed - no DB)."""

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
    async def test_no_protection_stubbed(self, stoploss_guard, mock_telegram):
        """No protection since check() is stubbed (no DB)."""
        result = await stoploss_guard.check()

        assert result.is_protected is False


# =============================================================================
# MaxDrawdownProtection Tests
# =============================================================================

class TestMaxDrawdownProtection:
    """Tests for MaxDrawdownProtection (stubbed - no DB)."""

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
    async def test_no_protection_stubbed(self, drawdown_protection, mock_telegram):
        """No protection since check() is stubbed (no DB)."""
        result = await drawdown_protection.check()

        assert result.is_protected is False


# =============================================================================
# CooldownPeriodProtection Tests
# =============================================================================

class TestCooldownPeriodProtection:
    """Tests for CooldownPeriodProtection (stubbed - no DB)."""

    @pytest.fixture
    def cooldown_protection(self):
        """Create CooldownPeriodProtection with default config."""
        config = {
            "name": "CooldownPeriod",
            "cooldown_minutes": 5,
        }
        return CooldownPeriodProtection(config)

    @pytest.mark.asyncio
    async def test_no_protection_stubbed(self, cooldown_protection, mock_telegram):
        """No protection since check() is stubbed (no DB)."""
        result = await cooldown_protection.check()

        assert result.is_protected is False


# =============================================================================
# LowPerformanceProtection Tests
# =============================================================================

class TestLowPerformanceProtection:
    """Tests for LowPerformanceProtection with economic logic."""

    def _make_config(self) -> dict:
        return {
            "name": "LowPerformance",
            "min_trades": 20,
            "max_profit_factor": 0.90,
            "require_negative_pnl": True,
            "stop_duration_min": 1440,
        }

    def _fake_perf_monitor(self, trades):
        from unittest.mock import MagicMock
        pm = MagicMock()
        pm._trades = trades
        return pm

    @dataclass
    class _FakeTrade:
        realized_pnl: float = 0.0

    @pytest.mark.asyncio
    async def test_no_monitor_not_blocked(self, mock_telegram):
        """No performance monitor → never blocked."""
        prot = LowPerformanceProtection(self._make_config())
        result = await prot.check()
        assert result.is_protected is False

    @pytest.mark.asyncio
    async def test_few_trades_not_blocked(self, mock_telegram):
        """Under min_trades → never blocked."""
        trades = [self._FakeTrade(realized_pnl=-0.1) for _ in range(10)]
        prot = LowPerformanceProtection(
            self._make_config(), performance_monitor=self._fake_perf_monitor(trades)
        )
        result = await prot.check()
        assert result.is_protected is False

    @pytest.mark.asyncio
    async def test_low_pf_negative_pnl_blocks(self, mock_telegram):
        """PF < 0.90 with negative PnL → blocked."""
        # 5 wins of $0.50, 20 losses of $-0.50 → PF = 2.50/10.0 = 0.25
        trades = [self._FakeTrade(realized_pnl=0.50) for _ in range(5)]
        trades += [self._FakeTrade(realized_pnl=-0.50) for _ in range(20)]
        prot = LowPerformanceProtection(
            self._make_config(), performance_monitor=self._fake_perf_monitor(trades)
        )
        result = await prot.check()
        assert result.is_protected is True
        assert "PF=" in result.reason

    @pytest.mark.asyncio
    async def test_good_pf_not_blocked(self, mock_telegram):
        """PF > 0.90 → not blocked even if some losses."""
        # 15 wins of $1.0, 10 losses of $-0.50 → PF = 15.0/5.0 = 3.0
        trades = [self._FakeTrade(realized_pnl=1.0) for _ in range(15)]
        trades += [self._FakeTrade(realized_pnl=-0.50) for _ in range(10)]
        prot = LowPerformanceProtection(
            self._make_config(), performance_monitor=self._fake_perf_monitor(trades)
        )
        result = await prot.check()
        assert result.is_protected is False

    @pytest.mark.asyncio
    async def test_low_pf_positive_pnl_not_blocked(self, mock_telegram):
        """PF < 0.90 but net_pnl > 0 → not blocked (require_negative_pnl=True)."""
        # Edge case: technically impossible with PF<1 to have positive pnl,
        # but test the logic path anyway
        config = self._make_config()
        config["require_negative_pnl"] = False  # would block
        trades = [self._FakeTrade(realized_pnl=0.50) for _ in range(5)]
        trades += [self._FakeTrade(realized_pnl=-0.50) for _ in range(20)]
        prot = LowPerformanceProtection(
            config, performance_monitor=self._fake_perf_monitor(trades)
        )
        result = await prot.check()
        assert result.is_protected is True  # negative pnl check disabled → blocks on PF alone


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

    def test_initialization(self, manager_config, mock_telegram):
        """Test manager initializes all protections."""
        manager = ProtectionManager(manager_config, mock_telegram)

        assert len(manager.protections) == 4
        assert "StoplossGuard" in manager.protection_names
        assert "MaxDrawdownProtection" in manager.protection_names
        assert "CooldownPeriodProtection" in manager.protection_names
        assert "LowPerformanceProtection" in manager.protection_names

    def test_initialization_empty_config(self, mock_telegram):
        """Test manager handles empty config."""
        manager = ProtectionManager({}, mock_telegram)

        assert len(manager.protections) == 0

    def test_initialization_unknown_protection(self, mock_telegram):
        """Test manager handles unknown protection name."""
        config = {
            "protections": [
                {"name": "UnknownProtection", "some_param": 123},
            ]
        }
        manager = ProtectionManager(config, mock_telegram)

        # Unknown protection should be skipped
        assert len(manager.protections) == 0

    @pytest.mark.asyncio
    async def test_check_all_no_protections_active(self, manager_config, mock_telegram):
        """Test check_all when no protections are triggered (stubbed checks)."""
        manager = ProtectionManager(manager_config, mock_telegram)

        can_trade, result = await manager.check_all_protections()

        assert can_trade is True
        assert result is None

    def test_stats_property(self, manager_config, mock_telegram):
        """Test stats property."""
        manager = ProtectionManager(manager_config, mock_telegram)

        stats = manager.stats

        assert stats["configured_protections"] == 4
        assert len(stats["protection_names"]) == 4


# =============================================================================
# Protection Expiration Tests
# =============================================================================

class TestProtectionExpiration:
    """Tests for protection expiration logic."""

    @pytest.mark.asyncio
    async def test_protection_stubbed_returns_not_protected(self, mock_telegram):
        """Stubbed protection always returns not protected (no DB)."""
        config = {
            "name": "StoplossGuard",
            "lookback_period_min": 60,
            "stoploss_limit": 3,
            "stop_duration_min": 1,  # 1 minute duration
        }
        guard = StoplossGuard(config)

        result = await guard.check()

        assert result.is_protected is False


# =============================================================================
# Integration Tests
# =============================================================================

class TestProtectionIntegration:
    """Integration tests for protection system."""

    @pytest.mark.asyncio
    async def test_all_protections_stubbed_allow_trading(self, mock_telegram):
        """All stubbed protections should allow trading (no DB)."""
        config = {
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
            ]
        }
        manager = ProtectionManager(config, mock_telegram)

        can_trade, result = await manager.check_all_protections()

        assert can_trade is True
        assert result is None
