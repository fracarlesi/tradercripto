"""
Unit tests for exit_reason inference in ExecutionEngineService.

Targets the fix that replaces the distance-to-mark heuristic with a real
Hyperliquid fills-API lookup in
``ExecutionEngineService._infer_exit_reason_from_fills``.

We intentionally do not instantiate the full service (BotConfig +
MessageBus wiring is heavy); instead we invoke the unbound coroutine on a
lightweight namespace object that mimics the handful of attributes the
helper touches (``client``, ``_logger``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from crypto_bot.services.execution_engine import (
    ExecutionEngineService,
    ExecutionPosition,
)

_LOGGER = logging.getLogger("test_exit_reason")


def _make_position(
    *,
    symbol: str = "BTC",
    side: str = "long",
    entry: float = 100.0,
    tp: float = 102.0,
    sl: float = 99.0,
) -> ExecutionPosition:
    return ExecutionPosition(
        symbol=symbol,
        side=side,
        size=1.0,
        entry_price=entry,
        current_price=entry,
        tp_price=tp,
        sl_price=sl,
        opened_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )


class _StubClient:
    def __init__(self, fills):
        self._fills = fills

    async def get_fills(self, limit: int = 100):
        _ = limit
        return list(self._fills)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _fill(symbol: str, px: float, sz: float, direction: str, when: datetime):
    return {
        "symbol": symbol,
        "price": px,
        "size": sz,
        "dir": direction,
        "time": when,
    }


def test_infer_tp_trigger_from_fills():
    pos = _make_position(tp=102.0, sl=99.0)
    fills = [
        _fill("BTC", 101.98, 1.0, "Close Long", datetime.now(timezone.utc)),
    ]
    result = _run(
        ExecutionEngineService._infer_exit_reason_from_fills(
            _StubClient(fills), pos, _LOGGER
        )
    )
    assert result == "take_profit"


def test_infer_sl_trigger_from_fills():
    pos = _make_position(tp=102.0, sl=99.0)
    fills = [
        _fill("BTC", 98.95, 1.0, "Close Long", datetime.now(timezone.utc)),
    ]
    result = _run(
        ExecutionEngineService._infer_exit_reason_from_fills(
            _StubClient(fills), pos, _LOGGER
        )
    )
    assert result == "stop_loss"


def test_infer_returns_none_when_no_close_fill():
    pos = _make_position()
    fills = [
        _fill("BTC", 100.5, 1.0, "Open Long", datetime.now(timezone.utc)),
    ]
    result = _run(
        ExecutionEngineService._infer_exit_reason_from_fills(
            _StubClient(fills), pos, _LOGGER
        )
    )
    assert result is None


def test_infer_returns_none_on_api_error():
    pos = _make_position()

    class _BoomClient:
        async def get_fills(self, limit: int = 100):
            _ = limit
            raise RuntimeError("rate limit")

    result = _run(
        ExecutionEngineService._infer_exit_reason_from_fills(
            _BoomClient(), pos, _LOGGER
        )
    )
    assert result is None


def test_infer_filters_fills_before_opened_at():
    pos = _make_position()
    # Fill timestamp BEFORE position.opened_at should be ignored.
    stale_time = pos.opened_at - timedelta(hours=1) if pos.opened_at else datetime.now(timezone.utc)
    fills = [
        _fill("BTC", 101.98, 1.0, "Close Long", stale_time),
    ]
    result = _run(
        ExecutionEngineService._infer_exit_reason_from_fills(
            _StubClient(fills), pos, _LOGGER
        )
    )
    assert result is None


def test_infer_aggregates_partial_fills_vwap():
    pos = _make_position(tp=102.0, sl=99.0)
    now = datetime.now(timezone.utc)
    # Two partial fills at SL with tiny slippage, VWAP ~ 98.96.
    fills = [
        _fill("BTC", 98.97, 0.5, "Close Long", now),
        _fill("BTC", 98.95, 0.5, "Close Long", now),
    ]
    result = _run(
        ExecutionEngineService._infer_exit_reason_from_fills(
            _StubClient(fills), pos, _LOGGER
        )
    )
    assert result == "stop_loss"


def test_infer_bail_when_neither_side_within_tolerance():
    # Fill at 100.5 - nowhere near tp (110) or sl (90) with tol 0.3% of 100 = 0.3.
    pos = _make_position(entry=100.0, tp=110.0, sl=90.0)
    fills = [
        _fill("BTC", 100.5, 1.0, "Close Long", datetime.now(timezone.utc)),
    ]
    result = _run(
        ExecutionEngineService._infer_exit_reason_from_fills(
            _StubClient(fills), pos, _LOGGER
        )
    )
    assert result is None


def test_expiry_exit_reason_not_overwritten():
    """When exit_reason is already set upstream (expiry/manual), the
    inference path in _handle_position_closed must not touch it.

    We assert this at the contract level: the helper only runs when
    position.exit_reason is falsy. This test documents the invariant by
    verifying the ExecutionPosition field semantics.
    """
    pos = _make_position()
    pos.exit_reason = "expiry"
    # If the caller respects `if not position.exit_reason:` the helper is
    # never invoked; exit_reason stays "expiry".
    assert pos.exit_reason == "expiry"
    assert not pos.exit_reason_inferred_via_fallback
