"""
Tests for startup safety fixes:
1. Orphan order cancellation on startup
2. Real opened_at recovery from fills API
3. TP/SL reconciliation (duplicate cleanup)
4. Startup safety check warnings

Run:
    pytest crypto_bot/tests/test_startup_safety.py -v
"""

import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from crypto_bot.services.execution_engine import (
    ExecutionEngineService,
    ExecutionPosition,
    PositionStatus,
)


# =============================================================================
# Test Fixtures
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
    client.close_position = AsyncMock(return_value={"success": True})
    client.cancel_order = AsyncMock()
    client.cancel_all_orders = AsyncMock(return_value=0)
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
        take_profit_pct = 2.5
        stop_loss_pct = 1.0
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
    return svc


# =============================================================================
# Fix 1: Orphan Order Cancellation
# =============================================================================

class TestOrphanOrderCancellation:
    """Verify that non-reduce-only orders are cancelled on startup."""

    @pytest.mark.asyncio
    async def test_cancels_non_reduce_only_orders(self, engine, mock_client):
        """Non-reduce-only orders from previous instances are cancelled."""
        mock_client.get_open_orders.return_value = [
            {"orderId": 111, "symbol": "WLD", "side": "sell", "price": 0.38,
             "reduceOnly": False},
            {"orderId": 222, "symbol": "CAKE", "side": "sell", "price": 1.30,
             "reduceOnly": False},
            {"orderId": 333, "symbol": "WLD", "side": "buy", "price": 0.35,
             "reduceOnly": True},  # TP — should NOT be cancelled
        ]

        await engine._cancel_orphan_orders_on_startup()

        assert mock_client.cancel_order.call_count == 2
        cancelled_ids = [
            call.args[1] for call in mock_client.cancel_order.call_args_list
        ]
        assert 111 in cancelled_ids
        assert 222 in cancelled_ids
        assert 333 not in cancelled_ids

    @pytest.mark.asyncio
    async def test_no_orphans_logs_clean(self, engine, mock_client):
        """When no orphan orders exist, logs 'no orphan orders found'."""
        mock_client.get_open_orders.return_value = [
            {"orderId": 100, "symbol": "ETH", "reduceOnly": True},
        ]

        await engine._cancel_orphan_orders_on_startup()
        mock_client.cancel_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_order_book(self, engine, mock_client):
        """No orders at all — should not error."""
        mock_client.get_open_orders.return_value = []
        await engine._cancel_orphan_orders_on_startup()
        mock_client.cancel_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_api_error_handled_gracefully(self, engine, mock_client):
        """API failure during orphan cleanup should not crash startup."""
        mock_client.get_open_orders.side_effect = Exception("rate limit")
        await engine._cancel_orphan_orders_on_startup()  # should not raise


# =============================================================================
# Fix 2: Real opened_at Recovery
# =============================================================================

class TestRealOpenedAt:
    """Verify opened_at is recovered from fills API."""

    @pytest.mark.asyncio
    async def test_recovers_real_open_time(self, engine, mock_client):
        """Position opened_at comes from the earliest 'Open' fill."""
        real_time = datetime(2026, 3, 8, 15, 45, 0, tzinfo=timezone.utc)
        mock_client.get_fills.return_value = [
            {"symbol": "WLD", "dir": "Close Short", "time": datetime(2026, 3, 8, 19, 0, tzinfo=timezone.utc), "fillId": "f2"},
            {"symbol": "WLD", "dir": "Open Short", "time": real_time, "fillId": "f1"},
            {"symbol": "ETH", "dir": "Open Long", "time": datetime(2026, 3, 8, 10, 0, tzinfo=timezone.utc), "fillId": "f3"},
        ]

        result = await engine._get_position_open_time("WLD")
        assert result == real_time

    @pytest.mark.asyncio
    async def test_fallback_when_no_fills(self, engine, mock_client):
        """Falls back to now-1h when no opening fills found."""
        mock_client.get_fills.return_value = []

        before = datetime.now(timezone.utc) - timedelta(hours=1, minutes=1)
        result = await engine._get_position_open_time("WLD")
        after = datetime.now(timezone.utc) - timedelta(minutes=59)

        assert before <= result <= after

    @pytest.mark.asyncio
    async def test_fallback_on_api_error(self, engine, mock_client):
        """Falls back gracefully on API errors."""
        mock_client.get_fills.side_effect = Exception("timeout")

        result = await engine._get_position_open_time("WLD")
        # Should return approximately now - 1h
        expected = datetime.now(timezone.utc) - timedelta(hours=1)
        assert abs((result - expected).total_seconds()) < 5

    @pytest.mark.asyncio
    async def test_picks_earliest_open_fill(self, engine, mock_client):
        """When multiple opening fills exist, picks the earliest."""
        t1 = datetime(2026, 3, 8, 14, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 3, 8, 14, 0, 5, tzinfo=timezone.utc)
        mock_client.get_fills.return_value = [
            {"symbol": "WLD", "dir": "Open Short", "time": t2, "fillId": "f2"},
            {"symbol": "WLD", "dir": "Open Short", "time": t1, "fillId": "f1"},
        ]

        result = await engine._get_position_open_time("WLD")
        assert result == t1


# =============================================================================
# Fix 3: TP/SL Reconciliation
# =============================================================================

class TestTpSlReconciliation:
    """Verify duplicate TP/SL orders are cleaned up."""

    @pytest.mark.asyncio
    async def test_cancels_duplicate_tp_orders(self, engine, mock_client, mock_config):
        """Extra TP orders are cancelled, keeping the closest to config."""
        position = ExecutionPosition(
            symbol="CAKE",
            side="short",
            size=42.3,
            entry_price=1.3087,
            current_price=1.30,
            status=PositionStatus.OPEN,
        )

        # 2 TPs (below entry for short) + 2 SLs (above entry for short)
        mock_client.get_open_orders.return_value = [
            {"orderId": 1, "symbol": "CAKE", "reduceOnly": True,
             "limitPx": 1.2629, "price": 1.2629},  # old TP 3.5%
            {"orderId": 2, "symbol": "CAKE", "reduceOnly": True,
             "limitPx": 1.2760, "price": 1.2760},  # new TP 2.5% ← should keep
            {"orderId": 3, "symbol": "CAKE", "reduceOnly": True,
             "limitPx": 1.3218, "price": 1.3218},  # SL 1%
            {"orderId": 4, "symbol": "CAKE", "reduceOnly": True,
             "limitPx": 1.3218, "price": 1.3218},  # duplicate SL ← should cancel
        ]

        await engine._ensure_tp_sl_for_position(position)

        # Should have cancelled 2 duplicates (1 TP + 1 SL)
        assert mock_client.cancel_order.call_count == 2
        cancelled_ids = [
            call.args[1] for call in mock_client.cancel_order.call_args_list
        ]
        # The old 3.5% TP (id=1) should be cancelled in favor of 2.5% (id=2)
        assert 1 in cancelled_ids

    @pytest.mark.asyncio
    async def test_no_duplicates_no_cancellation(self, engine, mock_client):
        """Exactly 1 TP + 1 SL — no cancellation needed."""
        position = ExecutionPosition(
            symbol="WLD",
            side="short",
            size=153.0,
            entry_price=0.3636,
            current_price=0.36,
            status=PositionStatus.OPEN,
        )

        mock_client.get_open_orders.return_value = [
            {"orderId": 10, "symbol": "WLD", "reduceOnly": True,
             "limitPx": 0.3509, "price": 0.3509},  # TP
            {"orderId": 11, "symbol": "WLD", "reduceOnly": True,
             "limitPx": 0.3672, "price": 0.3672},  # SL
        ]

        await engine._ensure_tp_sl_for_position(position)
        mock_client.cancel_order.assert_not_called()
        assert position.tp_price == 0.3509
        assert position.sl_price == 0.3672

    def test_classify_tp_sl_short(self, engine):
        """Correctly classifies TP (below entry) and SL (above entry) for shorts."""
        position = ExecutionPosition(
            symbol="CAKE", side="short", entry_price=1.30,
        )
        orders = [
            {"limitPx": 1.25, "price": 1.25},  # TP (below)
            {"limitPx": 1.27, "price": 1.27},  # TP (below)
            {"limitPx": 1.32, "price": 1.32},  # SL (above)
        ]

        tp, sl = engine._classify_tp_sl_orders(orders, position)
        assert len(tp) == 2
        assert len(sl) == 1

    def test_classify_tp_sl_long(self, engine):
        """Correctly classifies TP (above entry) and SL (below entry) for longs."""
        position = ExecutionPosition(
            symbol="WLFI", side="long", entry_price=0.10,
        )
        orders = [
            {"limitPx": 0.103, "price": 0.103},  # TP (above)
            {"limitPx": 0.096, "price": 0.096},  # SL (below)
            {"limitPx": 0.096, "price": 0.096},  # duplicate SL
        ]

        tp, sl = engine._classify_tp_sl_orders(orders, position)
        assert len(tp) == 1
        assert len(sl) == 2
