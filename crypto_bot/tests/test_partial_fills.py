"""
Tests for Partial Fill Handling
================================

When a limit order is filled in multiple tranches (partial fills), the bot
must accumulate the fills and place TP/SL for the **total** filled size,
not just the first fill.

Key scenarios tested:
1. Grace period: bot waits for remaining fills before processing
2. Size growth resets the grace timer
3. Grace expiry: bot proceeds with actual size after timeout
4. Full fill: no grace delay when exchange size matches expected
5. Sync detects size growth and re-places TP/SL
6. Cleanup on position close

Run:
    pytest crypto_bot/tests/test_partial_fills.py -v
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from crypto_bot.services.execution_engine import (
    ExecutionEngineService,
    ExecutionPosition,
    Order,
    OrderStatus,
    PositionStatus,
)
from crypto_bot.services.message_bus import Topic


# =============================================================================
# Helpers
# =============================================================================


def _make_engine() -> ExecutionEngineService:
    """Create an ExecutionEngineService with mocked dependencies."""
    engine = ExecutionEngineService.__new__(ExecutionEngineService)
    engine._logger = MagicMock()
    engine.active_positions = {}
    engine.pending_orders = {}
    engine.processed_signals = set()
    engine._tp_sl_confirmed = set()
    engine._closing_positions = set()
    engine._settling_symbols = set()
    engine._partial_fill_first_seen = {}
    engine._tp_sl_placed_size = {}
    engine.metrics = MagicMock()
    engine.metrics.orders_filled = 0
    engine.metrics.orders_cancelled = 0
    engine.client = AsyncMock()
    engine.client.cancel_order = AsyncMock()
    engine.client.get_open_orders = AsyncMock(return_value=[])
    engine.client.get_positions = AsyncMock(return_value=[])
    engine._place_trigger_with_retry = AsyncMock(return_value={"orderId": "50001"})
    engine._handle_order_filled = AsyncMock()
    engine.publish = AsyncMock()
    engine._send_alert = AsyncMock()

    # Mock config
    class _RiskConfig:
        stop_loss_pct = 0.8
        take_profit_pct = 1.6

    class _StopsConfig:
        initial_atr_mult = 2.5
        trailing_atr_mult = 2.5

    class _ExecConfig:
        order_type = "limit"
        max_slippage_pct = 0.25
        limit_timeout_seconds = 60
        retry_attempts = 3
        retry_delay_seconds = 1.0

    class _ServicesConfig:
        execution_engine = _ExecConfig()

    class _BotConfig:
        risk = _RiskConfig()
        stops = _StopsConfig()
        services = _ServicesConfig()

    engine._bot_config = _BotConfig()
    engine._exec_config = _ExecConfig()

    return engine


def _make_pending_order(
    order_id: str = "12345",
    symbol: str = "FTT",
    side: str = "sell",
    size: float = 191.4,
    strategy: str = "trend_momentum",
) -> Order:
    """Create a pending limit order."""
    return Order(
        order_id=order_id,
        symbol=symbol,
        side=side,
        size=size,
        price=1.05,
        order_type="limit",
        status=OrderStatus.SUBMITTED,
        signal_id="sig_001",
        strategy=strategy,
        submitted_at=datetime.now(timezone.utc),
        original_signal={
            "symbol": symbol,
            "direction": "short" if side == "sell" else "long",
            "entry_price": 1.05,
            "size": size,
            "strategy": strategy,
            "atr_pct": 0.5,
        },
    )


# =============================================================================
# Partial Fill Grace Period Tests
# =============================================================================


class TestPartialFillGracePeriod:
    """Tests for the grace period that waits for remaining fills."""

    @pytest.mark.asyncio
    async def test_partial_fill_triggers_grace_period(self) -> None:
        """When exchange shows less than expected, bot should wait."""
        engine = _make_engine()
        order = _make_pending_order(size=191.4)
        engine.pending_orders["12345"] = order

        # Exchange shows only 57.7 filled (first tranche)
        engine.client.get_open_orders = AsyncMock(return_value=[])
        engine.client.get_positions = AsyncMock(return_value=[
            {"symbol": "FTT", "size": -57.7, "entryPrice": 1.05, "markPrice": 1.05},
        ])

        await engine._poll_pending_order_fills()

        # Should NOT have processed the fill yet
        engine._handle_order_filled.assert_not_called()
        # Order should still be pending
        assert "12345" in engine.pending_orders
        # Grace period tracking should be active
        assert "12345" in engine._partial_fill_first_seen

    @pytest.mark.asyncio
    async def test_full_fill_no_grace_period(self) -> None:
        """When exchange size >= 95% of expected, process immediately."""
        engine = _make_engine()
        order = _make_pending_order(size=191.4)
        engine.pending_orders["12345"] = order

        # Exchange shows full size
        engine.client.get_open_orders = AsyncMock(return_value=[])
        engine.client.get_positions = AsyncMock(return_value=[
            {"symbol": "FTT", "size": -191.4, "entryPrice": 1.05, "markPrice": 1.05},
        ])

        await engine._poll_pending_order_fills()

        # Should process fill immediately
        engine._handle_order_filled.assert_called_once()
        # Order should be removed from pending
        assert "12345" not in engine.pending_orders

    @pytest.mark.asyncio
    async def test_size_growth_resets_grace_timer(self) -> None:
        """When more fills arrive during grace period, timer resets."""
        engine = _make_engine()
        order = _make_pending_order(size=191.4)
        engine.pending_orders["12345"] = order

        engine.client.get_open_orders = AsyncMock(return_value=[])

        # First poll: 57.7 filled
        engine.client.get_positions = AsyncMock(return_value=[
            {"symbol": "FTT", "size": -57.7, "entryPrice": 1.05, "markPrice": 1.05},
        ])
        await engine._poll_pending_order_fills()

        first_seen_time, first_size = engine._partial_fill_first_seen["12345"]
        assert first_size == pytest.approx(57.7)

        # Second poll: 133.7 more arrived (total 191.4)
        # But still < 95% of 191.4? No, 191.4/191.4 = 100%. Let's test intermediate growth.
        engine.client.get_positions = AsyncMock(return_value=[
            {"symbol": "FTT", "size": -120.0, "entryPrice": 1.05, "markPrice": 1.05},
        ])
        await engine._poll_pending_order_fills()

        # Timer should have been reset (new first_seen time)
        new_time, new_size = engine._partial_fill_first_seen["12345"]
        assert new_size == pytest.approx(120.0)
        # Still not processed
        engine._handle_order_filled.assert_not_called()

    @pytest.mark.asyncio
    async def test_grace_period_expiry_processes_partial(self) -> None:
        """After grace period expires, process with actual (partial) size."""
        engine = _make_engine()
        order = _make_pending_order(size=191.4)
        engine.pending_orders["12345"] = order

        engine.client.get_open_orders = AsyncMock(return_value=[])
        engine.client.get_positions = AsyncMock(return_value=[
            {"symbol": "FTT", "size": -57.7, "entryPrice": 1.05, "markPrice": 1.05},
        ])

        # Simulate first detection was 15 seconds ago (> 10s grace)
        engine._partial_fill_first_seen["12345"] = (
            datetime.now(timezone.utc) - timedelta(seconds=15),
            57.7,
        )

        await engine._poll_pending_order_fills()

        # Should now process the fill with actual size
        engine._handle_order_filled.assert_called_once()
        call_args = engine._handle_order_filled.call_args
        filled_order = call_args[0][1]  # second positional arg is the order
        assert filled_order.filled_size == pytest.approx(57.7)
        # Order removed from pending
        assert "12345" not in engine.pending_orders
        # Partial fill tracking cleaned up
        assert "12345" not in engine._partial_fill_first_seen

    @pytest.mark.asyncio
    async def test_still_within_grace_period_keeps_waiting(self) -> None:
        """During grace period (size unchanged), keep waiting."""
        engine = _make_engine()
        order = _make_pending_order(size=191.4)
        engine.pending_orders["12345"] = order

        engine.client.get_open_orders = AsyncMock(return_value=[])
        engine.client.get_positions = AsyncMock(return_value=[
            {"symbol": "FTT", "size": -57.7, "entryPrice": 1.05, "markPrice": 1.05},
        ])

        # Simulate first detection was 3 seconds ago (< 10s grace)
        engine._partial_fill_first_seen["12345"] = (
            datetime.now(timezone.utc) - timedelta(seconds=3),
            57.7,
        )

        await engine._poll_pending_order_fills()

        # Should NOT process
        engine._handle_order_filled.assert_not_called()
        assert "12345" in engine.pending_orders

    @pytest.mark.asyncio
    async def test_order_still_open_clears_partial_tracking(self) -> None:
        """If order reappears in open orders, clear partial fill tracking."""
        engine = _make_engine()
        order = _make_pending_order(size=191.4)
        engine.pending_orders["12345"] = order

        # Pre-seed partial fill tracking
        engine._partial_fill_first_seen["12345"] = (
            datetime.now(timezone.utc),
            57.7,
        )

        # Order is back in open orders (e.g. exchange partial fill, rest still open)
        engine.client.get_open_orders = AsyncMock(return_value=[
            {"orderId": "12345"},
        ])

        await engine._poll_pending_order_fills()

        # Tracking should be cleared
        assert "12345" not in engine._partial_fill_first_seen
        # Not processed
        engine._handle_order_filled.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_position_after_removal_is_external_cancel(self) -> None:
        """If order vanishes and no position exists, it was cancelled."""
        engine = _make_engine()
        order = _make_pending_order(size=191.4)
        engine.pending_orders["12345"] = order

        engine.client.get_open_orders = AsyncMock(return_value=[])
        engine.client.get_positions = AsyncMock(return_value=[])

        await engine._poll_pending_order_fills()

        engine._handle_order_filled.assert_not_called()
        assert "12345" not in engine.pending_orders
        # Should publish cancellation event
        engine.publish.assert_called()

    @pytest.mark.asyncio
    async def test_settling_symbol_skipped(self) -> None:
        """Orders for settling symbols should be skipped."""
        engine = _make_engine()
        order = _make_pending_order(size=191.4)
        engine.pending_orders["12345"] = order
        engine._settling_symbols.add("FTT")

        engine.client.get_open_orders = AsyncMock(return_value=[])

        await engine._poll_pending_order_fills()

        engine._handle_order_filled.assert_not_called()
        assert "12345" in engine.pending_orders


# =============================================================================
# Size Change Detection in Sync
# =============================================================================


class TestSyncSizeChangeDetection:
    """Tests for TP/SL re-placement when position size grows."""

    @pytest.mark.asyncio
    async def test_size_growth_triggers_tp_sl_update(self) -> None:
        """When synced size > tp_sl_placed_size by >2%, re-place TP/SL."""
        engine = _make_engine()

        # Existing position with TP/SL placed for 57.7
        pos = ExecutionPosition(
            symbol="FTT",
            side="short",
            size=57.7,
            entry_price=1.05,
            current_price=1.05,
            status=PositionStatus.OPEN,
            opened_at=datetime.now(timezone.utc),
            tp_order_id="10001",
            tp_price=0.98,
            sl_order_id="20001",
            sl_price=1.12,
            highest_price=1.05,
            lowest_price=1.05,
        )
        engine.active_positions["FTT"] = pos
        engine._tp_sl_placed_size["FTT"] = 57.7

        # Exchange now shows 191.4 (additional fills)
        engine.client.get_positions = AsyncMock(return_value=[
            {
                "symbol": "FTT",
                "size": -191.4,
                "entryPrice": 1.048,
                "markPrice": 1.05,
                "unrealizedPnl": -0.5,
            },
        ])

        # Mock _update_tp_sl_for_size_change
        engine._update_tp_sl_for_size_change = AsyncMock()

        await engine._sync_positions_from_exchange()

        # Size should be updated
        assert pos.size == pytest.approx(191.4)
        # Entry price should be updated from exchange
        assert pos.entry_price == pytest.approx(1.048)
        # Should have called the TP/SL update
        engine._update_tp_sl_for_size_change.assert_called_once_with(pos)

    @pytest.mark.asyncio
    async def test_minor_size_change_no_update(self) -> None:
        """Size changes <= 2% should not trigger TP/SL re-placement."""
        engine = _make_engine()

        pos = ExecutionPosition(
            symbol="BTC",
            side="long",
            size=0.1000,
            entry_price=50000.0,
            current_price=50100.0,
            status=PositionStatus.OPEN,
            opened_at=datetime.now(timezone.utc),
            tp_order_id="10001",
            sl_order_id="20001",
            highest_price=50000.0,
            lowest_price=50000.0,
        )
        engine.active_positions["BTC"] = pos
        engine._tp_sl_placed_size["BTC"] = 0.1000

        # Size barely changed (within 2% float noise)
        engine.client.get_positions = AsyncMock(return_value=[
            {
                "symbol": "BTC",
                "size": 0.1001,
                "entryPrice": 50000.0,
                "markPrice": 50100.0,
                "unrealizedPnl": 1.0,
            },
        ])

        engine._update_tp_sl_for_size_change = AsyncMock()

        await engine._sync_positions_from_exchange()

        engine._update_tp_sl_for_size_change.assert_not_called()

    @pytest.mark.asyncio
    async def test_settling_symbol_no_update(self) -> None:
        """Settling symbols should not trigger TP/SL re-placement."""
        engine = _make_engine()

        pos = ExecutionPosition(
            symbol="FTT",
            side="short",
            size=57.7,
            entry_price=1.05,
            current_price=1.05,
            status=PositionStatus.OPEN,
            opened_at=datetime.now(timezone.utc),
            highest_price=1.05,
            lowest_price=1.05,
        )
        engine.active_positions["FTT"] = pos
        engine._tp_sl_placed_size["FTT"] = 57.7
        engine._settling_symbols.add("FTT")

        engine.client.get_positions = AsyncMock(return_value=[
            {
                "symbol": "FTT",
                "size": -191.4,
                "entryPrice": 1.05,
                "markPrice": 1.05,
                "unrealizedPnl": 0,
            },
        ])

        engine._update_tp_sl_for_size_change = AsyncMock()

        await engine._sync_positions_from_exchange()

        # Size is updated by sync...
        assert pos.size == pytest.approx(191.4)
        # ...but TP/SL re-placement should NOT be triggered for settling symbols
        engine._update_tp_sl_for_size_change.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_tp_sl_placed_size_no_update(self) -> None:
        """If _tp_sl_placed_size has no entry, no re-placement."""
        engine = _make_engine()

        pos = ExecutionPosition(
            symbol="ETH",
            side="long",
            size=1.0,
            entry_price=3000.0,
            current_price=3000.0,
            status=PositionStatus.OPEN,
            opened_at=datetime.now(timezone.utc),
            highest_price=3000.0,
            lowest_price=3000.0,
        )
        engine.active_positions["ETH"] = pos
        # No _tp_sl_placed_size entry for ETH

        engine.client.get_positions = AsyncMock(return_value=[
            {
                "symbol": "ETH",
                "size": 2.0,
                "entryPrice": 3000.0,
                "markPrice": 3010.0,
                "unrealizedPnl": 10.0,
            },
        ])

        engine._update_tp_sl_for_size_change = AsyncMock()

        await engine._sync_positions_from_exchange()

        # tp_sl_placed_size is 0, so new_size (2.0) > 0 * 1.02 = 0 is true,
        # but the condition also requires tp_sl_size > 0, which is false
        engine._update_tp_sl_for_size_change.assert_not_called()


# =============================================================================
# _update_tp_sl_for_size_change Tests
# =============================================================================


class TestUpdateTpSlForSizeChange:
    """Tests for the TP/SL re-placement method."""

    @pytest.mark.asyncio
    async def test_cancels_old_and_places_new_tp_sl(self) -> None:
        """Should cancel old TP/SL and place new ones with updated size."""
        engine = _make_engine()

        pos = ExecutionPosition(
            symbol="FTT",
            side="short",
            size=191.4,
            entry_price=1.048,
            current_price=1.05,
            status=PositionStatus.OPEN,
            opened_at=datetime.now(timezone.utc),
            tp_order_id="10001",
            tp_price=0.98,
            sl_order_id="20001",
            sl_price=1.12,
            highest_price=1.048,
            lowest_price=1.048,
        )
        engine.active_positions["FTT"] = pos

        # Make _place_trigger_with_retry return different IDs for TP and SL
        call_count = 0

        async def mock_place_trigger(**kwargs):
            nonlocal call_count
            call_count += 1
            return {"orderId": f"new_{call_count}"}

        engine._place_trigger_with_retry = AsyncMock(side_effect=mock_place_trigger)

        await engine._update_tp_sl_for_size_change(pos)

        # Should have cancelled old orders
        cancel_calls = engine.client.cancel_order.call_args_list
        assert len(cancel_calls) == 2
        # TP cancel
        assert cancel_calls[0][0] == ("FTT", 10001)
        # SL cancel
        assert cancel_calls[1][0] == ("FTT", 20001)

        # Should have placed new TP and SL with full size
        trigger_calls = engine._place_trigger_with_retry.call_args_list
        assert len(trigger_calls) == 2

        # TP call: is_buy=True (close short), size=191.4, same price
        tp_kwargs = trigger_calls[0][1]
        assert tp_kwargs["symbol"] == "FTT"
        assert tp_kwargs["is_buy"] is True  # Close short
        assert tp_kwargs["size"] == pytest.approx(191.4)
        assert tp_kwargs["trigger_price"] == pytest.approx(0.98)
        assert tp_kwargs["tpsl"] == "tp"

        # SL call: is_buy=True (close short), size=191.4, same price
        sl_kwargs = trigger_calls[1][1]
        assert sl_kwargs["symbol"] == "FTT"
        assert sl_kwargs["is_buy"] is True  # Close short
        assert sl_kwargs["size"] == pytest.approx(191.4)
        assert sl_kwargs["trigger_price"] == pytest.approx(1.12)
        assert sl_kwargs["tpsl"] == "sl"

        # Should update tracking
        assert engine._tp_sl_placed_size["FTT"] == pytest.approx(191.4)

        # Should send alert
        engine._send_alert.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_cancel_failure_gracefully(self) -> None:
        """If cancel fails, still place new orders."""
        engine = _make_engine()

        pos = ExecutionPosition(
            symbol="FTT",
            side="long",
            size=100.0,
            entry_price=1.05,
            current_price=1.06,
            status=PositionStatus.OPEN,
            opened_at=datetime.now(timezone.utc),
            tp_order_id="10001",
            tp_price=1.10,
            sl_order_id="20001",
            sl_price=1.00,
            highest_price=1.05,
            lowest_price=1.05,
        )

        # Cancel will fail
        engine.client.cancel_order = AsyncMock(side_effect=Exception("Cancel failed"))

        await engine._update_tp_sl_for_size_change(pos)

        # Should still try to place new orders despite cancel failure
        assert engine._place_trigger_with_retry.call_count == 2

    @pytest.mark.asyncio
    async def test_no_tp_price_skips_tp_placement(self) -> None:
        """If position has no tp_price, skip TP re-placement."""
        engine = _make_engine()

        pos = ExecutionPosition(
            symbol="BTC",
            side="long",
            size=0.1,
            entry_price=50000.0,
            current_price=50000.0,
            status=PositionStatus.OPEN,
            opened_at=datetime.now(timezone.utc),
            tp_order_id=None,
            tp_price=None,
            sl_order_id="20001",
            sl_price=49000.0,
            highest_price=50000.0,
            lowest_price=50000.0,
        )

        await engine._update_tp_sl_for_size_change(pos)

        # Should only place SL (1 call), not TP
        assert engine._place_trigger_with_retry.call_count == 1
        sl_kwargs = engine._place_trigger_with_retry.call_args[1]
        assert sl_kwargs["tpsl"] == "sl"


# =============================================================================
# Handle Order Filled Records Size
# =============================================================================


class TestHandleOrderFilledRecordsSize:
    """Verify _handle_order_filled records tp_sl_placed_size."""

    @pytest.mark.asyncio
    async def test_records_tp_sl_placed_size(self) -> None:
        """After handling a fill, _tp_sl_placed_size should be set."""
        engine = _make_engine()

        # We need the real _handle_order_filled, not mocked
        # Re-instantiate with the real method
        real_engine = _make_engine()
        real_engine._handle_order_filled = (
            ExecutionEngineService._handle_order_filled.__get__(real_engine)
        )
        # Mock _set_tp_sl
        real_engine._set_tp_sl = AsyncMock()

        signal = {
            "symbol": "FTT",
            "direction": "short",
            "entry_price": 1.05,
            "size": 191.4,
            "strategy": "trend_momentum",
        }
        order = Order(
            order_id="12345",
            symbol="FTT",
            side="sell",
            size=191.4,
            avg_price=1.05,
            filled_size=191.4,
            status=OrderStatus.FILLED,
        )

        await real_engine._handle_order_filled(signal, order)

        assert "FTT" in real_engine._tp_sl_placed_size
        assert real_engine._tp_sl_placed_size["FTT"] == pytest.approx(191.4)
        assert "FTT" in real_engine.active_positions


# =============================================================================
# Cleanup Tests
# =============================================================================


class TestCleanup:
    """Verify tracking dicts are cleaned up properly."""

    @pytest.mark.asyncio
    async def test_partial_fill_tracking_cleared_on_cancel(self) -> None:
        """_partial_fill_first_seen is cleared when stale order is cancelled."""
        engine = _make_engine()
        order = _make_pending_order(size=191.4)
        # Make order stale (submitted 120s ago, timeout is 60s)
        order.submitted_at = datetime.now(timezone.utc) - timedelta(seconds=120)
        engine.pending_orders["12345"] = order
        engine._partial_fill_first_seen["12345"] = (
            datetime.now(timezone.utc) - timedelta(seconds=30),
            57.7,
        )

        engine.client.cancel_order = AsyncMock()

        await engine._cancel_stale_orders()

        assert "12345" not in engine._partial_fill_first_seen
        assert "12345" not in engine.pending_orders

    @pytest.mark.asyncio
    async def test_partial_fill_cleared_on_external_cancel(self) -> None:
        """_partial_fill_first_seen is cleared when order is externally cancelled."""
        engine = _make_engine()
        order = _make_pending_order(size=191.4)
        engine.pending_orders["12345"] = order
        engine._partial_fill_first_seen["12345"] = (
            datetime.now(timezone.utc),
            57.7,
        )

        engine.client.get_open_orders = AsyncMock(return_value=[])
        engine.client.get_positions = AsyncMock(return_value=[])  # No position = cancel

        await engine._poll_pending_order_fills()

        assert "12345" not in engine._partial_fill_first_seen
        assert "12345" not in engine.pending_orders


# =============================================================================
# Integration-like: Full Scenario
# =============================================================================


class TestPartialFillFullScenario:
    """End-to-end scenario: FTT order filled in 2 tranches."""

    @pytest.mark.asyncio
    async def test_ftt_two_tranche_fill(self) -> None:
        """
        Simulate the real FTT bug:
        1. SELL 191.4 FTT limit
        2. First tranche: 57.7 fills
        3. Grace period waits
        4. Second tranche: 133.7 fills (total 191.4)
        5. Full fill detected, TP/SL placed for 191.4
        """
        engine = _make_engine()
        order = _make_pending_order(order_id="12345", symbol="FTT", side="sell", size=191.4)
        engine.pending_orders["12345"] = order

        engine.client.get_open_orders = AsyncMock(return_value=[])

        # --- Poll 1: First tranche (57.7) ---
        engine.client.get_positions = AsyncMock(return_value=[
            {"symbol": "FTT", "size": -57.7, "entryPrice": 1.05, "markPrice": 1.05},
        ])
        await engine._poll_pending_order_fills()

        # Should be in grace period
        assert "12345" in engine._partial_fill_first_seen
        engine._handle_order_filled.assert_not_called()

        # --- Poll 2: More fills arrive (total 120) ---
        engine.client.get_positions = AsyncMock(return_value=[
            {"symbol": "FTT", "size": -120.0, "entryPrice": 1.048, "markPrice": 1.05},
        ])
        await engine._poll_pending_order_fills()

        # Grace timer reset, still waiting
        _, last_size = engine._partial_fill_first_seen["12345"]
        assert last_size == pytest.approx(120.0)
        engine._handle_order_filled.assert_not_called()

        # --- Poll 3: Full fill (191.4) ---
        engine.client.get_positions = AsyncMock(return_value=[
            {"symbol": "FTT", "size": -191.4, "entryPrice": 1.048, "markPrice": 1.05},
        ])
        await engine._poll_pending_order_fills()

        # 191.4 / 191.4 = 100% >= 95%, so should process immediately
        engine._handle_order_filled.assert_called_once()
        call_args = engine._handle_order_filled.call_args
        filled_order = call_args[0][1]
        assert filled_order.filled_size == pytest.approx(191.4)
        assert "12345" not in engine.pending_orders
        assert "12345" not in engine._partial_fill_first_seen

    @pytest.mark.asyncio
    async def test_size_growth_after_tp_sl_placed(self) -> None:
        """
        If TP/SL were placed for partial fill and then more fills arrive
        (detected by sync), TP/SL should be re-placed for full size.
        """
        engine = _make_engine()

        # Position already open with TP/SL for 57.7
        pos = ExecutionPosition(
            symbol="FTT",
            side="short",
            size=57.7,
            entry_price=1.05,
            current_price=1.05,
            status=PositionStatus.OPEN,
            opened_at=datetime.now(timezone.utc),
            tp_order_id="10001",
            tp_price=0.98,
            sl_order_id="20001",
            sl_price=1.12,
            highest_price=1.05,
            lowest_price=1.05,
        )
        engine.active_positions["FTT"] = pos
        engine._tp_sl_placed_size["FTT"] = 57.7

        # Exchange now shows 191.4
        engine.client.get_positions = AsyncMock(return_value=[
            {
                "symbol": "FTT",
                "size": -191.4,
                "entryPrice": 1.048,
                "markPrice": 1.05,
                "unrealizedPnl": -0.5,
            },
        ])

        # Use real _update_tp_sl_for_size_change
        engine._update_tp_sl_for_size_change = (
            ExecutionEngineService._update_tp_sl_for_size_change.__get__(engine)
        )

        await engine._sync_positions_from_exchange()

        # Size updated
        assert pos.size == pytest.approx(191.4)

        # TP/SL should have been re-placed
        # 2 cancels (old TP + old SL) + 2 placements (new TP + new SL)
        assert engine.client.cancel_order.call_count == 2
        assert engine._place_trigger_with_retry.call_count == 2

        # Check new trigger orders use full size
        for call in engine._place_trigger_with_retry.call_args_list:
            assert call[1]["size"] == pytest.approx(191.4)

        # Tracking updated
        assert engine._tp_sl_placed_size["FTT"] == pytest.approx(191.4)
