# pyright: reportAttributeAccessIssue=false
"""
Tests for global min_hold enforcement in ExecutionEngineService.close_position.
===============================================================================

Covers the anti-churn gate added in fix/min-hold-global-enforcement. The gate
lives in ExecutionEngineService.close_position itself so ALL in-bot mechanical
close paths (strength_exit, roi_target, momentum_fade, regime_change,
max_hold_time, TP/SL hit) respect it uniformly. Emergency callers can bypass
via ``override_min_hold=True``.

Run:
    pytest crypto_bot/tests/test_min_hold_global_enforcement.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from crypto_bot.services.execution_engine import (
    ExecutionEngineService,
    ExecutionPosition,
    PositionStatus,
)


# =============================================================================
# Helpers
# =============================================================================


def _make_engine(min_hold_minutes: int = 120) -> ExecutionEngineService:
    """Create an ExecutionEngineService with mocked dependencies."""
    engine = ExecutionEngineService.__new__(ExecutionEngineService)
    engine._logger = MagicMock()
    engine.active_positions = {}
    engine._settling_symbols = set()
    engine._closing_positions = set()
    engine.client = AsyncMock()
    engine.client.cancel_order = AsyncMock()
    engine.client.close_position = AsyncMock(return_value={"status": "ok"})
    engine._bot_config = MagicMock()
    engine._bot_config.stops.min_hold_minutes = min_hold_minutes
    return engine


def _make_position(
    *,
    symbol: str = "ETH",
    age_minutes: float,
    status: PositionStatus = PositionStatus.OPEN,
    exit_reason: str | None = None,
) -> ExecutionPosition:
    return ExecutionPosition(
        symbol=symbol,
        side="long",
        size=0.1,
        entry_price=2000.0,
        current_price=2020.0,
        status=status,
        opened_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
        exit_reason=exit_reason,
    )


# =============================================================================
# Tests
# =============================================================================


class TestMinHoldGlobalEnforcement:
    """close_position must respect config min_hold_minutes by default."""

    @pytest.mark.asyncio
    async def test_strength_exit_blocked_at_90min(self) -> None:
        """strength_exit at 90min age < 120min min_hold -> blocked, no close."""
        engine = _make_engine(min_hold_minutes=120)
        pos = _make_position(age_minutes=90.0, exit_reason="strength_exit")
        engine.active_positions["ETH"] = pos

        result = await engine.close_position("ETH")

        assert result is None
        engine.client.close_position.assert_not_called()
        # Should not leave status stuck in CLOSING
        assert pos.status == PositionStatus.OPEN

    @pytest.mark.asyncio
    async def test_strength_exit_allowed_at_121min(self) -> None:
        """strength_exit at 121min > 120min min_hold -> close executes."""
        engine = _make_engine(min_hold_minutes=120)
        pos = _make_position(age_minutes=121.0, exit_reason="strength_exit")
        engine.active_positions["ETH"] = pos

        result = await engine.close_position("ETH")

        assert result == {"status": "ok"}
        engine.client.close_position.assert_awaited_once_with("ETH")
        assert pos.status == PositionStatus.CLOSING

    @pytest.mark.asyncio
    async def test_regime_change_emergency_override_at_30min(self) -> None:
        """regime_change with override_min_hold=True must close even at 30min."""
        engine = _make_engine(min_hold_minutes=120)
        pos = _make_position(age_minutes=30.0, exit_reason="regime_change")
        engine.active_positions["ETH"] = pos

        result = await engine.close_position("ETH", override_min_hold=True)

        assert result == {"status": "ok"}
        engine.client.close_position.assert_awaited_once_with("ETH")
        assert pos.status == PositionStatus.CLOSING

    @pytest.mark.asyncio
    async def test_min_hold_disabled_allows_close(self) -> None:
        """min_hold=0 disables the gate entirely."""
        engine = _make_engine(min_hold_minutes=0)
        pos = _make_position(age_minutes=1.0, exit_reason="roi_target")
        engine.active_positions["ETH"] = pos

        result = await engine.close_position("ETH")

        assert result == {"status": "ok"}
        engine.client.close_position.assert_awaited_once_with("ETH")

    @pytest.mark.asyncio
    async def test_blocked_close_rolls_back_closing_guard(self) -> None:
        """If a caller pre-marked the symbol CLOSING, block should roll it back."""
        engine = _make_engine(min_hold_minutes=120)
        pos = _make_position(
            age_minutes=10.0,
            status=PositionStatus.CLOSING,
            exit_reason="momentum_fade",
        )
        engine.active_positions["ETH"] = pos
        engine._closing_positions.add("ETH")

        result = await engine.close_position("ETH")

        assert result is None
        engine.client.close_position.assert_not_called()
        assert "ETH" not in engine._closing_positions
        assert pos.status == PositionStatus.OPEN

    def test_helper_returns_false_when_opened_at_none(self) -> None:
        """Unknown opened_at must fail OPEN (do not wedge the bot)."""
        engine = _make_engine(min_hold_minutes=120)
        pos = _make_position(age_minutes=5.0)
        pos.opened_at = None

        assert engine._should_block_close_for_min_hold(pos) is False
