"""
Tests for Bug B fix: stray trigger-order cleanup.

Residual reduce-only TP/SL trigger orders from previous (pre-LLM-only) deploys
can fire on the exchange, closing positions before the bot's min_hold timer
elapses.  ``ExecutionEngineService._cleanup_stray_trigger_orders`` cancels
reduce-only trigger orders whose symbol has no tracked active position.

Run:
    pytest crypto_bot/tests/test_stray_trigger_cleanup.py -v
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from crypto_bot.services.execution_engine import (
    ExecutionEngineService,
    ExecutionPosition,
    PositionStatus,
)


# =============================================================================
# Fixtures (mirrors test_startup_safety.py conventions)
# =============================================================================

@pytest.fixture
def mock_bus():
    bus = AsyncMock()
    bus.subscribe = AsyncMock()
    bus.publish = AsyncMock()
    return bus


@pytest.fixture
def mock_client():
    client = AsyncMock()
    client.get_positions = AsyncMock(return_value=[])
    client.get_fills = AsyncMock(return_value=[])
    client.get_open_orders = AsyncMock(return_value=[])
    client.cancel_order = AsyncMock(return_value=True)
    return client


@pytest.fixture
def mock_config():
    class _ExecConfig:
        order_type = "limit"
        max_slippage_pct = 0.1
        limit_timeout_seconds = 60
        retry_attempts = 3
        retry_delay_seconds = 5
        position_sync_interval = 30
        fill_sync_interval = 10

    class _RiskConfig:
        take_profit_pct = 0.0  # LLM-only mode
        stop_loss_pct = 0.0
        leverage = 3

    class _StopsConfig:
        initial_atr_mult = 2.5
        trailing_atr_mult = 0
        minimal_roi = {"0": 9.0}
        max_hold_hours = 6.0

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
def engine(mock_bus, mock_client, mock_config):
    svc = ExecutionEngineService(
        bus=mock_bus,
        config=mock_config,
        client=mock_client,
    )
    # Silence alerts side-effects
    svc._send_alert = AsyncMock()  # type: ignore[method-assign]
    return svc


# =============================================================================
# Tests
# =============================================================================

class TestStrayTriggerCleanup:
    """Verify stray reduce-only TP/SL trigger orders are cancelled."""

    @pytest.mark.asyncio
    async def test_cancels_stray_trigger_when_no_position(self, engine, mock_client):
        """Stray reduce-only trigger on a symbol with NO tracked position → cancelled."""
        mock_client.get_open_orders.return_value = [
            {
                "orderId": 999,
                "symbol": "XLM",
                "side": "sell",
                "price": 0.42,
                "orderType": "Stop Market",
                "reduceOnly": True,
            },
        ]

        await engine._cleanup_stray_trigger_orders()

        mock_client.cancel_order.assert_awaited_once_with("XLM", 999)

    @pytest.mark.asyncio
    async def test_ignores_non_reduce_only_orders(self, engine, mock_client):
        """Non-reduce-only limit orders are handled by the orphan cleanup, NOT here."""
        mock_client.get_open_orders.return_value = [
            {
                "orderId": 1,
                "symbol": "ETH",
                "side": "buy",
                "price": 3000.0,
                "orderType": "Limit",
                "reduceOnly": False,
            },
        ]

        await engine._cleanup_stray_trigger_orders()
        mock_client.cancel_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_non_trigger_reduce_only(self, engine, mock_client):
        """Reduce-only plain limit (non-trigger) → NOT touched by this method."""
        mock_client.get_open_orders.return_value = [
            {
                "orderId": 2,
                "symbol": "ETH",
                "side": "sell",
                "price": 3100.0,
                "orderType": "Limit",
                "reduceOnly": True,
            },
        ]

        await engine._cleanup_stray_trigger_orders()
        mock_client.cancel_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_preserves_tracked_tp_sl(self, engine, mock_client):
        """A tracked position's TP/SL trigger IDs must NEVER be cancelled."""
        engine.active_positions["BTC"] = ExecutionPosition(
            symbol="BTC",
            side="long",
            size=0.01,
            entry_price=50000.0,
            status=PositionStatus.OPEN,
            tp_order_id="555",
            sl_order_id="666",
        )
        mock_client.get_open_orders.return_value = [
            {
                "orderId": 555,
                "symbol": "BTC",
                "side": "sell",
                "price": 52000.0,
                "orderType": "Take Profit Market",
                "reduceOnly": True,
            },
            {
                "orderId": 666,
                "symbol": "BTC",
                "side": "sell",
                "price": 48000.0,
                "orderType": "Stop Market",
                "reduceOnly": True,
            },
        ]

        await engine._cleanup_stray_trigger_orders()
        mock_client.cancel_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_preserves_tracked_position_even_if_id_mismatch(
        self, engine, mock_client
    ):
        """If symbol IS tracked, defer to _ensure_tp_sl_for_position (don't double-cancel)."""
        engine.active_positions["BTC"] = ExecutionPosition(
            symbol="BTC",
            side="long",
            size=0.01,
            entry_price=50000.0,
            status=PositionStatus.OPEN,
            tp_order_id="555",
            sl_order_id="666",
        )
        # Unknown trigger id but same symbol — leave for dedicated reconciler.
        mock_client.get_open_orders.return_value = [
            {
                "orderId": 7777,
                "symbol": "BTC",
                "side": "sell",
                "price": 48500.0,
                "orderType": "Stop Market",
                "reduceOnly": True,
            },
        ]

        await engine._cleanup_stray_trigger_orders()
        mock_client.cancel_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_settling_symbols(self, engine, mock_client):
        """Never cancel while a symbol is mid-open/close (partial sync state)."""
        engine._settling_symbols.add("XLM")
        mock_client.get_open_orders.return_value = [
            {
                "orderId": 999,
                "symbol": "XLM",
                "side": "sell",
                "price": 0.42,
                "orderType": "Stop Market",
                "reduceOnly": True,
            },
        ]

        await engine._cleanup_stray_trigger_orders()
        mock_client.cancel_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_closing_positions(self, engine, mock_client):
        """Never cancel while a close order is already in flight."""
        engine._closing_positions.add("XLM")
        mock_client.get_open_orders.return_value = [
            {
                "orderId": 999,
                "symbol": "XLM",
                "side": "sell",
                "price": 0.42,
                "orderType": "Take Profit Market",
                "reduceOnly": True,
            },
        ]

        await engine._cleanup_stray_trigger_orders()
        mock_client.cancel_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_api_error_handled_gracefully(self, engine, mock_client):
        """get_open_orders raising must not crash the monitor loop."""
        mock_client.get_open_orders.side_effect = Exception("rate limit")
        await engine._cleanup_stray_trigger_orders()  # should not raise
        mock_client.cancel_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancel_failure_does_not_abort_remaining(self, engine, mock_client):
        """A failure cancelling one order must not stop the rest."""
        mock_client.get_open_orders.return_value = [
            {"orderId": 101, "symbol": "A", "side": "sell", "price": 1.0,
             "orderType": "Stop Market", "reduceOnly": True},
            {"orderId": 102, "symbol": "B", "side": "sell", "price": 2.0,
             "orderType": "Stop Market", "reduceOnly": True},
        ]
        mock_client.cancel_order.side_effect = [Exception("boom"), True]

        await engine._cleanup_stray_trigger_orders()
        assert mock_client.cancel_order.await_count == 2

    @pytest.mark.asyncio
    async def test_empty_order_book_is_noop(self, engine, mock_client):
        mock_client.get_open_orders.return_value = []
        await engine._cleanup_stray_trigger_orders()
        mock_client.cancel_order.assert_not_called()
