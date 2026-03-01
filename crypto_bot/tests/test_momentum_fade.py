"""
Tests for Momentum Fade Exit feature.

Tests that positions are closed when momentum fades while still in profit,
and that all guard conditions are properly enforced.

Run:
    pytest crypto_bot/tests/test_momentum_fade.py -v
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock
from dataclasses import dataclass

from crypto_bot.services.execution_engine import (
    ExecutionEngineService,
    ExecutionPosition,
    PositionStatus,
)


# =============================================================================
# Helpers
# =============================================================================


@dataclass
class MockMarketState:
    """Minimal MarketState mock with rsi_slope."""
    symbol: str
    rsi_slope: float = 0.0
    ema_spread: float = 0.0


@dataclass
class MockMomentumExitConfig:
    """Mock config for momentum exit."""
    enabled: bool = True
    min_age_minutes: int = 15
    min_profit_pct: float = 0.1
    rsi_slope_threshold: float = 1.0


def _make_engine(momentum_exit_enabled: bool = True, **kwargs) -> ExecutionEngineService:
    """Create an ExecutionEngineService with mocked dependencies (bypass __init__)."""
    engine = ExecutionEngineService.__new__(ExecutionEngineService)
    engine._logger = MagicMock()
    engine.active_positions = {}
    engine._settling_symbols = set()
    engine._closing_positions = set()
    engine._market_states = {}
    engine._bot_config = MagicMock()
    engine._bot_config.momentum_exit = MockMomentumExitConfig(
        enabled=momentum_exit_enabled, **kwargs
    )
    engine.close_position = AsyncMock()
    return engine


def _make_position(
    symbol: str = "ETH",
    side: str = "long",
    entry_price: float = 100.0,
    current_price: float = 100.2,
    status: PositionStatus = PositionStatus.OPEN,
    age_minutes: float = 20.0,
    entry_rsi_slope: float = 5.0,
    entry_ema_spread: float = 0.5,
) -> ExecutionPosition:
    """Create an ExecutionPosition with specified age."""
    opened_at = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    return ExecutionPosition(
        symbol=symbol,
        side=side,
        size=1.0,
        entry_price=entry_price,
        current_price=current_price,
        status=status,
        opened_at=opened_at,
        entry_rsi_slope=entry_rsi_slope,
        entry_ema_spread=entry_ema_spread,
        highest_price=max(entry_price, current_price),
        lowest_price=min(entry_price, current_price),
    )


# =============================================================================
# LONG Positions
# =============================================================================


class TestMomentumFadeLong:
    """Tests for momentum fade exit on LONG positions."""

    @pytest.mark.asyncio
    async def test_long_closes_on_rsi_reversal(self) -> None:
        """LONG position closes when RSI slope turns negative (momentum died)."""
        engine = _make_engine()
        pos = _make_position(side="long", entry_price=100.0, current_price=100.2)
        engine.active_positions["ETH"] = pos
        engine._market_states = {"ETH": MockMarketState(symbol="ETH", rsi_slope=-2.0)}

        await engine._check_momentum_fade()

        assert pos.exit_reason == "momentum_fade"
        engine.close_position.assert_called_once_with("ETH")

    @pytest.mark.asyncio
    async def test_long_stays_with_strong_momentum(self) -> None:
        """LONG position stays open when RSI slope is still strong."""
        engine = _make_engine()
        pos = _make_position(side="long", entry_price=100.0, current_price=100.2)
        engine.active_positions["ETH"] = pos
        engine._market_states = {"ETH": MockMarketState(symbol="ETH", rsi_slope=3.5)}

        await engine._check_momentum_fade()

        assert pos.exit_reason is None
        engine.close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_long_closes_on_rsi_flat(self) -> None:
        """LONG position closes when RSI slope is flat (0.5 < threshold 1.0)."""
        engine = _make_engine()
        pos = _make_position(side="long", entry_price=100.0, current_price=100.2)
        engine.active_positions["ETH"] = pos
        engine._market_states = {"ETH": MockMarketState(symbol="ETH", rsi_slope=0.5)}

        await engine._check_momentum_fade()

        assert pos.exit_reason == "momentum_fade"
        engine.close_position.assert_called_once_with("ETH")


# =============================================================================
# SHORT Positions
# =============================================================================


class TestMomentumFadeShort:
    """Tests for momentum fade exit on SHORT positions."""

    @pytest.mark.asyncio
    async def test_short_closes_on_rsi_reversal(self) -> None:
        """SHORT position closes when RSI slope turns positive (momentum died)."""
        engine = _make_engine()
        pos = _make_position(side="short", entry_price=100.0, current_price=99.8)
        engine.active_positions["ETH"] = pos
        engine._market_states = {"ETH": MockMarketState(symbol="ETH", rsi_slope=2.0)}

        await engine._check_momentum_fade()

        assert pos.exit_reason == "momentum_fade"
        engine.close_position.assert_called_once_with("ETH")

    @pytest.mark.asyncio
    async def test_short_stays_with_strong_momentum(self) -> None:
        """SHORT position stays open when RSI slope is strongly negative."""
        engine = _make_engine()
        pos = _make_position(side="short", entry_price=100.0, current_price=99.8)
        engine.active_positions["ETH"] = pos
        engine._market_states = {"ETH": MockMarketState(symbol="ETH", rsi_slope=-3.5)}

        await engine._check_momentum_fade()

        assert pos.exit_reason is None
        engine.close_position.assert_not_called()


# =============================================================================
# Guard Conditions
# =============================================================================


class TestMomentumFadeGuards:
    """Tests for all guard conditions that prevent momentum fade exit."""

    @pytest.mark.asyncio
    async def test_skips_disabled(self) -> None:
        """No action when momentum_exit is disabled."""
        engine = _make_engine(momentum_exit_enabled=False)
        pos = _make_position(side="long", entry_price=100.0, current_price=100.2)
        engine.active_positions["ETH"] = pos
        engine._market_states = {"ETH": MockMarketState(symbol="ETH", rsi_slope=-5.0)}

        await engine._check_momentum_fade()

        assert pos.exit_reason is None
        engine.close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_young_position(self) -> None:
        """No action when position is younger than min_age_minutes."""
        engine = _make_engine()
        pos = _make_position(side="long", entry_price=100.0, current_price=100.2, age_minutes=5.0)
        engine.active_positions["ETH"] = pos
        engine._market_states = {"ETH": MockMarketState(symbol="ETH", rsi_slope=-5.0)}

        await engine._check_momentum_fade()

        assert pos.exit_reason is None
        engine.close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_in_loss(self) -> None:
        """No action when position is in loss (LONG price below entry)."""
        engine = _make_engine()
        pos = _make_position(side="long", entry_price=100.0, current_price=99.5)
        engine.active_positions["ETH"] = pos
        engine._market_states = {"ETH": MockMarketState(symbol="ETH", rsi_slope=-5.0)}

        await engine._check_momentum_fade()

        assert pos.exit_reason is None
        engine.close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_no_market_state(self) -> None:
        """No action when no market state data is available for symbol."""
        engine = _make_engine()
        pos = _make_position(side="long", entry_price=100.0, current_price=100.2)
        engine.active_positions["ETH"] = pos
        engine._market_states = {}  # No data

        await engine._check_momentum_fade()

        assert pos.exit_reason is None
        engine.close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_non_open_status(self) -> None:
        """No action when position status is not OPEN."""
        engine = _make_engine()
        pos = _make_position(side="long", entry_price=100.0, current_price=100.2, status=PositionStatus.CLOSING)
        engine.active_positions["ETH"] = pos
        engine._market_states = {"ETH": MockMarketState(symbol="ETH", rsi_slope=-5.0)}

        await engine._check_momentum_fade()

        assert pos.exit_reason is None
        engine.close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_settling(self) -> None:
        """No action when position symbol is mid-settlement."""
        engine = _make_engine()
        pos = _make_position(side="long", entry_price=100.0, current_price=100.2)
        engine.active_positions["ETH"] = pos
        engine._settling_symbols.add("ETH")
        engine._market_states = {"ETH": MockMarketState(symbol="ETH", rsi_slope=-5.0)}

        await engine._check_momentum_fade()

        assert pos.exit_reason is None
        engine.close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_closing(self) -> None:
        """No action when position symbol is already in _closing_positions."""
        engine = _make_engine()
        pos = _make_position(side="long", entry_price=100.0, current_price=100.2)
        engine.active_positions["ETH"] = pos
        engine._closing_positions.add("ETH")
        engine._market_states = {"ETH": MockMarketState(symbol="ETH", rsi_slope=-5.0)}

        await engine._check_momentum_fade()

        assert pos.exit_reason is None
        engine.close_position.assert_not_called()


# =============================================================================
# Edge Cases
# =============================================================================


class TestMomentumFadeEdgeCases:
    """Tests for boundary values and multi-position scenarios."""

    @pytest.mark.asyncio
    async def test_min_profit_boundary(self) -> None:
        """Position closes when profit is just above min_profit_pct (0.1%)."""
        engine = _make_engine()
        # Slightly above 0.1% to avoid floating-point boundary issues
        pos = _make_position(side="long", entry_price=100.0, current_price=100.15)
        engine.active_positions["ETH"] = pos
        engine._market_states = {"ETH": MockMarketState(symbol="ETH", rsi_slope=0.0)}

        await engine._check_momentum_fade()

        assert pos.exit_reason == "momentum_fade"
        engine.close_position.assert_called_once_with("ETH")

    @pytest.mark.asyncio
    async def test_multiple_positions_independent(self) -> None:
        """Only the faded position closes; the strong one stays open."""
        engine = _make_engine()

        # ETH: faded momentum
        pos_eth = _make_position(symbol="ETH", side="long", entry_price=100.0, current_price=100.2)
        engine.active_positions["ETH"] = pos_eth

        # BTC: strong momentum
        pos_btc = _make_position(symbol="BTC", side="long", entry_price=50000.0, current_price=50100.0)
        engine.active_positions["BTC"] = pos_btc

        engine._market_states = {
            "ETH": MockMarketState(symbol="ETH", rsi_slope=-1.0),  # faded
            "BTC": MockMarketState(symbol="BTC", rsi_slope=5.0),   # strong
        }

        await engine._check_momentum_fade()

        assert pos_eth.exit_reason == "momentum_fade"
        assert pos_btc.exit_reason is None
        engine.close_position.assert_called_once_with("ETH")
