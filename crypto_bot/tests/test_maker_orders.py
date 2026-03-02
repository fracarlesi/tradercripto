"""
Tests for Maker (Post-Only) Order Entry
=========================================

When ``entry_mode`` is ``"maker"``, entry orders are placed at the best
bid/ask with ``time_in_force="Alo"`` (Add Liquidity Only).  This guarantees
maker execution (lower fees) or rejection (with automatic taker fallback).

Key scenarios tested:
1. BUY maker posts at best bid with Alo
2. SELL maker posts at best ask with Alo
3. Taker mode unchanged (default, crossing offset)
4. Reprice when best price moves
5. No reprice when price is stable
6. Max reprices reached — stops repricing
7. Empty orderbook — fallback to taker
8. Alo rejection — fallback to taker

Run:
    pytest crypto_bot/tests/test_maker_orders.py -v
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

from crypto_bot.services.execution_engine import (
    ExecutionEngineService,
    ExecutionMetrics,
    Order,
    OrderStatus,
)


# =============================================================================
# Helpers
# =============================================================================


def _make_engine(entry_mode: str = "maker") -> ExecutionEngineService:
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
    engine._market_states = {}
    engine.metrics = ExecutionMetrics()
    engine.client = AsyncMock()
    engine.client.get_orderbook = AsyncMock()
    engine.client.place_order = AsyncMock(return_value={
        "success": True,
        "orderId": "10001",
    })
    engine.client.cancel_order = AsyncMock(return_value=True)
    engine._handle_order_filled = AsyncMock()
    engine.publish = AsyncMock()
    engine._send_alert = AsyncMock()

    class _ExecConfig:
        order_type = "limit"
        max_slippage_pct = 0.25
        limit_timeout_seconds = 300
        retry_attempts = 3
        retry_delay_seconds = 1.0
        maker_reprice_interval_seconds = 10
        maker_max_reprices = 6

    _ExecConfig.entry_mode = entry_mode
    engine._exec_config = _ExecConfig()

    class _RiskConfig:
        stop_loss_pct = 0.8
        take_profit_pct = 1.6
        leverage = 10
        breakeven_threshold_pct = 1.2

    class _StopsConfig:
        initial_atr_mult = 2.5
        trailing_atr_mult = 2.5

    class _ServicesConfig:
        execution_engine = _ExecConfig()

    class _BotConfig:
        risk = _RiskConfig()
        stops = _StopsConfig()
        services = _ServicesConfig()

    engine._bot_config = _BotConfig()

    return engine


def _make_order(
    order_id: str = "10001",
    symbol: str = "ETH",
    side: str = "buy",
    size: float = 0.5,
    price: float = 3000.0,
    entry_mode: str = "taker",
) -> Order:
    """Create a pending order."""
    return Order(
        order_id=order_id,
        symbol=symbol,
        side=side,
        size=size,
        price=price,
        order_type="limit",
        status=OrderStatus.SUBMITTED,
        signal_id="sig_001",
        strategy="trend_momentum",
        submitted_at=datetime.now(timezone.utc),
        entry_mode=entry_mode,
    )


def _orderbook(best_bid: float = 3000.0, best_ask: float = 3001.0) -> dict:
    """Create a simple orderbook with one level per side."""
    return {
        "bids": [[best_bid, 10.0]],
        "asks": [[best_ask, 10.0]],
    }


# =============================================================================
# Test: BUY maker posts at best bid
# =============================================================================


@pytest.mark.asyncio
async def test_buy_maker_posts_at_best_bid():
    """BUY maker order should be placed at best bid with Alo TIF."""
    engine = _make_engine(entry_mode="maker")
    engine.client.get_orderbook.return_value = _orderbook(
        best_bid=3000.0, best_ask=3001.0,
    )
    order = _make_order(side="buy", price=3000.5)

    result = await engine._place_maker_order(order, is_buy=True)

    engine.client.place_order.assert_called_once_with(
        symbol="ETH",
        is_buy=True,
        size=0.5,
        price=3000.0,  # best bid, not the original price
        order_type="limit",
        reduce_only=False,
        time_in_force="Alo",
    )
    assert result["success"] is True
    assert order.entry_mode == "maker"
    assert order.price == 3000.0
    assert engine.metrics.maker_orders_submitted == 1


# =============================================================================
# Test: SELL maker posts at best ask
# =============================================================================


@pytest.mark.asyncio
async def test_sell_maker_posts_at_best_ask():
    """SELL maker order should be placed at best ask with Alo TIF."""
    engine = _make_engine(entry_mode="maker")
    engine.client.get_orderbook.return_value = _orderbook(
        best_bid=3000.0, best_ask=3001.0,
    )
    order = _make_order(side="sell", price=3000.5)

    result = await engine._place_maker_order(order, is_buy=False)

    engine.client.place_order.assert_called_once_with(
        symbol="ETH",
        is_buy=False,
        size=0.5,
        price=3001.0,  # best ask
        order_type="limit",
        reduce_only=False,
        time_in_force="Alo",
    )
    assert result["success"] is True
    assert order.entry_mode == "maker"
    assert order.price == 3001.0


# =============================================================================
# Test: Taker mode unchanged (default behaviour)
# =============================================================================


@pytest.mark.asyncio
async def test_taker_mode_uses_crossing_offset():
    """Default taker mode should apply 0.1% crossing offset."""
    engine = _make_engine(entry_mode="taker")
    order = _make_order(side="buy", price=3000.0)

    signal = {"symbol": "ETH", "direction": "long"}
    result = await engine._place_order_on_exchange(order, signal)

    # Should use taker fallback (price * 1.001)
    call_kwargs = engine.client.place_order.call_args.kwargs
    assert call_kwargs["price"] == pytest.approx(3000.0 * 1.001, rel=1e-6)
    assert call_kwargs["time_in_force"] == "Gtc"


# =============================================================================
# Test: Reprice when price moves
# =============================================================================


@pytest.mark.asyncio
async def test_reprice_when_price_moves():
    """Maker order should be repriced when best bid/ask moves > 0.01%."""
    engine = _make_engine(entry_mode="maker")
    order = _make_order(
        order_id="10001",
        side="buy",
        price=3000.0,
        entry_mode="maker",
    )
    # Set submitted_at in the past to trigger reprice interval
    order.submitted_at = datetime.now(timezone.utc) - timedelta(seconds=15)
    engine.pending_orders["10001"] = order

    # New orderbook: bid moved from 3000 to 3005 (0.17% > 0.01%)
    engine.client.get_orderbook.return_value = _orderbook(
        best_bid=3005.0, best_ask=3006.0,
    )
    engine.client.place_order.return_value = {
        "success": True,
        "orderId": "10002",
    }

    await engine._reprice_maker_orders()

    # Should have cancelled old order and placed new one
    engine.client.cancel_order.assert_called_once_with("ETH", 10001)
    engine.client.place_order.assert_called_once_with(
        symbol="ETH",
        is_buy=True,
        size=0.5,
        price=3005.0,
        order_type="limit",
        reduce_only=False,
        time_in_force="Alo",
    )
    # Old order removed, new one tracked
    assert "10001" not in engine.pending_orders
    assert "10002" in engine.pending_orders
    assert engine.pending_orders["10002"].reprice_count == 1
    assert engine.metrics.maker_orders_repriced == 1


# =============================================================================
# Test: No reprice when price is stable
# =============================================================================


@pytest.mark.asyncio
async def test_no_reprice_when_price_stable():
    """Should NOT reprice when price hasn't moved significantly."""
    engine = _make_engine(entry_mode="maker")
    order = _make_order(
        order_id="10001",
        side="buy",
        price=3000.0,
        entry_mode="maker",
    )
    order.submitted_at = datetime.now(timezone.utc) - timedelta(seconds=15)
    engine.pending_orders["10001"] = order

    # Price barely moved (3000.0 -> 3000.2 = 0.007% < 0.01%)
    engine.client.get_orderbook.return_value = _orderbook(
        best_bid=3000.2, best_ask=3001.2,
    )

    await engine._reprice_maker_orders()

    # Should NOT cancel or re-place
    engine.client.cancel_order.assert_not_called()
    engine.client.place_order.assert_not_called()
    assert "10001" in engine.pending_orders


# =============================================================================
# Test: Max reprices reached
# =============================================================================


@pytest.mark.asyncio
async def test_max_reprices_stops_repricing():
    """Should stop repricing after max_reprices is reached."""
    engine = _make_engine(entry_mode="maker")
    order = _make_order(
        order_id="10001",
        side="buy",
        price=3000.0,
        entry_mode="maker",
    )
    order.reprice_count = 6  # Already at max
    order.submitted_at = datetime.now(timezone.utc) - timedelta(seconds=15)
    engine.pending_orders["10001"] = order

    engine.client.get_orderbook.return_value = _orderbook(
        best_bid=3100.0, best_ask=3101.0,  # Big move
    )

    await engine._reprice_maker_orders()

    # Should NOT reprice — limit reached
    engine.client.cancel_order.assert_not_called()
    engine.client.place_order.assert_not_called()


# =============================================================================
# Test: Empty orderbook falls back to taker
# =============================================================================


@pytest.mark.asyncio
async def test_empty_orderbook_fallback_to_taker():
    """Empty orderbook should trigger taker fallback."""
    engine = _make_engine(entry_mode="maker")
    engine.client.get_orderbook.return_value = {"bids": [], "asks": []}
    order = _make_order(side="buy", price=3000.0)

    result = await engine._place_maker_order(order, is_buy=True)

    # Should fall back to taker (price * 1.001, Gtc)
    call_kwargs = engine.client.place_order.call_args.kwargs
    assert call_kwargs["time_in_force"] == "Gtc"
    assert call_kwargs["price"] == pytest.approx(3000.0 * 1.001, rel=1e-6)
    assert order.entry_mode == "taker"


# =============================================================================
# Test: Alo rejection falls back to taker
# =============================================================================


@pytest.mark.asyncio
async def test_alo_rejection_fallback_to_taker():
    """Alo rejection (would cross spread) should trigger taker fallback."""
    engine = _make_engine(entry_mode="maker")
    engine.client.get_orderbook.return_value = _orderbook(
        best_bid=3000.0, best_ask=3001.0,
    )
    # First call: Alo rejected; second call: taker succeeds
    engine.client.place_order.side_effect = [
        {"success": False},  # Alo rejected
        {"success": True, "orderId": "10002"},  # Taker fallback
    ]
    order = _make_order(side="buy", price=3000.0)

    result = await engine._place_maker_order(order, is_buy=True)

    assert engine.client.place_order.call_count == 2
    # Second call should be taker (Gtc)
    second_call = engine.client.place_order.call_args_list[1].kwargs
    assert second_call["time_in_force"] == "Gtc"
    assert result["success"] is True
    assert order.entry_mode == "taker"
