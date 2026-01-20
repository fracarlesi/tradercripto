"""
Tests for HLQuantBot Cooldown System
=====================================

Unit tests for the cooldown system that pauses trading after loss streaks.

Run:
    pytest simple_bot/tests/test_cooldown.py -v

"""

import asyncio
import json
import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from simple_bot.core.models import CooldownState, CooldownReason
from simple_bot.services.risk_manager import RiskManagerService, RiskConfig


# =============================================================================
# Test Fixtures
# =============================================================================

@pytest.fixture
def mock_db():
    """Create a mock database."""
    db = AsyncMock()
    db.fetch = AsyncMock(return_value=[])
    db.fetchrow = AsyncMock(return_value=None)
    return db


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
def risk_manager(mock_db, mock_client, mock_telegram):
    """Create a RiskManagerService instance for testing."""
    rm = RiskManagerService(
        name="test_risk_manager",
        bus=None,
        db=mock_db,
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
    async def test_no_cooldown_on_zero_losses(self, risk_manager, mock_db):
        """No cooldown when no recent trades."""
        mock_db.fetch.return_value = []
        
        is_cooldown, state = await risk_manager.check_cooldown_required()
        
        assert is_cooldown is False
        assert state is None

    @pytest.mark.asyncio
    async def test_no_cooldown_on_two_stoplosses(self, risk_manager, mock_db):
        """No cooldown with only 2 consecutive stoplosses."""
        now = datetime.now(timezone.utc)
        # Return 2 stoplosses for 1h check (not enough for SL streak)
        # Return small losses for 24h check (not enough for DD or low perf)
        mock_db.fetch.side_effect = [
            # 1h window - only 2 stoplosses
            [
                {"trade_id": "1", "net_pnl": Decimal("-1"), "exit_time": now, "notes": "stop_loss"},
                {"trade_id": "2", "net_pnl": Decimal("-1"), "exit_time": now - timedelta(minutes=30), "notes": "sl triggered"},
            ],
            # 24h window - same trades, small loss (2% DD)
            [
                {"trade_id": "1", "net_pnl": Decimal("-1"), "exit_time": now, "notes": "stop_loss"},
                {"trade_id": "2", "net_pnl": Decimal("-1"), "exit_time": now - timedelta(minutes=30), "notes": "sl triggered"},
            ],
        ]
        
        is_cooldown, state = await risk_manager.check_cooldown_required()
        
        assert is_cooldown is False
        assert state is None

    @pytest.mark.asyncio
    async def test_cooldown_on_three_stoplosses(self, risk_manager, mock_db, mock_telegram):
        """6h cooldown triggered on 3 consecutive stoplosses."""
        now = datetime.now(timezone.utc)
        mock_db.fetch.return_value = [
            {"trade_id": "1", "net_pnl": -10, "exit_time": now, "notes": "stop_loss"},
            {"trade_id": "2", "net_pnl": -15, "exit_time": now - timedelta(minutes=20), "notes": "sl hit"},
            {"trade_id": "3", "net_pnl": -8, "exit_time": now - timedelta(minutes=40), "notes": "stop triggered"},
        ]
        
        is_cooldown, state = await risk_manager.check_cooldown_required()
        
        assert is_cooldown is True
        assert state is not None
        assert state.reason == CooldownReason.STOPLOSS_STREAK
        assert state.trigger_details["consecutive_losses"] == 3
        
        # Should persist to DB
        mock_db.fetch.assert_called()
        
        # Should send Telegram alert
        mock_telegram.send_custom_alert.assert_called_once()
        call_args = mock_telegram.send_custom_alert.call_args
        assert "COOLDOWN TRIGGERED" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_no_cooldown_mixed_exits(self, risk_manager, mock_db):
        """No cooldown when losses are not consecutive."""
        now = datetime.now(timezone.utc)
        mock_db.fetch.return_value = [
            {"trade_id": "1", "net_pnl": -10, "exit_time": now, "notes": "stop_loss"},
            {"trade_id": "2", "net_pnl": 20, "exit_time": now - timedelta(minutes=20), "notes": "take_profit"},
            {"trade_id": "3", "net_pnl": -8, "exit_time": now - timedelta(minutes=40), "notes": "stop_loss"},
        ]
        
        is_cooldown, state = await risk_manager.check_cooldown_required()
        
        assert is_cooldown is False


# =============================================================================
# Daily Drawdown Cooldown Tests
# =============================================================================

class TestDailyDrawdownCooldown:
    """Tests for daily drawdown cooldown trigger."""

    @pytest.mark.asyncio
    async def test_no_cooldown_under_5pct(self, risk_manager, mock_db):
        """No cooldown when DD < 5%."""
        now = datetime.now(timezone.utc)
        # $4 loss on $100 equity = 4% DD
        mock_db.fetch.return_value = [
            {"trade_id": "1", "net_pnl": -2, "exit_time": now, "notes": "tp"},
            {"trade_id": "2", "net_pnl": -2, "exit_time": now - timedelta(hours=2), "notes": "tp"},
        ]
        
        is_cooldown, state = await risk_manager.check_cooldown_required()
        
        assert is_cooldown is False

    @pytest.mark.asyncio
    async def test_cooldown_over_5pct(self, risk_manager, mock_db, mock_telegram):
        """12h cooldown when DD > 5%."""
        now = datetime.now(timezone.utc)
        # 3 stoploss-like trades but not consecutive (broke by time window)
        # $6 loss on $100 equity = 6% DD
        mock_db.fetch.side_effect = [
            # First call: 1 hour window for stoploss streak check
            [
                {"trade_id": "1", "net_pnl": -3, "exit_time": now, "notes": "take_profit"},
            ],
            # Second call: 24 hour window for DD check
            [
                {"trade_id": "1", "net_pnl": -3, "exit_time": now, "notes": "take_profit"},
                {"trade_id": "2", "net_pnl": -2, "exit_time": now - timedelta(hours=5), "notes": "take_profit"},
                {"trade_id": "3", "net_pnl": -2, "exit_time": now - timedelta(hours=10), "notes": "take_profit"},
            ],
        ]
        
        is_cooldown, state = await risk_manager.check_cooldown_required()
        
        assert is_cooldown is True
        assert state is not None
        assert state.reason == CooldownReason.DAILY_DRAWDOWN
        assert "drawdown_pct" in state.trigger_details

    @pytest.mark.asyncio
    async def test_no_cooldown_profit_day(self, risk_manager, mock_db):
        """No DD cooldown on profitable day."""
        now = datetime.now(timezone.utc)
        mock_db.fetch.return_value = [
            {"trade_id": "1", "net_pnl": 10, "exit_time": now, "notes": "tp"},
            {"trade_id": "2", "net_pnl": 5, "exit_time": now - timedelta(hours=2), "notes": "tp"},
        ]
        
        is_cooldown, state = await risk_manager.check_cooldown_required()
        
        assert is_cooldown is False


# =============================================================================
# Low Performance Cooldown Tests
# =============================================================================

class TestLowPerformanceCooldown:
    """Tests for low performance cooldown trigger."""

    @pytest.mark.asyncio
    async def test_no_cooldown_few_trades(self, risk_manager, mock_db):
        """No cooldown with < 5 trades (and losses are small)."""
        now = datetime.now(timezone.utc)
        mock_db.fetch.side_effect = [
            [],  # 1h window - no stoplosses
            [
                # 3 trades with small losses (3% DD, below 5% threshold)
                {"trade_id": "1", "net_pnl": Decimal("-1"), "exit_time": now, "notes": "tp"},
                {"trade_id": "2", "net_pnl": Decimal("-1"), "exit_time": now - timedelta(hours=5), "notes": "tp"},
                {"trade_id": "3", "net_pnl": Decimal("-1"), "exit_time": now - timedelta(hours=10), "notes": "tp"},
            ],  # Only 3 trades, not enough for low performance check
        ]
        
        is_cooldown, state = await risk_manager.check_cooldown_required()
        
        assert is_cooldown is False

    @pytest.mark.asyncio
    async def test_cooldown_low_win_rate(self, risk_manager, mock_db, mock_telegram):
        """24h cooldown when win rate < 20% on 5+ trades."""
        now = datetime.now(timezone.utc)
        # 0 wins, 5 losses = 0% win rate
        mock_db.fetch.side_effect = [
            [],  # 1h window - no stoploss streak
            [
                {"trade_id": "1", "net_pnl": -1, "exit_time": now, "notes": "tp"},
                {"trade_id": "2", "net_pnl": -1, "exit_time": now - timedelta(hours=4), "notes": "tp"},
                {"trade_id": "3", "net_pnl": -1, "exit_time": now - timedelta(hours=8), "notes": "tp"},
                {"trade_id": "4", "net_pnl": -1, "exit_time": now - timedelta(hours=12), "notes": "tp"},
                {"trade_id": "5", "net_pnl": -1, "exit_time": now - timedelta(hours=16), "notes": "tp"},
            ],  # 5 trades, 0% win rate (but only 5% DD, not enough for DD trigger)
        ]
        
        is_cooldown, state = await risk_manager.check_cooldown_required()
        
        assert is_cooldown is True
        assert state is not None
        assert state.reason == CooldownReason.LOW_PERFORMANCE
        assert state.trigger_details["win_rate"] == 0
        assert state.trigger_details["num_trades"] == 5

    @pytest.mark.asyncio
    async def test_no_cooldown_good_win_rate(self, risk_manager, mock_db):
        """No cooldown when win rate >= 20%."""
        now = datetime.now(timezone.utc)
        # 1 win, 4 losses = 20% win rate (exactly at threshold)
        mock_db.fetch.side_effect = [
            [],  # 1h window
            [
                {"trade_id": "1", "net_pnl": 5, "exit_time": now, "notes": "tp"},  # WIN
                {"trade_id": "2", "net_pnl": -1, "exit_time": now - timedelta(hours=4), "notes": "tp"},
                {"trade_id": "3", "net_pnl": -1, "exit_time": now - timedelta(hours=8), "notes": "tp"},
                {"trade_id": "4", "net_pnl": -1, "exit_time": now - timedelta(hours=12), "notes": "tp"},
                {"trade_id": "5", "net_pnl": -1, "exit_time": now - timedelta(hours=16), "notes": "tp"},
            ],  # 5 trades, 20% win rate
        ]
        
        is_cooldown, state = await risk_manager.check_cooldown_required()
        
        # 20% is exactly at threshold, should NOT trigger (< 20% is the condition)
        assert is_cooldown is False


# =============================================================================
# Cooldown Expiration Tests
# =============================================================================

class TestCooldownExpiration:
    """Tests for cooldown expiration logic."""

    @pytest.mark.asyncio
    async def test_cooldown_expires(self, risk_manager, mock_db, mock_telegram):
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
        
        mock_db.fetch.return_value = []  # No new issues
        
        is_cooldown, state = await risk_manager.check_cooldown_required()
        
        assert is_cooldown is False
        assert state is None
        assert risk_manager._cooldown_state is None  # Cleared
        
        # Should send "resumed" alert
        mock_telegram.send_custom_alert.assert_called()

    @pytest.mark.asyncio
    async def test_cooldown_still_active(self, risk_manager, mock_db):
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
    """Tests for cooldown database persistence."""

    @pytest.mark.asyncio
    async def test_load_active_cooldown(self, risk_manager, mock_db):
        """Load active cooldown from database on startup."""
        now = datetime.now(timezone.utc)
        mock_db.fetchrow.return_value = {
            "reason": "StoplossStreak",
            "triggered_at": now - timedelta(hours=2),
            "cooldown_until": now + timedelta(hours=4),
            "details": json.dumps({"consecutive_losses": 3}),
        }
        
        await risk_manager.load_active_cooldown()
        
        assert risk_manager._cooldown_state is not None
        assert risk_manager._cooldown_state.active is True
        assert risk_manager._cooldown_state.reason == CooldownReason.STOPLOSS_STREAK

    @pytest.mark.asyncio
    async def test_no_active_cooldown_in_db(self, risk_manager, mock_db):
        """No cooldown state when DB has no active cooldown."""
        mock_db.fetchrow.return_value = None
        
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
