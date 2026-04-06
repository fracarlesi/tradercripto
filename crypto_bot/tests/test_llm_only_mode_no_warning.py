# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportGeneralTypeIssues=false
"""
Tests for the LLM-only exit mode warning-spam fix in ExecutionEngineService.
============================================================================

When the bot runs in "LLM-only exit mode" (``risk.stop_loss_pct == 0`` and
``risk.take_profit_pct == 0``), the FLAG-Trader LLM is the sole exit authority
and no protective TP/SL orders are placed on the exchange by design. Before
this fix, ``_sync_positions_from_exchange`` would nonetheless log a misleading
warning every 5s complaining that the position was "missing TP/SL protection"
and would call ``_ensure_tp_sl_for_position`` (a no-op in that mode).

On 2026-04-06 19:14 that log spam caused a user-initiated panic close of an
FET position for ~-$0.50 of avoidable loss. This test suite pins the fix:

1. No warning fires in LLM-only mode.
2. Warning STILL fires when TP/SL is enforced (regression guard).
3. ``_ensure_tp_sl_for_position`` is not invoked in LLM-only mode.

Run:
    pytest crypto_bot/tests/test_llm_only_mode_no_warning.py -v
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from crypto_bot.services.execution_engine import ExecutionEngineService, PositionStatus


# =============================================================================
# Helpers
# =============================================================================


def _make_exec_stub(
    *,
    stop_loss_pct: float,
    take_profit_pct: float,
    local_tp: float | None,
    local_sl: float | None,
    symbol: str = "FET",
):
    """Build a minimal stub mimicking ``ExecutionEngineService`` for the sync
    loop code path. Only the fields touched by ``_sync_positions_from_exchange``
    while processing an already-known position are populated."""
    entry_price = 0.50
    local_pos = SimpleNamespace(
        symbol=symbol,
        side="long",
        size=100.0,
        entry_price=entry_price,
        current_price=entry_price,
        unrealized_pnl=0.0,
        status=PositionStatus.OPEN,
        tp_price=local_tp,
        sl_price=local_sl,
        tp_order_id=None,
        sl_order_id=None,
        opened_at=datetime.now(timezone.utc),
    )

    client = MagicMock()
    client.get_positions = AsyncMock(
        return_value=[
            {
                "symbol": symbol,
                "size": 100.0,
                "entryPrice": entry_price,
                "markPrice": entry_price,
                "unrealizedPnl": 0.0,
                "leverage": 5,
                "side": "long",
            }
        ]
    )

    bot_config = SimpleNamespace(
        risk=SimpleNamespace(
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
        )
    )

    stub = SimpleNamespace(
        client=client,
        _bot_config=bot_config,
        active_positions={symbol: local_pos},
        _tp_sl_confirmed=set(),
        _tp_sl_placed_size={},
        _settling_symbols=set(),
        _logger=logging.getLogger("test_llm_only_mode"),
        _ensure_tp_sl_for_position=AsyncMock(),
        _update_tp_sl_for_size_change=AsyncMock(),
        _handle_position_closed=AsyncMock(),
        _get_position_open_time=AsyncMock(return_value=datetime.now(timezone.utc)),
    )
    return stub


async def _run_sync(stub) -> None:
    """Invoke the real ``_sync_positions_from_exchange`` bound to the stub."""
    await ExecutionEngineService._sync_positions_from_exchange(stub)  # type: ignore[arg-type]


# =============================================================================
# Tests
# =============================================================================


@pytest.mark.asyncio
async def test_no_warning_when_llm_only_mode(caplog):
    """LLM-only mode (sl=0, tp=0): the misleading warning must NOT fire."""
    stub = _make_exec_stub(
        stop_loss_pct=0,
        take_profit_pct=0,
        local_tp=None,
        local_sl=None,
    )
    with caplog.at_level(logging.WARNING, logger="test_llm_only_mode"):
        await _run_sync(stub)

    assert not any(
        "missing TP/SL protection" in rec.getMessage() for rec in caplog.records
    ), "Warning should be silenced in LLM-only exit mode"


@pytest.mark.asyncio
async def test_warning_still_fires_when_tp_sl_enforced(caplog):
    """Regression guard: with SL%>0, the warning MUST still fire."""
    stub = _make_exec_stub(
        stop_loss_pct=0.02,
        take_profit_pct=0,
        local_tp=None,
        local_sl=None,
    )
    with caplog.at_level(logging.WARNING, logger="test_llm_only_mode"):
        await _run_sync(stub)

    assert any(
        "missing TP/SL protection" in rec.getMessage() for rec in caplog.records
    ), "Legitimate TP/SL warning must not be silenced when enforcement is on"
    stub._ensure_tp_sl_for_position.assert_awaited()


@pytest.mark.asyncio
async def test_ensure_tp_sl_not_called_in_llm_only_mode():
    """LLM-only mode: ``_ensure_tp_sl_for_position`` must not be called
    (it is a no-op in that mode; skipping saves CPU and prevents log spam)."""
    stub = _make_exec_stub(
        stop_loss_pct=0,
        take_profit_pct=0,
        local_tp=None,
        local_sl=None,
    )
    await _run_sync(stub)
    stub._ensure_tp_sl_for_position.assert_not_called()
