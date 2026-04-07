# pyright: reportArgumentType=false
import pytest
pytest.skip(
    "STAGE A: min_hold race tests removed — _evaluate_positions_with_llm "
    "is now a no-op (predict-and-place execution).",
    allow_module_level=True,
)
"""
Tests for the FLAG-Trader EXIT eval min_hold race-condition fix.
================================================================

Covers:
1. fail-safe when opened_at is None (race window after fill)
2. fallback to execution_engine.active_positions[symbol].opened_at
3. hard 60s age floor blocks 2-second closes
4. normal young position still blocked by min_hold
5. old position passes and is evaluated normally

Run:
    pytest crypto_bot/tests/test_min_hold_race.py -v
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from crypto_bot.main import ConservativeBot


# =============================================================================
# Helpers
# =============================================================================

def _make_bot_stub(
    *,
    opened_at_rm,
    opened_at_exec=None,
    min_hold_minutes=120,
    should_close=True,
    exit_reason="model_reversal",
    pnl_pct_entry_mark=(2000.0, 2000.0),
):
    """Build a minimal stub that mimics ConservativeBot's attributes used by
    _evaluate_positions_with_llm. Returns (bot_stub, risk_manager, exec_engine)."""
    entry_px, mark_px = pnl_pct_entry_mark

    pos = {
        "entry_price": entry_px,
        "mark_price": mark_px,
        "side": "long",
        "opened_at": opened_at_rm,
    }
    risk_manager = SimpleNamespace(_open_positions={"ETH": pos})

    exec_pos = SimpleNamespace(
        opened_at=opened_at_exec,
        entry_reason="",
        entry_confidence=0.0,
        entry_trigger_details="",
        current_r_multiple=0.0,
        peak_r_multiple=0.0,
        one_r_pct=0.0,
        breakeven_activated=False,
        exit_reason=None,
    )
    exec_engine = MagicMock()
    exec_engine.active_positions = {"ETH": exec_pos}
    exec_engine.close_position = AsyncMock()

    flag_agent = MagicMock()
    flag_agent.evaluate_position = AsyncMock(
        return_value=SimpleNamespace(
            should_close=should_close,
            confidence=0.9,
            reason=exit_reason,
        ),
    )

    config = SimpleNamespace(
        flag_trader_config={
            "min_hold_minutes": min_hold_minutes,
            "min_net_profit_pct_to_close": 0.15,
        },
        violation_exit_enabled=False,
        violation_min_profit_pct=0.5,
    )

    bot = SimpleNamespace(
        _flag_agent=flag_agent,
        _exchange=MagicMock(),
        _services={"execution": exec_engine},
        config=config,
        _position_eval_interval=60.0,
        _check_violation_exit=AsyncMock(return_value={}),
    )
    return bot, risk_manager, exec_engine, flag_agent


# =============================================================================
# Tests
# =============================================================================

@pytest.mark.asyncio
async def test_min_hold_skipped_when_opened_at_none():
    """opened_at=None in both rm and exec should cause a skip (fail-safe)."""
    bot, rm, exec_engine, flag_agent = _make_bot_stub(
        opened_at_rm=None, opened_at_exec=None,
    )
    closed = await ConservativeBot._evaluate_positions_with_llm(  # type: ignore[arg-type]
        bot, ["ETH"], rm, {},
    )
    assert closed == 0
    flag_agent.evaluate_position.assert_not_called()
    exec_engine.close_position.assert_not_called()


@pytest.mark.asyncio
async def test_min_hold_uses_execution_fallback():
    """opened_at None in rm but fresh in exec -> fallback used and 60s floor blocks."""
    now = datetime.now(timezone.utc)
    bot, rm, exec_engine, flag_agent = _make_bot_stub(
        opened_at_rm=None,
        opened_at_exec=now - timedelta(seconds=5),
        min_hold_minutes=0,  # disable min_hold to isolate the fallback path
    )
    closed = await ConservativeBot._evaluate_positions_with_llm(  # type: ignore[arg-type]
        bot, ["ETH"], rm, {},
    )
    # Fallback used -> min_hold=0 passes -> but hard 60s floor blocks
    assert closed == 0
    exec_engine.close_position.assert_not_called()


@pytest.mark.asyncio
async def test_hard_age_floor_blocks_2sec_close():
    """2-second-old position with min_hold=0 should still be refused by the hard floor."""
    now = datetime.now(timezone.utc)
    bot, rm, exec_engine, flag_agent = _make_bot_stub(
        opened_at_rm=now - timedelta(seconds=2),
        min_hold_minutes=0,
        pnl_pct_entry_mark=(2000.0, 2010.0),  # +0.5%, above fee gate
    )
    closed = await ConservativeBot._evaluate_positions_with_llm(  # type: ignore[arg-type]
        bot, ["ETH"], rm, {},
    )
    assert closed == 0
    exec_engine.close_position.assert_not_called()


@pytest.mark.asyncio
async def test_min_hold_normal_case_still_works():
    """5-minute-old position with min_hold=120 should be skipped (regression)."""
    now = datetime.now(timezone.utc)
    bot, rm, exec_engine, flag_agent = _make_bot_stub(
        opened_at_rm=now - timedelta(minutes=5),
        min_hold_minutes=120,
    )
    closed = await ConservativeBot._evaluate_positions_with_llm(  # type: ignore[arg-type]
        bot, ["ETH"], rm, {},
    )
    assert closed == 0
    flag_agent.evaluate_position.assert_not_called()
    exec_engine.close_position.assert_not_called()


@pytest.mark.asyncio
async def test_min_hold_old_position_evaluated():
    """200-minute-old profitable position with min_hold=120 should close normally."""
    now = datetime.now(timezone.utc)
    bot, rm, exec_engine, flag_agent = _make_bot_stub(
        opened_at_rm=now - timedelta(minutes=200),
        min_hold_minutes=120,
        pnl_pct_entry_mark=(2000.0, 2020.0),  # +1% — above fee gate
        exit_reason="take_profit",
        should_close=True,
    )
    closed = await ConservativeBot._evaluate_positions_with_llm(  # type: ignore[arg-type]
        bot, ["ETH"], rm, {},
    )
    assert closed == 1
    flag_agent.evaluate_position.assert_called_once()
    exec_engine.close_position.assert_called_once_with("ETH")
