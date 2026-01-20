"""
Tests for HLQuantBot ROI Graduated Time-Based Feature
======================================================

Unit tests for the time-based ROI exit system.

The system checks if positions should exit based on graduated ROI targets:
- 0-30min: 3% profit target
- 30-60min: 2% profit target
- 1-2h: 1.5% profit target
- 2-4h: 1% profit target
- 4-8h: 0.5% profit target
- 8h+: Break-even (exit at any profit)

Run:
    pytest simple_bot/tests/test_roi_graduated.py -v

"""

import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from simple_bot.services.execution_engine import (
    ExecutionEngineService,
    ExecutionPosition,
    PositionStatus,
)


# =============================================================================
# Test Fixtures
# =============================================================================

@pytest.fixture
def roi_config():
    """Standard ROI configuration for testing."""
    return {
        "0": 0.03,      # 3% profit in first 30 min
        "30": 0.02,     # 2% profit after 30 min
        "60": 0.015,    # 1.5% profit after 1 hour
        "120": 0.01,    # 1% profit after 2 hours
        "240": 0.005,   # 0.5% profit after 4 hours
        "480": 0.0,     # Break-even after 8 hours
    }


@pytest.fixture
def mock_bus():
    """Create a mock message bus."""
    bus = AsyncMock()
    bus.subscribe = AsyncMock()
    bus.publish = AsyncMock()
    return bus


@pytest.fixture
def mock_client():
    """Create a mock Hyperliquid client."""
    client = AsyncMock()
    client.get_positions = AsyncMock(return_value=[])
    client.get_fills = AsyncMock(return_value=[])
    client.close_position = AsyncMock(return_value={"success": True})
    client.cancel_order = AsyncMock()
    return client


@pytest.fixture
def mock_config(roi_config):
    """Create a mock config with ROI settings."""
    class _ExecConfig:
        order_type = "limit"
        max_slippage_pct = 0.1
        limit_timeout_seconds = 60
        retry_attempts = 3
        retry_delay_seconds = 5
        position_sync_interval = 30
        fill_sync_interval = 10

    class _RiskConfig:
        take_profit_pct = 3.0
        stop_loss_pct = 2.0

    class _StopsConfig:
        def __init__(self, roi):
            self.initial_atr_mult = 2.5
            self.trailing_atr_mult = 2.5
            self.minimal_roi = roi

    class _ServicesConfig:
        def __init__(self):
            self.execution_engine = _ExecConfig()

    class _ConfigAdapter:
        def __init__(self, roi):
            self.services = _ServicesConfig()
            self.risk = _RiskConfig()
            self.stops = _StopsConfig(roi)

    return _ConfigAdapter(roi_config)


@pytest.fixture
def empty_roi_config():
    """Create a config with no ROI settings."""
    class _ExecConfig:
        order_type = "limit"
        max_slippage_pct = 0.1
        limit_timeout_seconds = 60
        retry_attempts = 3
        retry_delay_seconds = 5
        position_sync_interval = 30
        fill_sync_interval = 10

    class _RiskConfig:
        take_profit_pct = 3.0
        stop_loss_pct = 2.0

    class _StopsConfig:
        minimal_roi = {}

    class _ServicesConfig:
        def __init__(self):
            self.execution_engine = _ExecConfig()

    class _ConfigAdapter:
        def __init__(self):
            self.services = _ServicesConfig()
            self.risk = _RiskConfig()
            self.stops = _StopsConfig()

    return _ConfigAdapter()


@pytest.fixture
def execution_engine(mock_bus, mock_config, mock_client):
    """Create an ExecutionEngineService instance for testing."""
    engine = ExecutionEngineService(
        bus=mock_bus,
        config=mock_config,
        client=mock_client,
        db=None,
    )
    return engine


@pytest.fixture
def execution_engine_no_roi(mock_bus, empty_roi_config, mock_client):
    """Create an ExecutionEngineService with no ROI config."""
    engine = ExecutionEngineService(
        bus=mock_bus,
        config=empty_roi_config,
        client=mock_client,
        db=None,
    )
    return engine


def create_position(
    symbol: str = "BTC",
    side: str = "long",
    entry_price: float = 100.0,
    current_price: float = 103.0,
    opened_minutes_ago: int = 10,
    status: PositionStatus = PositionStatus.OPEN,
) -> ExecutionPosition:
    """Helper to create test positions."""
    opened_at = datetime.now(timezone.utc) - timedelta(minutes=opened_minutes_ago)
    
    return ExecutionPosition(
        symbol=symbol,
        side=side,
        size=1.0,
        entry_price=entry_price,
        current_price=current_price,
        unrealized_pnl=(current_price - entry_price) if side == "long" else (entry_price - current_price),
        realized_pnl=0.0,
        leverage=5,
        status=status,
        strategy="trend_follow",
        signal_id="test-signal-123",
        opened_at=opened_at,
    )


# =============================================================================
# ROI Target Calculation Tests
# =============================================================================

class TestROITargetCalculation:
    """Tests for ROI target calculation based on time."""

    @pytest.mark.asyncio
    async def test_roi_early_exit_3pct(self, execution_engine):
        """Trade at +3% profit after 10 minutes should exit."""
        # 3% profit after 10 minutes (target is 3% at t=0)
        position = create_position(
            entry_price=100.0,
            current_price=103.0,  # +3%
            opened_minutes_ago=10,
        )
        
        should_exit, current_roi, target_roi = await execution_engine.should_exit_on_roi(position)
        
        assert should_exit is True
        assert abs(current_roi - 0.03) < 0.001  # 3%
        assert abs(target_roi - 0.03) < 0.001  # Target is 3% at t=0

    @pytest.mark.asyncio
    async def test_roi_early_hold_2pct(self, execution_engine):
        """Trade at +2% profit after 10 minutes should NOT exit (target is 3%)."""
        # 2% profit after 10 minutes (target is 3% at t=0)
        position = create_position(
            entry_price=100.0,
            current_price=102.0,  # +2%
            opened_minutes_ago=10,
        )
        
        should_exit, current_roi, target_roi = await execution_engine.should_exit_on_roi(position)
        
        assert should_exit is False
        assert abs(current_roi - 0.02) < 0.001  # 2%
        assert abs(target_roi - 0.03) < 0.001  # Target is 3% at t=0

    @pytest.mark.asyncio
    async def test_roi_mid_exit_2pct_after_45min(self, execution_engine):
        """Trade at +2% profit after 45 minutes should exit (target is 2% at t=30)."""
        # 2% profit after 45 minutes (target is 2% at t=30)
        position = create_position(
            entry_price=100.0,
            current_price=102.0,  # +2%
            opened_minutes_ago=45,
        )
        
        should_exit, current_roi, target_roi = await execution_engine.should_exit_on_roi(position)
        
        assert should_exit is True
        assert abs(current_roi - 0.02) < 0.001  # 2%
        assert abs(target_roi - 0.02) < 0.001  # Target is 2% at t=30

    @pytest.mark.asyncio
    async def test_roi_mid_exit_1_5pct_after_90min(self, execution_engine):
        """Trade at +1.5% profit after 90 minutes should exit (target is 1.5% at t=60)."""
        # 1.5% profit after 90 minutes (target is 1.5% at t=60)
        position = create_position(
            entry_price=100.0,
            current_price=101.5,  # +1.5%
            opened_minutes_ago=90,
        )
        
        should_exit, current_roi, target_roi = await execution_engine.should_exit_on_roi(position)
        
        assert should_exit is True
        assert abs(current_roi - 0.015) < 0.001  # 1.5%
        assert abs(target_roi - 0.015) < 0.001  # Target is 1.5% at t=60

    @pytest.mark.asyncio
    async def test_roi_late_hold_1pct_after_3h(self, execution_engine):
        """Trade at +1% profit after 3 hours should exit (target is 1% at t=120)."""
        # 1% profit after 3 hours = 180 minutes (target is 1% at t=120)
        position = create_position(
            entry_price=100.0,
            current_price=101.0,  # +1%
            opened_minutes_ago=180,
        )
        
        should_exit, current_roi, target_roi = await execution_engine.should_exit_on_roi(position)
        
        assert should_exit is True
        assert abs(current_roi - 0.01) < 0.001  # 1%
        assert abs(target_roi - 0.01) < 0.001  # Target is 1% at t=120

    @pytest.mark.asyncio
    async def test_roi_breakeven_exit_after_9h(self, execution_engine):
        """Trade at +0.1% profit after 9 hours should exit (target is 0% at t=480)."""
        # 0.1% profit after 9 hours = 540 minutes (target is 0% at t=480)
        position = create_position(
            entry_price=100.0,
            current_price=100.1,  # +0.1%
            opened_minutes_ago=540,
        )
        
        should_exit, current_roi, target_roi = await execution_engine.should_exit_on_roi(position)
        
        assert should_exit is True
        assert abs(current_roi - 0.001) < 0.001  # 0.1%
        assert abs(target_roi - 0.0) < 0.001  # Target is 0% at t=480

    @pytest.mark.asyncio
    async def test_roi_hold_below_target(self, execution_engine):
        """Trade below target should not exit."""
        # 0.5% profit after 5 hours = 300 minutes (target is 0.5% at t=240)
        # But position is only at 0.3%, so should not exit
        position = create_position(
            entry_price=100.0,
            current_price=100.3,  # +0.3%
            opened_minutes_ago=300,
        )
        
        should_exit, current_roi, target_roi = await execution_engine.should_exit_on_roi(position)
        
        assert should_exit is False
        assert abs(current_roi - 0.003) < 0.001  # 0.3%
        assert abs(target_roi - 0.005) < 0.001  # Target is 0.5% at t=240


# =============================================================================
# Negative PnL Tests
# =============================================================================

class TestNegativePnL:
    """Tests for positions with negative PnL (losses)."""

    @pytest.mark.asyncio
    async def test_roi_negative_pnl_no_exit(self, execution_engine):
        """Trade at -2% loss should NOT exit via ROI."""
        position = create_position(
            entry_price=100.0,
            current_price=98.0,  # -2%
            opened_minutes_ago=60,
        )
        
        should_exit, current_roi, target_roi = await execution_engine.should_exit_on_roi(position)
        
        assert should_exit is False
        assert current_roi < 0  # Negative ROI

    @pytest.mark.asyncio
    async def test_roi_breakeven_negative_no_exit(self, execution_engine):
        """Trade at -0.5% loss after 9 hours should NOT exit (even with 0% target)."""
        # Negative profit should never trigger ROI exit
        position = create_position(
            entry_price=100.0,
            current_price=99.5,  # -0.5%
            opened_minutes_ago=540,  # 9 hours
        )
        
        should_exit, current_roi, target_roi = await execution_engine.should_exit_on_roi(position)
        
        assert should_exit is False
        assert current_roi < 0


# =============================================================================
# Short Position Tests
# =============================================================================

class TestShortPositions:
    """Tests for short position ROI calculation."""

    @pytest.mark.asyncio
    async def test_roi_short_position_profit(self, execution_engine):
        """Short position at +2% profit after 45 min should exit."""
        # Short: profit when price goes down
        position = create_position(
            side="short",
            entry_price=100.0,
            current_price=98.0,  # Price down = profit for short
            opened_minutes_ago=45,
        )
        
        should_exit, current_roi, target_roi = await execution_engine.should_exit_on_roi(position)
        
        assert should_exit is True
        assert abs(current_roi - 0.02) < 0.001  # 2% profit

    @pytest.mark.asyncio
    async def test_roi_short_position_loss(self, execution_engine):
        """Short position at -3% loss should NOT exit."""
        # Short: loss when price goes up
        position = create_position(
            side="short",
            entry_price=100.0,
            current_price=103.0,  # Price up = loss for short
            opened_minutes_ago=30,
        )
        
        should_exit, current_roi, target_roi = await execution_engine.should_exit_on_roi(position)
        
        assert should_exit is False
        assert current_roi < 0  # Negative ROI (loss)


# =============================================================================
# Edge Cases
# =============================================================================

class TestEdgeCases:
    """Edge case tests."""

    @pytest.mark.asyncio
    async def test_roi_no_config_no_exit(self, execution_engine_no_roi):
        """ROI disabled if no config - should never exit."""
        position = create_position(
            entry_price=100.0,
            current_price=110.0,  # +10%
            opened_minutes_ago=10,
        )
        
        should_exit, current_roi, target_roi = await execution_engine_no_roi.should_exit_on_roi(position)
        
        assert should_exit is False

    @pytest.mark.asyncio
    async def test_roi_no_opened_at(self, execution_engine):
        """Position without opened_at should not exit."""
        position = create_position(
            entry_price=100.0,
            current_price=110.0,  # +10%
            opened_minutes_ago=0,
        )
        position.opened_at = None  # Remove opened_at
        
        should_exit, current_roi, target_roi = await execution_engine.should_exit_on_roi(position)
        
        assert should_exit is False

    @pytest.mark.asyncio
    async def test_roi_zero_entry_price(self, execution_engine):
        """Position with zero entry price should not exit."""
        position = create_position(
            entry_price=0.0,  # Invalid
            current_price=100.0,
            opened_minutes_ago=10,
        )
        
        should_exit, current_roi, target_roi = await execution_engine.should_exit_on_roi(position)
        
        assert should_exit is False

    @pytest.mark.asyncio
    async def test_roi_exactly_at_threshold(self, execution_engine):
        """Position exactly at threshold boundary should use correct target."""
        # Exactly at 30 min boundary - should use 2% target
        position = create_position(
            entry_price=100.0,
            current_price=102.0,  # +2%
            opened_minutes_ago=30,  # Exactly at boundary
        )
        
        should_exit, current_roi, target_roi = await execution_engine.should_exit_on_roi(position)
        
        assert should_exit is True
        assert abs(target_roi - 0.02) < 0.001  # Target is 2% at t=30


# =============================================================================
# Integration Tests
# =============================================================================

class TestROIExitIntegration:
    """Integration tests for the full ROI exit flow."""

    @pytest.mark.asyncio
    async def test_check_roi_exits_closes_position(self, execution_engine, mock_client):
        """_check_roi_exits should close positions that meet ROI target."""
        # Add a position that should exit
        position = create_position(
            symbol="BTC",
            entry_price=100.0,
            current_price=103.0,  # +3%
            opened_minutes_ago=5,
        )
        execution_engine.active_positions["BTC"] = position
        
        # Run check
        await execution_engine._check_roi_exits()
        
        # Verify close was called
        mock_client.close_position.assert_called_once_with("BTC")
        
        # Verify exit reason was set
        assert position.exit_reason == "roi_target"

    @pytest.mark.asyncio
    async def test_check_roi_exits_skips_non_open(self, execution_engine, mock_client):
        """_check_roi_exits should skip positions that aren't OPEN."""
        # Add a position that's closing
        position = create_position(
            symbol="BTC",
            entry_price=100.0,
            current_price=103.0,  # +3%
            opened_minutes_ago=5,
            status=PositionStatus.CLOSING,
        )
        execution_engine.active_positions["BTC"] = position
        
        # Run check
        await execution_engine._check_roi_exits()
        
        # Verify close was NOT called
        mock_client.close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_roi_exits_handles_multiple_positions(self, execution_engine, mock_client):
        """_check_roi_exits should handle multiple positions correctly."""
        # Add positions - one should exit, one should not
        position_exit = create_position(
            symbol="BTC",
            entry_price=100.0,
            current_price=103.0,  # +3% - should exit
            opened_minutes_ago=5,
        )
        position_hold = create_position(
            symbol="ETH",
            entry_price=100.0,
            current_price=101.0,  # +1% - should NOT exit (target is 3%)
            opened_minutes_ago=5,
        )
        
        execution_engine.active_positions["BTC"] = position_exit
        execution_engine.active_positions["ETH"] = position_hold
        
        # Run check
        await execution_engine._check_roi_exits()
        
        # Verify only BTC was closed
        mock_client.close_position.assert_called_once_with("BTC")
        assert position_exit.exit_reason == "roi_target"
        assert position_hold.exit_reason is None


# =============================================================================
# ExecutionPosition Model Tests
# =============================================================================

class TestExecutionPositionModel:
    """Tests for ExecutionPosition with exit_reason field."""

    def test_position_to_dict_includes_exit_reason(self):
        """to_dict() should include exit_reason."""
        position = create_position()
        position.exit_reason = "roi_target"
        
        data = position.to_dict()
        
        assert "exit_reason" in data
        assert data["exit_reason"] == "roi_target"

    def test_position_exit_reason_default_none(self):
        """exit_reason should default to None."""
        position = create_position()
        
        assert position.exit_reason is None
        
        data = position.to_dict()
        assert data["exit_reason"] is None
