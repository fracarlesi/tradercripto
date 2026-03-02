"""
Tests for Breakeven Stop Feature
=================================

When a position's unrealized P&L reaches +1.2%, the stop-loss trigger
is moved slightly above (LONG) or below (SHORT) the entry price
(breakeven + fee offset), making the trade risk-free.

Run:
    pytest crypto_bot/tests/test_breakeven_stop.py -v
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from crypto_bot.services.execution_engine import (
    BREAKEVEN_THRESHOLD_PCT,
    BREAKEVEN_OFFSET_PCT,
    ExecutionEngineService,
    ExecutionPosition,
    PositionStatus,
)


# =============================================================================
# Helpers
# =============================================================================


def _make_engine() -> ExecutionEngineService:
    """Create an ExecutionEngineService with mocked dependencies."""
    engine = ExecutionEngineService.__new__(ExecutionEngineService)
    engine._logger = MagicMock()
    engine.active_positions = {}
    engine._settling_symbols = set()
    engine.client = AsyncMock()
    engine.client.cancel_order = AsyncMock()
    engine._place_trigger_with_retry = AsyncMock(return_value={"orderId": "new_sl_123"})
    # Mock config for breakeven_threshold_pct (read by _check_breakeven_stops)
    engine._bot_config = MagicMock()
    engine._bot_config.risk.breakeven_threshold_pct = BREAKEVEN_THRESHOLD_PCT
    return engine


def _make_position(
    symbol: str = "BTC",
    side: str = "long",
    entry_price: float = 100_000.0,
    current_price: float = 100_000.0,
    sl_order_id: str | None = "999",
    sl_price: float | None = 99_200.0,
    breakeven_activated: bool = False,
    status: PositionStatus = PositionStatus.OPEN,
) -> ExecutionPosition:
    """Create a minimal ExecutionPosition for testing."""
    return ExecutionPosition(
        symbol=symbol,
        side=side,
        size=0.01,
        entry_price=entry_price,
        current_price=current_price,
        status=status,
        opened_at=datetime.now(timezone.utc),
        sl_order_id=sl_order_id,
        sl_price=sl_price,
        breakeven_activated=breakeven_activated,
    )


# =============================================================================
# LONG Positions
# =============================================================================


class TestBreakevenLong:
    """Tests for breakeven activation on LONG positions."""

    @pytest.mark.asyncio
    async def test_activates_at_threshold(self) -> None:
        """Breakeven triggers when long P&L reaches exactly +1.2%."""
        engine = _make_engine()
        # +1.2% on a 100_000 entry = 101_200
        pos = _make_position(
            entry_price=100_000.0,
            current_price=101_200.0,
            side="long",
        )
        engine.active_positions["BTC"] = pos

        await engine._check_breakeven_stops()

        # Old SL cancelled
        engine.client.cancel_order.assert_called_once_with("BTC", 999)
        # New SL placed at entry + offset (0.08%)
        expected_sl = 100_000.0 * (1 + BREAKEVEN_OFFSET_PCT / 100)
        engine._place_trigger_with_retry.assert_called_once_with(
            symbol="BTC",
            is_buy=False,  # close a long = sell
            size=pos.size,
            trigger_price=expected_sl,
            tpsl="sl",
        )
        assert pos.breakeven_activated is True
        assert pos.sl_price == expected_sl
        assert pos.sl_order_id == "new_sl_123"

    @pytest.mark.asyncio
    async def test_activates_above_threshold(self) -> None:
        """Breakeven triggers when P&L exceeds +1.2% (e.g. +1.5%)."""
        engine = _make_engine()
        pos = _make_position(
            entry_price=100_000.0,
            current_price=101_500.0,  # +1.5%
            side="long",
        )
        engine.active_positions["BTC"] = pos

        await engine._check_breakeven_stops()

        expected_sl = 100_000.0 * (1 + BREAKEVEN_OFFSET_PCT / 100)
        assert pos.breakeven_activated is True
        assert pos.sl_price == expected_sl

    @pytest.mark.asyncio
    async def test_does_not_activate_below_threshold(self) -> None:
        """No breakeven when P&L is below +0.3%."""
        engine = _make_engine()
        pos = _make_position(
            entry_price=100_000.0,
            current_price=100_200.0,  # +0.2%
            side="long",
        )
        engine.active_positions["BTC"] = pos

        await engine._check_breakeven_stops()

        engine.client.cancel_order.assert_not_called()
        engine._place_trigger_with_retry.assert_not_called()
        assert pos.breakeven_activated is False

    @pytest.mark.asyncio
    async def test_does_not_activate_at_loss(self) -> None:
        """No breakeven when long position is in loss."""
        engine = _make_engine()
        pos = _make_position(
            entry_price=100_000.0,
            current_price=99_000.0,  # -1%
            side="long",
        )
        engine.active_positions["BTC"] = pos

        await engine._check_breakeven_stops()

        assert pos.breakeven_activated is False
        engine._place_trigger_with_retry.assert_not_called()


# =============================================================================
# SHORT Positions
# =============================================================================


class TestBreakevenShort:
    """Tests for breakeven activation on SHORT positions."""

    @pytest.mark.asyncio
    async def test_activates_at_threshold(self) -> None:
        """Breakeven triggers when short P&L reaches +1.2% (price drops 1.2%)."""
        engine = _make_engine()
        # SHORT: profit when price goes DOWN.  entry=100k, current=98_800 => +1.2%
        pos = _make_position(
            entry_price=100_000.0,
            current_price=98_800.0,
            side="short",
            sl_price=100_800.0,
        )
        engine.active_positions["ETH"] = pos

        await engine._check_breakeven_stops()

        engine.client.cancel_order.assert_called_once()
        # New SL placed at entry - offset (0.08%)
        expected_sl = 100_000.0 * (1 - BREAKEVEN_OFFSET_PCT / 100)
        engine._place_trigger_with_retry.assert_called_once_with(
            symbol="ETH",
            is_buy=True,  # close a short = buy
            size=pos.size,
            trigger_price=expected_sl,
            tpsl="sl",
        )
        assert pos.breakeven_activated is True
        assert pos.sl_price == expected_sl

    @pytest.mark.asyncio
    async def test_does_not_activate_below_threshold(self) -> None:
        """No breakeven when short P&L is below +0.3%."""
        engine = _make_engine()
        pos = _make_position(
            entry_price=100_000.0,
            current_price=99_850.0,  # +0.15% for short
            side="short",
        )
        engine.active_positions["ETH"] = pos

        await engine._check_breakeven_stops()

        assert pos.breakeven_activated is False
        engine._place_trigger_with_retry.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_activate_at_loss(self) -> None:
        """No breakeven when short position is at a loss (price went UP)."""
        engine = _make_engine()
        pos = _make_position(
            entry_price=100_000.0,
            current_price=101_000.0,  # -1% for short
            side="short",
        )
        engine.active_positions["ETH"] = pos

        await engine._check_breakeven_stops()

        assert pos.breakeven_activated is False


# =============================================================================
# Idempotency & Edge Cases
# =============================================================================


class TestBreakevenIdempotency:
    """Ensure breakeven is not re-triggered once activated."""

    @pytest.mark.asyncio
    async def test_does_not_re_activate(self) -> None:
        """If breakeven is already activated, no further action is taken."""
        engine = _make_engine()
        pos = _make_position(
            entry_price=100_000.0,
            current_price=101_000.0,  # well above threshold
            side="long",
            breakeven_activated=True,
        )
        engine.active_positions["BTC"] = pos

        await engine._check_breakeven_stops()

        engine.client.cancel_order.assert_not_called()
        engine._place_trigger_with_retry.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_non_open_positions(self) -> None:
        """Positions that are CLOSING or CLOSED are skipped."""
        engine = _make_engine()
        pos = _make_position(
            entry_price=100_000.0,
            current_price=101_000.0,
            side="long",
            status=PositionStatus.CLOSING,
        )
        engine.active_positions["BTC"] = pos

        await engine._check_breakeven_stops()

        engine._place_trigger_with_retry.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_settling_symbols(self) -> None:
        """Positions mid-settle are skipped."""
        engine = _make_engine()
        engine._settling_symbols.add("BTC")
        pos = _make_position(
            entry_price=100_000.0,
            current_price=101_000.0,
            side="long",
        )
        engine.active_positions["BTC"] = pos

        await engine._check_breakeven_stops()

        engine._place_trigger_with_retry.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_zero_entry_price(self) -> None:
        """Positions with entry_price=0 (invalid) are skipped."""
        engine = _make_engine()
        pos = _make_position(
            entry_price=0.0,
            current_price=100.0,
            side="long",
        )
        engine.active_positions["BTC"] = pos

        await engine._check_breakeven_stops()

        engine._place_trigger_with_retry.assert_not_called()


# =============================================================================
# Order Lifecycle
# =============================================================================


class TestBreakevenOrderLifecycle:
    """Tests for cancel/place order interactions."""

    @pytest.mark.asyncio
    async def test_no_old_sl_to_cancel(self) -> None:
        """If sl_order_id is None, cancel is not called but new SL is placed."""
        engine = _make_engine()
        pos = _make_position(
            entry_price=100_000.0,
            current_price=101_300.0,  # +1.3% > 1.2% threshold
            side="long",
            sl_order_id=None,
            sl_price=None,
        )
        engine.active_positions["BTC"] = pos

        await engine._check_breakeven_stops()

        engine.client.cancel_order.assert_not_called()
        engine._place_trigger_with_retry.assert_called_once()
        assert pos.breakeven_activated is True

    @pytest.mark.asyncio
    async def test_cancel_failure_still_places_new_sl(self) -> None:
        """If cancelling the old SL fails, the new SL is still placed."""
        engine = _make_engine()
        engine.client.cancel_order.side_effect = Exception("Order not found")
        pos = _make_position(
            entry_price=100_000.0,
            current_price=101_300.0,  # +1.3% > 1.2% threshold
            side="long",
        )
        engine.active_positions["BTC"] = pos

        await engine._check_breakeven_stops()

        # Cancel was attempted
        engine.client.cancel_order.assert_called_once()
        # New SL still placed
        engine._place_trigger_with_retry.assert_called_once()
        assert pos.breakeven_activated is True

    @pytest.mark.asyncio
    async def test_new_sl_placement_failure(self) -> None:
        """If placing new SL fails, breakeven_activated stays False."""
        engine = _make_engine()
        engine._place_trigger_with_retry = AsyncMock(
            side_effect=Exception("API error")
        )
        pos = _make_position(
            entry_price=100_000.0,
            current_price=101_300.0,  # +1.3% > 1.2% threshold
            side="long",
        )
        engine.active_positions["BTC"] = pos

        await engine._check_breakeven_stops()

        # Old SL was cancelled
        engine.client.cancel_order.assert_called_once()
        # breakeven NOT marked (placement failed)
        assert pos.breakeven_activated is False

    @pytest.mark.asyncio
    async def test_multiple_positions_independent(self) -> None:
        """Breakeven is evaluated independently for each position."""
        engine = _make_engine()

        # BTC: above threshold -> should activate
        pos_btc = _make_position(
            symbol="BTC",
            entry_price=100_000.0,
            current_price=101_200.0,  # +1.2%
            side="long",
            sl_order_id="111",
        )
        # ETH: below threshold -> should NOT activate
        pos_eth = _make_position(
            symbol="ETH",
            entry_price=3_000.0,
            current_price=3_005.0,  # +0.17% (below 1.2%)
            side="long",
            sl_order_id="222",
        )
        engine.active_positions["BTC"] = pos_btc
        engine.active_positions["ETH"] = pos_eth

        await engine._check_breakeven_stops()

        assert pos_btc.breakeven_activated is True
        assert pos_eth.breakeven_activated is False

        # Only BTC's old SL was cancelled
        engine.client.cancel_order.assert_called_once_with("BTC", 111)
        # Only BTC got a new SL
        engine._place_trigger_with_retry.assert_called_once()


# =============================================================================
# Model Field Tests
# =============================================================================


class TestBreakevenField:
    """Tests for the breakeven_activated field on ExecutionPosition."""

    def test_defaults_to_false(self) -> None:
        """breakeven_activated defaults to False."""
        pos = _make_position()
        assert pos.breakeven_activated is False

    def test_to_dict_includes_breakeven(self) -> None:
        """to_dict() serializes breakeven_activated."""
        pos = _make_position(breakeven_activated=True)
        d = pos.to_dict()
        assert "breakeven_activated" in d
        assert d["breakeven_activated"] is True

    def test_to_dict_false_by_default(self) -> None:
        """to_dict() shows False when not activated."""
        pos = _make_position()
        d = pos.to_dict()
        assert d["breakeven_activated"] is False


# =============================================================================
# Constant Validation
# =============================================================================


class TestBreakevenConstant:
    """Verify the BREAKEVEN_THRESHOLD_PCT and BREAKEVEN_OFFSET_PCT constants."""

    def test_threshold_value(self) -> None:
        assert BREAKEVEN_THRESHOLD_PCT == 1.2

    def test_offset_value(self) -> None:
        assert BREAKEVEN_OFFSET_PCT == 0.15
