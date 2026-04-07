# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportGeneralTypeIssues=false
"""
Tests for the exit_reason fallback-inference bug in LLM-only mode.
==================================================================

Bug A (2026-04-07): In ``ExecutionEngineService._handle_position_closed``
the final fallback block computed ``implied_tp`` and ``implied_sl`` from
``risk.take_profit_pct`` and ``risk.stop_loss_pct``. In LLM-only mode both
are 0, so ``implied_tp == implied_sl == entry_price`` and the ternary
``"take_profit" if tp_dist < sl_dist else "stop_loss"`` defaulted to
``stop_loss`` on the tie — mislabeling EVERY winning trade as a stop-loss
hit and poisoning trade_logger / RAG / capital_ladder / audit.

The fix:
    - LLM-only mode (tp_pct==0 and sl_pct==0, or both tp_price/sl_price None)
      → exit_reason = "external_close" (neutral, outcome from PnL sign).
    - Degenerate tie in non-LLM-only mode → use PnL sign.
    - Normal non-LLM-only TP hit → "take_profit" unchanged.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from crypto_bot.services.execution_engine import ExecutionEngineService, PositionStatus


def _make_stub(
    *,
    tp_pct: float,
    sl_pct: float,
    entry_price: float,
    exit_price: float,
    unrealized_pnl: float,
    tp_price: float | None = None,
    sl_price: float | None = None,
    side: str = "long",
    symbol: str = "FET",
):
    position = SimpleNamespace(
        symbol=symbol,
        side=side,
        size=100.0,
        entry_price=entry_price,
        current_price=exit_price,
        unrealized_pnl=unrealized_pnl,
        status=PositionStatus.OPEN,
        tp_price=tp_price,
        sl_price=sl_price,
        tp_order_id=None,
        sl_order_id=None,
        opened_at=datetime.now(timezone.utc),
        closed_at=None,
        exit_reason=None,
    )

    bot_config = SimpleNamespace(
        risk=SimpleNamespace(
            stop_loss_pct=sl_pct,
            take_profit_pct=tp_pct,
        )
    )

    client = MagicMock()
    client.cancel_order = AsyncMock()
    client.get_fills = AsyncMock(return_value=[])

    metrics = SimpleNamespace(positions_closed=0)

    stub = SimpleNamespace(
        client=client,
        _bot_config=bot_config,
        active_positions={symbol: position},
        _logger=logging.getLogger("test_exit_reason_llm_only"),
        metrics=metrics,
        # Downstream calls we don't care about — stub them all as async no-ops.
        _publish_trade_outcome=AsyncMock(),
        _record_trade_for_analytics=AsyncMock(),
        _log_trade=AsyncMock(),
        _apply_capital_ladder=AsyncMock(),
        _notify_position_closed=AsyncMock(),
        _persist_trade_outcome=AsyncMock(),
    )
    return stub, position


async def _run_handle_closed(stub, symbol: str = "FET") -> None:
    # We only care about the inference block — wrap in a try/except to swallow
    # post-inference bookkeeping errors from the simplified stub.
    try:
        await ExecutionEngineService._handle_position_closed(stub, symbol)  # type: ignore[arg-type]
    except Exception:
        pass


@pytest.mark.asyncio
async def test_llm_only_winning_close_is_external_close():
    """LLM-only mode, winning trade: must label external_close (not stop_loss)."""
    stub, pos = _make_stub(
        tp_pct=0,
        sl_pct=0,
        entry_price=0.50,
        exit_price=0.55,  # +10% winner
        unrealized_pnl=5.0,
    )
    await _run_handle_closed(stub)
    assert pos.exit_reason == "external_close", (
        f"Winning LLM-only close mislabeled as {pos.exit_reason!r} "
        "(Bug A regression)"
    )


@pytest.mark.asyncio
async def test_llm_only_losing_close_is_external_close():
    """LLM-only mode, losing trade: also external_close (PnL sign is metadata)."""
    stub, pos = _make_stub(
        tp_pct=0,
        sl_pct=0,
        entry_price=0.50,
        exit_price=0.48,
        unrealized_pnl=-2.0,
    )
    await _run_handle_closed(stub)
    assert pos.exit_reason == "external_close"


@pytest.mark.asyncio
async def test_non_llm_only_tp_hit_still_take_profit():
    """Regression guard: normal mode with clear TP hit must still be take_profit."""
    stub, pos = _make_stub(
        tp_pct=2.5,   # 2.5% TP
        sl_pct=1.0,   # 1.0% SL
        entry_price=100.0,
        exit_price=102.5,  # exactly at implied TP
        unrealized_pnl=2.5,
    )
    await _run_handle_closed(stub)
    assert pos.exit_reason == "take_profit"
