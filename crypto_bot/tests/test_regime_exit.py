"""
Tests for regime invalidation exit
====================================

When a position was opened in TREND regime and the regime changes
to RANGE or CHAOS, the position should be closed at market to free
the slot for a better opportunity.

Run:
    pytest crypto_bot/tests/test_regime_exit.py -v
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from crypto_bot.services.execution_engine import (
    ExecutionEngineService,
    ExecutionPosition,
    PositionStatus,
)
from crypto_bot.services.message_bus import Message
from crypto_bot.core.enums import Topic


def _make_engine() -> ExecutionEngineService:
    """Create an ExecutionEngineService with mocked dependencies."""
    engine = ExecutionEngineService.__new__(ExecutionEngineService)
    engine._logger = MagicMock()
    engine.active_positions = {}
    engine.client = AsyncMock()
    engine.client.cancel_order = AsyncMock()
    engine.client.close_position = AsyncMock()
    return engine


def _make_position(
    symbol: str = "BTC",
    side: str = "long",
    entry_regime: str | None = "trend",
    status: PositionStatus = PositionStatus.OPEN,
) -> ExecutionPosition:
    """Create a minimal ExecutionPosition for testing.

    Default opened_at is 1 hour ago to be well past the grace period
    (REGIME_GRACE_PERIOD_MINUTES = 20).
    """
    return ExecutionPosition(
        symbol=symbol,
        side=side,
        size=0.01,
        entry_price=95000.0,
        current_price=95100.0,
        status=status,
        opened_at=datetime.now(timezone.utc) - timedelta(hours=1),
        entry_regime=entry_regime,
    )


def _regime_message(symbol: str, regime: str) -> Message:
    """Create a regime change message."""
    return Message(
        topic=Topic.REGIME,
        payload={
            "symbol": symbol,
            "regime": regime,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "adx": 18.0,
            "trend_direction": "long",
        },
    )


class TestRegimeInvalidationExit:
    """Tests for closing positions when regime changes."""

    @pytest.mark.asyncio
    async def test_trend_to_range_closes_position(self) -> None:
        """Position opened in TREND closes when regime flips to RANGE."""
        engine = _make_engine()
        engine.active_positions["BTC"] = _make_position("BTC", entry_regime="trend")

        await engine._handle_regime_change(_regime_message("BTC", "range"))

        engine.client.close_position.assert_called_once_with("BTC")
        assert engine.active_positions["BTC"].exit_reason == "regime_change"
        assert engine.active_positions["BTC"].status == PositionStatus.CLOSING

    @pytest.mark.asyncio
    async def test_trend_to_chaos_closes_position(self) -> None:
        """Position opened in TREND closes when regime flips to CHAOS."""
        engine = _make_engine()
        engine.active_positions["ETH"] = _make_position("ETH", entry_regime="trend")

        await engine._handle_regime_change(_regime_message("ETH", "chaos"))

        engine.client.close_position.assert_called_once_with("ETH")
        assert engine.active_positions["ETH"].exit_reason == "regime_change"

    @pytest.mark.asyncio
    async def test_same_regime_no_action(self) -> None:
        """Position stays open if regime hasn't changed."""
        engine = _make_engine()
        engine.active_positions["BTC"] = _make_position("BTC", entry_regime="trend")

        await engine._handle_regime_change(_regime_message("BTC", "trend"))

        engine.client.close_position.assert_not_called()
        assert engine.active_positions["BTC"].status == PositionStatus.OPEN

    @pytest.mark.asyncio
    async def test_no_position_no_action(self) -> None:
        """Regime change for symbol without position is a no-op."""
        engine = _make_engine()

        await engine._handle_regime_change(_regime_message("BTC", "range"))

        engine.client.close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_entry_regime_no_action(self) -> None:
        """Synced positions (entry_regime=None) are never closed by regime change."""
        engine = _make_engine()
        engine.active_positions["BTC"] = _make_position("BTC", entry_regime=None)

        await engine._handle_regime_change(_regime_message("BTC", "range"))

        engine.client.close_position.assert_not_called()
        assert engine.active_positions["BTC"].status == PositionStatus.OPEN

    @pytest.mark.asyncio
    async def test_already_closing_no_action(self) -> None:
        """Position already CLOSING is not closed again."""
        engine = _make_engine()
        engine.active_positions["BTC"] = _make_position(
            "BTC", entry_regime="trend", status=PositionStatus.CLOSING
        )

        await engine._handle_regime_change(_regime_message("BTC", "range"))

        engine.client.close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_positions_only_affected_closed(self) -> None:
        """Only the position whose symbol changed regime is closed."""
        engine = _make_engine()
        engine.active_positions["BTC"] = _make_position("BTC", entry_regime="trend")
        engine.active_positions["ETH"] = _make_position("ETH", entry_regime="trend")

        await engine._handle_regime_change(_regime_message("BTC", "range"))

        engine.client.close_position.assert_called_once_with("BTC")
        assert engine.active_positions["BTC"].exit_reason == "regime_change"
        assert engine.active_positions["ETH"].status == PositionStatus.OPEN
        assert engine.active_positions["ETH"].exit_reason is None

    @pytest.mark.asyncio
    async def test_entry_regime_propagated_from_signal(self) -> None:
        """entry_regime field is set correctly when position is created."""
        pos = _make_position("SOL", entry_regime="trend")
        assert pos.entry_regime == "trend"

        d = pos.to_dict()
        assert d["entry_regime"] == "trend"
