# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportGeneralTypeIssues=false
"""
Tests for the LLM-only exit mode warning-spam fix in ExecutionEngineService.
============================================================================

When the bot runs in "LLM-only exit mode" (``risk.stop_loss_pct == 0`` and
``risk.take_profit_pct == 0``), the FLAG-Trader LLM is the sole exit authority
for positions whose brackets are both placed. Historically the sync loop
logged a misleading "missing TP/SL protection" warning every 5s and called
``_ensure_tp_sl_for_position`` as a no-op. On 2026-04-06 19:14 that log spam
caused a user-initiated panic close of an FET position.

After the 2026-04-14 Gap 3 fix, the sync loop MUST still reconcile positions
whose bracket is partially missing (e.g. SL placed but TP missing) even in
LLM-only mode, recovering predicted TP/SL from the sidecar/decisions.jsonl.
This test suite now pins the refined contract:

1. In LLM-only mode, with both bracket legs already present, no warning fires
   and ``_ensure_tp_sl_for_position`` is NOT called (quiet path).
2. In enforcement mode with a missing bracket leg, the legacy warning fires
   and the reconcile is attempted (regression guard).
3. In LLM-only mode with a missing bracket leg, a different warning fires
   (mentions "LLM-only mode") and the reconcile IS attempted with the
   recovered overrides (Gap 3 behavior).

Run:
    pytest crypto_bot/tests/test_llm_only_mode_no_warning.py -v
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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
        _recover_decision_for_synced_position=MagicMock(return_value=None),
    )
    return stub


async def _run_sync(stub) -> None:
    """Invoke the real ``_sync_positions_from_exchange`` bound to the stub.

    Patches the module-level ``list_open_sidecars`` so the Gap 3 reconcile
    path does not read the real on-disk sidecars directory during unit tests.
    """
    with patch(
        "crypto_bot.services.execution_engine.list_open_sidecars",
        return_value=[],
    ):
        await ExecutionEngineService._sync_positions_from_exchange(stub)  # type: ignore[arg-type]


# =============================================================================
# Tests
# =============================================================================


@pytest.mark.asyncio
async def test_no_warning_when_llm_only_mode_and_brackets_present(caplog):
    """LLM-only mode with both bracket legs already placed: no warning, no
    reconcile call. This is the quiet steady-state path that used to emit
    spurious "missing TP/SL protection" warnings."""
    stub = _make_exec_stub(
        stop_loss_pct=0,
        take_profit_pct=0,
        local_tp=0.52,
        local_sl=0.48,
    )
    with caplog.at_level(logging.WARNING, logger="test_llm_only_mode"):
        await _run_sync(stub)

    assert not any(
        "missing TP/SL protection" in rec.getMessage() for rec in caplog.records
    ), "Legacy warning should not fire when both bracket legs are present"
    assert not any(
        "missing TP/SL bracket in LLM-only" in rec.getMessage()
        for rec in caplog.records
    ), "LLM-only reconcile warning should not fire when both bracket legs are present"
    stub._ensure_tp_sl_for_position.assert_not_called()


@pytest.mark.asyncio
async def test_warning_still_fires_when_tp_sl_enforced(caplog):
    """Regression guard: with SL%>0 enforcement, the legacy
    "missing TP/SL protection" warning MUST still fire when a bracket leg is
    missing, and the reconcile call MUST be attempted."""
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
async def test_reconcile_fires_in_llm_only_mode_when_leg_missing(caplog):
    """Gap 3 fix: in LLM-only mode, if a bracket leg is missing we MUST still
    call ``_ensure_tp_sl_for_position`` with the recovered overrides (sidecar
    or decisions), so the missing side gets placed. Previously this was
    silently gated out and positions stayed SL-only forever."""
    stub = _make_exec_stub(
        stop_loss_pct=0,
        take_profit_pct=0,
        local_tp=None,           # TP missing — typical ONDO/XMR failure mode
        local_sl=0.48,            # SL present
    )
    with caplog.at_level(logging.WARNING, logger="test_llm_only_mode"):
        await _run_sync(stub)

    # Legacy warning must NOT fire (would be spammy log noise in LLM-only mode)
    assert not any(
        "missing TP/SL protection" in rec.getMessage() for rec in caplog.records
    ), "Legacy warning should not fire in LLM-only mode"
    # New, more informative warning MUST fire
    assert any(
        "missing TP/SL bracket in LLM-only" in rec.getMessage()
        for rec in caplog.records
    ), "LLM-only reconcile warning should fire when a bracket leg is missing"
    # Reconcile call MUST have been attempted
    stub._ensure_tp_sl_for_position.assert_awaited()
    # And it MUST have been called with overrides (safety-net SL=2.0% since
    # decisions + sidecar returned nothing; TP stays None so Gap 2 alerts).
    call = stub._ensure_tp_sl_for_position.await_args
    assert call.kwargs.get("sl_pct_override") == 2.0
    assert call.kwargs.get("tp_pct_override") is None


@pytest.mark.asyncio
async def test_reconcile_uses_decisions_overrides_when_sidecar_missing(caplog):
    """When the sidecar is absent but decisions.jsonl has a record, the
    reconcile call must receive the decision's predicted TP/SL as overrides
    (no safety-net SL fallback)."""
    stub = _make_exec_stub(
        stop_loss_pct=0,
        take_profit_pct=0,
        local_tp=None,
        local_sl=0.48,
    )
    stub._recover_decision_for_synced_position = MagicMock(
        return_value={
            "trade_id": "abc123",
            "predicted_tp_pct": 1.5,
            "predicted_sl_pct": 0.8,
        }
    )
    with caplog.at_level(logging.WARNING, logger="test_llm_only_mode"):
        await _run_sync(stub)

    stub._ensure_tp_sl_for_position.assert_awaited()
    call = stub._ensure_tp_sl_for_position.await_args
    # Recovered from decisions (preferred over the 2.0% safety-net)
    assert call.kwargs.get("tp_pct_override") == 1.5
    assert call.kwargs.get("sl_pct_override") == 0.8
