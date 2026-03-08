"""
Tests for HLQuantBot Cooldown System
=====================================

Unit tests for the cooldown system that pauses trading after loss streaks.

Run:
    pytest crypto_bot/tests/test_cooldown.py -v

"""

import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

from crypto_bot.core.models import CooldownState, CooldownReason
from crypto_bot.services.risk_manager import RiskManagerService, RiskConfig


# =============================================================================
# Test Fixtures
# =============================================================================

@pytest.fixture
def mock_telegram():
    """Create a mock telegram service."""
    telegram = AsyncMock()
    telegram.send_custom_alert = AsyncMock()
    return telegram


@pytest.fixture
def mock_client():
    """Create a mock Hyperliquid client."""
    client = AsyncMock()
    client.get_account_state = AsyncMock(return_value={"equity": 100.0})
    client.get_positions = AsyncMock(return_value=[])
    return client


@pytest.fixture
def risk_manager(mock_client, mock_telegram):
    """Create a RiskManagerService instance for testing."""
    rm = RiskManagerService(
        name="test_risk_manager",
        bus=None,
        config=RiskConfig(),
        client=mock_client,
        telegram=mock_telegram,
    )
    rm._current_equity = Decimal("100")
    return rm


# =============================================================================
# CooldownState Model Tests
# =============================================================================

class TestCooldownStateModel:
    """Tests for CooldownState Pydantic model."""

    def test_cooldown_state_inactive(self):
        """Test inactive cooldown state."""
        state = CooldownState(active=False)
        assert state.active is False
        assert state.reason is None
        assert state.is_expired() is True
        assert state.time_remaining() is None

    def test_cooldown_state_active(self):
        """Test active cooldown state."""
        now = datetime.now(timezone.utc)
        until = now + timedelta(hours=6)
        
        state = CooldownState(
            active=True,
            reason=CooldownReason.STOPLOSS_STREAK,
            triggered_at=now,
            cooldown_until=until,
            trigger_details={"consecutive_losses": 3}
        )
        
        assert state.active is True
        assert state.reason == CooldownReason.STOPLOSS_STREAK
        assert state.is_expired() is False
        
        remaining = state.time_remaining()
        assert remaining is not None
        assert remaining > 0
        assert remaining <= 6 * 3600  # 6 hours in seconds

    def test_cooldown_state_expired(self):
        """Test expired cooldown state."""
        now = datetime.now(timezone.utc)
        until = now - timedelta(hours=1)  # Expired 1 hour ago
        
        state = CooldownState(
            active=True,
            reason=CooldownReason.DAILY_DRAWDOWN,
            triggered_at=now - timedelta(hours=13),
            cooldown_until=until,
            trigger_details={"drawdown_pct": 5.5}
        )
        
        assert state.active is True  # Still marked as active
        assert state.is_expired() is True  # But logically expired
        assert state.time_remaining() == 0


# =============================================================================
# Stoploss Streak Cooldown Tests
# =============================================================================

class TestStoplossStreakCooldown:
    """Tests for stoploss streak cooldown trigger."""

    @pytest.mark.asyncio
    async def test_no_cooldown_on_zero_losses(self, risk_manager):
        """No cooldown when _get_recent_trades returns empty (stubbed)."""
        is_cooldown, state = await risk_manager.check_cooldown_required()

        assert is_cooldown is False
        assert state is None

    @pytest.mark.asyncio
    async def test_no_cooldown_stubbed(self, risk_manager):
        """No cooldown since _get_recent_trades always returns []."""
        is_cooldown, state = await risk_manager.check_cooldown_required()

        assert is_cooldown is False
        assert state is None


class TestCountConsecutiveStoplosses:
    """Unit tests for _count_consecutive_stoplosses logic.

    Validates that only stoploss exits with negative PnL count toward
    the consecutive loss streak that triggers cooldown.
    """

    def test_three_sl_negative_pnl_triggers(self, risk_manager):
        """3 consecutive SL with negative PnL → count = 3 (cooldown triggers)."""
        trades = [
            {"notes": "stop_loss", "net_pnl": -0.67},
            {"notes": "stop_loss", "net_pnl": -0.65},
            {"notes": "stop_loss", "net_pnl": -0.14},
        ]
        assert risk_manager._count_consecutive_stoplosses(trades) == 3

    def test_sl_breakeven_breaks_streak(self, risk_manager):
        """2 SL negative + 1 SL breakeven/profit → count = 0 (breakeven is most recent)."""
        trades = [
            {"notes": "stop_loss", "net_pnl": 0.006},   # breakeven SL (most recent)
            {"notes": "stop_loss", "net_pnl": -0.65},
            {"notes": "stop_loss", "net_pnl": -0.14},
        ]
        # Breakeven SL is most recent → breaks the streak immediately
        assert risk_manager._count_consecutive_stoplosses(trades) == 0

    def test_sl_profit_does_not_count(self, risk_manager):
        """SL with positive PnL (moved to profit) should not count."""
        trades = [
            {"notes": "stop_loss", "net_pnl": 0.50},    # SL in profit
            {"notes": "stop_loss", "net_pnl": -0.65},
        ]
        assert risk_manager._count_consecutive_stoplosses(trades) == 0

    def test_non_sl_exit_breaks_streak(self, risk_manager):
        """Non-SL exit reason breaks the streak."""
        trades = [
            {"notes": "stop_loss", "net_pnl": -0.67},
            {"notes": "take_profit", "net_pnl": 1.30},  # TP breaks streak
            {"notes": "stop_loss", "net_pnl": -0.14},
        ]
        assert risk_manager._count_consecutive_stoplosses(trades) == 1

    def test_mixed_sl_negative_then_breakeven(self, risk_manager):
        """2 SL losses then 1 SL breakeven → count = 2."""
        trades = [
            {"notes": "stop_loss", "net_pnl": -0.67},
            {"notes": "stop_loss", "net_pnl": -0.65},
            {"notes": "stop_loss", "net_pnl": 0.01},    # breakeven, breaks streak
        ]
        assert risk_manager._count_consecutive_stoplosses(trades) == 2

    def test_empty_trades(self, risk_manager):
        """Empty trade list → count = 0."""
        assert risk_manager._count_consecutive_stoplosses([]) == 0

    def test_none_pnl_treated_as_zero(self, risk_manager):
        """Trade with None PnL should not count as loss."""
        trades = [
            {"notes": "stop_loss", "net_pnl": None},
            {"notes": "stop_loss", "net_pnl": -0.50},
        ]
        # None → 0 → not < 0 → breaks streak
        assert risk_manager._count_consecutive_stoplosses(trades) == 0


# =============================================================================
# Daily Drawdown Cooldown Tests
# =============================================================================

class TestDailyDrawdownCooldown:
    """Tests for daily drawdown cooldown trigger.

    Note: _get_recent_trades is stubbed, so DD-based cooldown never triggers.
    """

    @pytest.mark.asyncio
    async def test_no_cooldown_stubbed(self, risk_manager):
        """No DD cooldown since _get_recent_trades returns []."""
        is_cooldown, state = await risk_manager.check_cooldown_required()

        assert is_cooldown is False


# =============================================================================
# Low Performance Cooldown Tests
# =============================================================================

class TestLowPerformanceCooldown:
    """Tests for low performance cooldown trigger.

    Note: _get_recent_trades is stubbed, so low-perf cooldown never triggers.
    """

    @pytest.mark.asyncio
    async def test_no_cooldown_stubbed(self, risk_manager):
        """No low-perf cooldown since _get_recent_trades returns []."""
        is_cooldown, state = await risk_manager.check_cooldown_required()

        assert is_cooldown is False


# =============================================================================
# Cooldown Expiration Tests
# =============================================================================

class TestCooldownExpiration:
    """Tests for cooldown expiration logic."""

    @pytest.mark.asyncio
    async def test_cooldown_expires(self, risk_manager, mock_telegram):
        """Cooldown expires correctly after duration."""
        # Set up an expired cooldown
        now = datetime.now(timezone.utc)
        risk_manager._cooldown_state = CooldownState(
            active=True,
            reason=CooldownReason.STOPLOSS_STREAK,
            triggered_at=now - timedelta(hours=7),  # Started 7h ago
            cooldown_until=now - timedelta(hours=1),  # Expired 1h ago
            trigger_details={"consecutive_losses": 3}
        )

        is_cooldown, state = await risk_manager.check_cooldown_required()
        
        assert is_cooldown is False
        assert state is None
        assert risk_manager._cooldown_state is None  # Cleared
        
        # Should send "resumed" alert
        mock_telegram.send_custom_alert.assert_called()

    @pytest.mark.asyncio
    async def test_cooldown_still_active(self, risk_manager):
        """Cooldown remains active before expiration."""
        now = datetime.now(timezone.utc)
        risk_manager._cooldown_state = CooldownState(
            active=True,
            reason=CooldownReason.STOPLOSS_STREAK,
            triggered_at=now - timedelta(hours=2),  # Started 2h ago
            cooldown_until=now + timedelta(hours=4),  # Expires in 4h
            trigger_details={"consecutive_losses": 3}
        )
        
        is_cooldown, state = await risk_manager.check_cooldown_required()
        
        assert is_cooldown is True
        assert state is not None
        assert state.reason == CooldownReason.STOPLOSS_STREAK


# =============================================================================
# Cooldown Persistence Tests
# =============================================================================

class TestCooldownPersistence:
    """Tests for cooldown persistence (no DB, in-memory only)."""

    @pytest.mark.asyncio
    async def test_load_active_cooldown_noop_without_db(self, risk_manager):
        """load_active_cooldown is a no-op without DB."""
        await risk_manager.load_active_cooldown()
        assert risk_manager._cooldown_state is None

    @pytest.mark.asyncio
    async def test_is_cooldown_active_property(self, risk_manager):
        """Test is_cooldown_active() helper method."""
        # No cooldown
        assert risk_manager.is_cooldown_active() is False
        
        # Active cooldown
        now = datetime.now(timezone.utc)
        risk_manager._cooldown_state = CooldownState(
            active=True,
            reason=CooldownReason.DAILY_DRAWDOWN,
            triggered_at=now,
            cooldown_until=now + timedelta(hours=12),
            trigger_details={}
        )
        assert risk_manager.is_cooldown_active() is True
        
        # Expired cooldown
        risk_manager._cooldown_state.cooldown_until = now - timedelta(hours=1)
        assert risk_manager.is_cooldown_active() is False


# =============================================================================
# Integration Tests
# =============================================================================

class TestCooldownIntegration:
    """Integration tests for cooldown system."""

    @pytest.mark.asyncio
    async def test_metrics_include_cooldown(self, risk_manager):
        """Metrics include cooldown information."""
        now = datetime.now(timezone.utc)
        risk_manager._cooldown_state = CooldownState(
            active=True,
            reason=CooldownReason.STOPLOSS_STREAK,
            triggered_at=now,
            cooldown_until=now + timedelta(hours=6),
            trigger_details={"consecutive_losses": 3}
        )
        
        metrics = risk_manager.metrics
        
        assert "cooldown" in metrics
        assert metrics["cooldown"] is not None
        assert metrics["cooldown"]["active"] is True
        assert metrics["cooldown"]["reason"] == "StoplossStreak"

    def test_get_cooldown_state(self, risk_manager):
        """Test get_cooldown_state() method."""
        assert risk_manager.get_cooldown_state() is None
        
        now = datetime.now(timezone.utc)
        state = CooldownState(
            active=True,
            reason=CooldownReason.LOW_PERFORMANCE,
            triggered_at=now,
            cooldown_until=now + timedelta(hours=24),
            trigger_details={"win_rate": 0.1, "num_trades": 10}
        )
        risk_manager._cooldown_state = state
        
        assert risk_manager.get_cooldown_state() == state
