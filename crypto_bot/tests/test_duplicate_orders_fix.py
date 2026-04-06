# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportGeneralTypeIssues=false
"""
Tests for the "duplicate orders fall-through" fix in
``ExecutionEngineService._ensure_tp_sl_for_position``.
=====================================================================

Bug (pre-fix): the ``elif len(reduce_only_orders) == 1`` branch recovered the
existing order's price into ``position.tp_price`` / ``position.sl_price`` but
did NOT ``return``. Execution then fell through to the "fresh place" block,
which placed BOTH a new TP and a new SL — duplicating whichever side the
exchange already had.

Fix: when exactly one reduce-only order exists, place ONLY the missing side
and ``return``. The existing order is left untouched.

Diagnostic reference: 2026-04-06.

Run:
    pytest crypto_bot/tests/test_duplicate_orders_fix.py -v
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from crypto_bot.services.execution_engine import (
    ExecutionEngineService,
    PositionStatus,
)


# =============================================================================
# Helpers
# =============================================================================


def _make_position(symbol: str = "BTC", entry: float = 100.0, size: float = 1.0):
    return SimpleNamespace(
        symbol=symbol,
        side="long",
        size=size,
        entry_price=entry,
        current_price=entry,
        unrealized_pnl=0.0,
        status=PositionStatus.OPEN,
        tp_price=None,
        sl_price=None,
        tp_order_id=None,
        sl_order_id=None,
        opened_at=datetime.now(timezone.utc),
    )


def _make_stub(
    *,
    open_orders: list[dict],
    stop_loss_pct: float = 2.0,
    take_profit_pct: float = 4.0,
):
    """Build a stub of ExecutionEngineService sufficient to drive
    ``_ensure_tp_sl_for_position``."""
    client = MagicMock()
    client.get_open_orders = AsyncMock(return_value=open_orders)

    # ``_round_price`` used by _validate_stop_distance — passthrough.
    client._round_price = lambda p: float(p)

    # Capture place_trigger_order calls.
    place_trigger_calls: list[dict] = []

    async def _fake_place_trigger(**kwargs):
        place_trigger_calls.append(kwargs)
        return {"orderId": f"order-{len(place_trigger_calls)}"}

    client.place_trigger_order = AsyncMock(side_effect=_fake_place_trigger)
    client.place_order = AsyncMock(return_value={"orderId": "market-1"})

    bot_config = SimpleNamespace(
        risk=SimpleNamespace(
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
        )
    )

    stub = SimpleNamespace(
        client=client,
        _bot_config=bot_config,
        _tp_sl_confirmed=set(),
        _tp_sl_placed_size={},
        _logger=logging.getLogger("test_duplicate_orders_fix"),
        _place_trigger_calls=place_trigger_calls,
    )

    # Bind real helper methods so the production logic runs unchanged.
    stub._classify_tp_sl_orders = lambda orders, pos: ExecutionEngineService._classify_tp_sl_orders(  # type: ignore[arg-type]
        stub, orders, pos
    )
    stub._validate_stop_distance = lambda *a, **kw: ExecutionEngineService._validate_stop_distance(  # type: ignore[arg-type]
        stub, *a, **kw
    )
    stub._place_trigger_with_retry = lambda **kw: ExecutionEngineService._place_trigger_with_retry(  # type: ignore[arg-type]
        stub, **kw
    )
    stub._cancel_duplicate_order = AsyncMock()
    stub._send_alert = AsyncMock()

    return stub


async def _run(stub, position) -> None:
    await ExecutionEngineService._ensure_tp_sl_for_position(stub, position)  # type: ignore[arg-type]


# =============================================================================
# Tests
# =============================================================================


@pytest.mark.asyncio
async def test_two_orders_no_duplicate_placed():
    """When 2 reduce-only orders already exist (1 TP + 1 SL), the function
    must reconcile and return early — placing zero new trigger orders."""
    pos = _make_position(symbol="BTC", entry=100.0)
    open_orders = [
        # TP for long: price > entry
        {"symbol": "BTC", "reduceOnly": True, "limitPx": 104.0,
         "price": 104.0, "orderId": "tp-existing"},
        # SL for long: price < entry
        {"symbol": "BTC", "reduceOnly": True, "limitPx": 98.0,
         "price": 98.0, "orderId": "sl-existing"},
    ]
    stub = _make_stub(open_orders=open_orders)

    await _run(stub, pos)

    assert len(stub._place_trigger_calls) == 0, (
        f"Expected NO new trigger orders, got {len(stub._place_trigger_calls)}: "
        f"{stub._place_trigger_calls}"
    )
    assert "BTC" in stub._tp_sl_confirmed
    assert pos.tp_price == 104.0
    assert pos.sl_price == 98.0


@pytest.mark.asyncio
async def test_one_tp_only_places_missing_sl():
    """1 existing TP, 0 SL → place ONLY the missing SL (not a duplicate TP)."""
    pos = _make_position(symbol="ETH", entry=100.0)
    open_orders = [
        {"symbol": "ETH", "reduceOnly": True, "limitPx": 105.0,
         "price": 105.0, "orderId": "tp-existing"},
    ]
    stub = _make_stub(open_orders=open_orders, stop_loss_pct=2.0, take_profit_pct=4.0)

    await _run(stub, pos)

    assert len(stub._place_trigger_calls) == 1, (
        f"Expected exactly 1 new trigger order, got {len(stub._place_trigger_calls)}"
    )
    call = stub._place_trigger_calls[0]
    assert call["tpsl"] == "sl", f"Expected SL placement, got {call['tpsl']}"
    # SL for long at 2% below entry 100 = 98.0
    assert call["trigger_price"] == pytest.approx(98.0)
    # TP price recovered from existing order
    assert pos.tp_price == 105.0
    assert pos.tp_order_id == "tp-existing"
    # SL set from new placement
    assert pos.sl_price == pytest.approx(98.0)
    assert "ETH" in stub._tp_sl_confirmed


@pytest.mark.asyncio
async def test_one_sl_only_places_missing_tp():
    """1 existing SL, 0 TP → place ONLY the missing TP (not a duplicate SL)."""
    pos = _make_position(symbol="SOL", entry=100.0)
    open_orders = [
        {"symbol": "SOL", "reduceOnly": True, "limitPx": 97.0,
         "price": 97.0, "orderId": "sl-existing"},
    ]
    stub = _make_stub(open_orders=open_orders, stop_loss_pct=2.0, take_profit_pct=4.0)

    await _run(stub, pos)

    assert len(stub._place_trigger_calls) == 1, (
        f"Expected exactly 1 new trigger order, got {len(stub._place_trigger_calls)}"
    )
    call = stub._place_trigger_calls[0]
    assert call["tpsl"] == "tp", f"Expected TP placement, got {call['tpsl']}"
    # TP for long at 4% above entry 100 = 104.0
    assert call["trigger_price"] == pytest.approx(104.0)
    # SL price recovered from existing order
    assert pos.sl_price == 97.0
    assert pos.sl_order_id == "sl-existing"
    # TP set from new placement
    assert pos.tp_price == pytest.approx(104.0)
    assert "SOL" in stub._tp_sl_confirmed


@pytest.mark.asyncio
async def test_zero_orders_places_both_fresh():
    """Regression guard: zero existing reduce-only orders → both TP and SL
    are placed fresh, as before the fix."""
    pos = _make_position(symbol="FET", entry=100.0)
    stub = _make_stub(open_orders=[], stop_loss_pct=2.0, take_profit_pct=4.0)

    await _run(stub, pos)

    assert len(stub._place_trigger_calls) == 2, (
        f"Expected 2 trigger orders (fresh TP+SL), got {len(stub._place_trigger_calls)}"
    )
    sides = sorted(c["tpsl"] for c in stub._place_trigger_calls)
    assert sides == ["sl", "tp"]
    assert pos.tp_price == pytest.approx(104.0)
    assert pos.sl_price == pytest.approx(98.0)
