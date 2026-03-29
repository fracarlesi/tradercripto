"""
Tests for R-Based Exit System
==============================

Tests for the new R-multiple based exit mechanisms:
1. Breakeven protection at 2R
2. Strength exit (auto-close) at 3R
3. R-based trailing stop after breakeven

Run:
    pytest crypto_bot/tests/test_r_based_exits.py -v
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from crypto_bot.services.execution_engine import (
    ExecutionEngineService,
    ExecutionPosition,
    PositionStatus,
)


# =============================================================================
# Helpers
# =============================================================================


def _make_engine(
    bp_activation_r: float = 2.0,
    bp_offset_pct: float = 0.15,
    strength_exit_r: float = 3.0,
    trailing_r_enabled: bool = True,
    trailing_start_r: float = 2.0,
    trailing_step_r: float = 1.0,
    trailing_lock_r: float = 0.5,
    r_based_enabled: bool = True,
) -> ExecutionEngineService:
    """Create an ExecutionEngineService with mocked dependencies for R-based tests."""
    engine = ExecutionEngineService.__new__(ExecutionEngineService)
    engine._logger = MagicMock()
    engine.active_positions = {}
    engine._settling_symbols = set()
    engine._closing_positions = set()
    engine.client = AsyncMock()
    engine.client.cancel_order = AsyncMock()
    engine._place_trigger_with_retry = AsyncMock(return_value={"orderId": "new_sl_r"})

    # Mock config with R-based exit settings
    engine._bot_config = MagicMock()
    engine._bot_config.risk.stop_loss_pct = 0
    engine._bot_config.risk.take_profit_pct = 0
    engine._bot_config.stops.r_based_exits_enabled = r_based_enabled
    engine._bot_config.stops.bp_activation_r = bp_activation_r
    engine._bot_config.stops.bp_offset_pct = bp_offset_pct
    engine._bot_config.stops.strength_exit_r = strength_exit_r
    engine._bot_config.stops.trailing_r_enabled = trailing_r_enabled
    engine._bot_config.stops.trailing_start_r = trailing_start_r
    engine._bot_config.stops.trailing_step_r = trailing_step_r
    engine._bot_config.stops.trailing_lock_r = trailing_lock_r

    # Mock close_position for strength exit
    engine.close_position = AsyncMock()

    return engine


def _make_position(
    symbol: str = "BTC",
    side: str = "long",
    entry_price: float = 100_000.0,
    current_price: float = 100_000.0,
    one_r_pct: float = 1.0,  # 1R = 1% of entry
    sl_order_id: str | None = "999",
    sl_price: float | None = 99_000.0,
    breakeven_activated: bool = False,
    peak_r_multiple: float = 0.0,
    last_trail_r: float = 0.0,
) -> ExecutionPosition:
    """Create a minimal ExecutionPosition for R-based testing."""
    one_r_price = entry_price * (one_r_pct / 100)
    return ExecutionPosition(
        symbol=symbol,
        side=side,
        size=0.01,
        entry_price=entry_price,
        current_price=current_price,
        status=PositionStatus.OPEN,
        opened_at=datetime.now(timezone.utc),
        sl_order_id=sl_order_id,
        sl_price=sl_price,
        breakeven_activated=breakeven_activated,
        one_r_pct=one_r_pct,
        one_r_price=one_r_price,
        peak_r_multiple=peak_r_multiple,
        last_trail_r=last_trail_r,
        highest_price=current_price,
        lowest_price=current_price,
    )


# =============================================================================
# R-Multiple Calculation
# =============================================================================


class TestUpdateRMultiples:
    """Tests for _update_r_multiples helper."""

    def test_long_in_profit(self) -> None:
        """Long at +2R should show current_r_multiple=2.0."""
        engine = _make_engine()
        # 1R = 1% of 100k = 1000. At +2000 (102k), R = 2.0
        pos = _make_position(entry_price=100_000, current_price=102_000, one_r_pct=1.0)
        engine.active_positions["BTC"] = pos

        engine._update_r_multiples()

        assert pos.current_r_multiple == 2.0
        assert pos.peak_r_multiple == 2.0

    def test_long_at_loss(self) -> None:
        """Long at -1R should show current_r_multiple=-1.0."""
        engine = _make_engine()
        pos = _make_position(entry_price=100_000, current_price=99_000, one_r_pct=1.0)
        engine.active_positions["BTC"] = pos

        engine._update_r_multiples()

        assert pos.current_r_multiple == -1.0
        assert pos.peak_r_multiple == 0.0  # Peak stays at 0 (never positive)

    def test_short_in_profit(self) -> None:
        """Short at +1.5R should show current_r_multiple=1.5."""
        engine = _make_engine()
        # Short: profit when price drops. 1R = 1% = 1000. At 98_500 => +1500 => 1.5R
        pos = _make_position(
            entry_price=100_000, current_price=98_500,
            side="short", one_r_pct=1.0,
        )
        engine.active_positions["BTC"] = pos

        engine._update_r_multiples()

        assert pos.current_r_multiple == 1.5
        assert pos.peak_r_multiple == 1.5

    def test_peak_r_preserves_maximum(self) -> None:
        """Peak R should not decrease when current R drops."""
        engine = _make_engine()
        pos = _make_position(
            entry_price=100_000, current_price=101_000,
            one_r_pct=1.0, peak_r_multiple=3.0,  # Was at 3R before
        )
        engine.active_positions["BTC"] = pos

        engine._update_r_multiples()

        assert pos.current_r_multiple == 1.0  # Currently at 1R
        assert pos.peak_r_multiple == 3.0     # Peak preserved

    def test_zero_one_r_skips(self) -> None:
        """Positions with no 1R defined are skipped."""
        engine = _make_engine()
        pos = _make_position(one_r_pct=0.0)
        pos.one_r_price = 0.0
        engine.active_positions["BTC"] = pos

        engine._update_r_multiples()

        assert pos.current_r_multiple == 0.0


# =============================================================================
# R-Based Breakeven
# =============================================================================


class TestRBasedBreakeven:
    """Tests for R-based breakeven protection."""

    @pytest.mark.asyncio
    async def test_activates_at_2r_long(self) -> None:
        """Breakeven triggers at +2R for long position."""
        engine = _make_engine(bp_activation_r=2.0, bp_offset_pct=0.15)
        # 1R = 1% = 1000. At 102_000 => +2R
        pos = _make_position(
            entry_price=100_000, current_price=102_000,
            one_r_pct=1.0, side="long",
        )
        pos.current_r_multiple = 2.0  # Pre-calculated
        engine.active_positions["BTC"] = pos

        await engine._check_r_based_breakeven()

        # Old SL cancelled
        engine.client.cancel_order.assert_called_once_with("BTC", 999)
        # New SL at entry + offset (0.15%)
        expected_sl = 100_000.0 * (1 + 0.15 / 100)
        engine._place_trigger_with_retry.assert_called_once_with(
            symbol="BTC",
            is_buy=False,
            size=pos.size,
            trigger_price=expected_sl,
            tpsl="sl",
        )
        assert pos.breakeven_activated is True

    @pytest.mark.asyncio
    async def test_activates_at_2r_short(self) -> None:
        """Breakeven triggers at +2R for short position."""
        engine = _make_engine(bp_activation_r=2.0, bp_offset_pct=0.15)
        # Short: 1R = 1% = 1000. At 98_000 => +2R
        pos = _make_position(
            entry_price=100_000, current_price=98_000,
            one_r_pct=1.0, side="short", sl_price=101_000,
        )
        pos.current_r_multiple = 2.0
        engine.active_positions["BTC"] = pos

        await engine._check_r_based_breakeven()

        expected_sl = 100_000.0 * (1 - 0.15 / 100)
        engine._place_trigger_with_retry.assert_called_once_with(
            symbol="BTC",
            is_buy=True,
            size=pos.size,
            trigger_price=expected_sl,
            tpsl="sl",
        )
        assert pos.breakeven_activated is True

    @pytest.mark.asyncio
    async def test_does_not_activate_below_2r(self) -> None:
        """No breakeven when profit is below 2R."""
        engine = _make_engine(bp_activation_r=2.0)
        pos = _make_position(
            entry_price=100_000, current_price=101_500,
            one_r_pct=1.0, side="long",
        )
        pos.current_r_multiple = 1.5
        engine.active_positions["BTC"] = pos

        await engine._check_r_based_breakeven()

        engine.client.cancel_order.assert_not_called()
        assert pos.breakeven_activated is False

    @pytest.mark.asyncio
    async def test_skips_if_already_activated(self) -> None:
        """No duplicate breakeven if already activated."""
        engine = _make_engine()
        pos = _make_position(breakeven_activated=True)
        pos.current_r_multiple = 3.0
        engine.active_positions["BTC"] = pos

        await engine._check_r_based_breakeven()

        engine.client.cancel_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_if_disabled(self) -> None:
        """No R-based breakeven when r_based_exits_enabled=False."""
        engine = _make_engine(r_based_enabled=False)
        pos = _make_position()
        pos.current_r_multiple = 5.0
        engine.active_positions["BTC"] = pos

        await engine._check_r_based_breakeven()

        engine.client.cancel_order.assert_not_called()


# =============================================================================
# Strength Exit
# =============================================================================


class TestStrengthExit:
    """Tests for automatic strength exit at high R-multiple."""

    @pytest.mark.asyncio
    async def test_closes_at_3r(self) -> None:
        """Position auto-closes at +3R."""
        engine = _make_engine(strength_exit_r=3.0)
        pos = _make_position(entry_price=100_000, current_price=103_000, one_r_pct=1.0)
        pos.current_r_multiple = 3.0
        engine.active_positions["BTC"] = pos

        await engine._check_strength_exit()

        engine.close_position.assert_called_once_with("BTC")
        assert pos.exit_reason == "strength_exit"

    @pytest.mark.asyncio
    async def test_does_not_close_below_3r(self) -> None:
        """No strength exit below 3R."""
        engine = _make_engine(strength_exit_r=3.0)
        pos = _make_position(entry_price=100_000, current_price=102_500, one_r_pct=1.0)
        pos.current_r_multiple = 2.5
        engine.active_positions["BTC"] = pos

        await engine._check_strength_exit()

        engine.close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_closing_positions(self) -> None:
        """Skip positions that are already being closed."""
        engine = _make_engine(strength_exit_r=3.0)
        engine._closing_positions.add("BTC")
        pos = _make_position()
        pos.current_r_multiple = 5.0
        engine.active_positions["BTC"] = pos

        await engine._check_strength_exit()

        engine.close_position.assert_not_called()


# =============================================================================
# R-Based Trailing Stop
# =============================================================================


class TestRBasedTrailing:
    """Tests for R-based trailing stop."""

    @pytest.mark.asyncio
    async def test_trails_at_3r_long(self) -> None:
        """At peak 3R, SL moves to entry + 0.5R (1 step above start)."""
        engine = _make_engine(
            trailing_start_r=2.0, trailing_step_r=1.0, trailing_lock_r=0.5,
        )
        # 1R = 1000. Peak at 3R = 3 steps? No: (3 - 2) / 1 = 1 step => lock 0.5R
        pos = _make_position(
            entry_price=100_000, current_price=103_000,
            one_r_pct=1.0, side="long",
            breakeven_activated=True, sl_price=100_150,  # Currently at BP
            peak_r_multiple=3.0, last_trail_r=2.0,
        )
        engine.active_positions["BTC"] = pos

        await engine._check_r_based_trailing()

        # New SL = entry + 0.5 * 1R = 100_000 + 500 = 100_500
        engine.client.cancel_order.assert_called_once()
        call_args = engine._place_trigger_with_retry.call_args
        assert call_args is not None
        expected_sl = 100_500.0
        assert call_args.kwargs["trigger_price"] == expected_sl

    @pytest.mark.asyncio
    async def test_trails_at_4r_long(self) -> None:
        """At peak 4R, SL moves to entry + 1.0R (2 steps above start)."""
        engine = _make_engine(
            trailing_start_r=2.0, trailing_step_r=1.0, trailing_lock_r=0.5,
        )
        pos = _make_position(
            entry_price=100_000, current_price=104_000,
            one_r_pct=1.0, side="long",
            breakeven_activated=True, sl_price=100_500,  # From previous trail
            peak_r_multiple=4.0, last_trail_r=2.5,
        )
        engine.active_positions["BTC"] = pos

        await engine._check_r_based_trailing()

        # 2 steps => lock 1.0R => SL = 100_000 + 1000 = 101_000
        call_args = engine._place_trigger_with_retry.call_args
        assert call_args is not None
        assert call_args.kwargs["trigger_price"] == 101_000.0

    @pytest.mark.asyncio
    async def test_no_trail_before_breakeven(self) -> None:
        """Trailing only activates after breakeven."""
        engine = _make_engine()
        pos = _make_position(
            entry_price=100_000, current_price=104_000,
            one_r_pct=1.0, side="long",
            breakeven_activated=False,
            peak_r_multiple=4.0,
        )
        engine.active_positions["BTC"] = pos

        await engine._check_r_based_trailing()

        engine.client.cancel_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_trail_before_full_step(self) -> None:
        """No trailing update when peak hasn't reached a full step."""
        engine = _make_engine(
            trailing_start_r=2.0, trailing_step_r=1.0,
        )
        pos = _make_position(
            entry_price=100_000, current_price=102_500,
            one_r_pct=1.0, side="long",
            breakeven_activated=True,
            peak_r_multiple=2.5, last_trail_r=2.0,
        )
        engine.active_positions["BTC"] = pos

        await engine._check_r_based_trailing()

        engine.client.cancel_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_short_trailing(self) -> None:
        """Short trailing: SL moves down as profit grows."""
        engine = _make_engine(
            trailing_start_r=2.0, trailing_step_r=1.0, trailing_lock_r=0.5,
        )
        # Short: entry 100k, 1R = 1% = 1000. Peak 3R = price at 97k
        pos = _make_position(
            entry_price=100_000, current_price=97_000,
            one_r_pct=1.0, side="short",
            breakeven_activated=True, sl_price=99_850,
            peak_r_multiple=3.0, last_trail_r=2.0,
        )
        engine.active_positions["BTC"] = pos

        await engine._check_r_based_trailing()

        # 1 step => lock 0.5R => SL = 100_000 - 500 = 99_500
        call_args = engine._place_trigger_with_retry.call_args
        assert call_args is not None
        assert call_args.kwargs["trigger_price"] == 99_500.0
        assert call_args.kwargs["is_buy"] is True  # Close short = buy


# =============================================================================
# ExecutionPosition R-Fields
# =============================================================================


class TestRFields:
    """Tests for R-related fields on ExecutionPosition."""

    def test_defaults(self) -> None:
        """R-fields default to zero."""
        pos = _make_position(one_r_pct=0.0)
        pos.one_r_price = 0.0
        assert pos.one_r_pct == 0.0
        assert pos.one_r_price == 0.0
        assert pos.current_r_multiple == 0.0
        assert pos.peak_r_multiple == 0.0
        assert pos.last_trail_r == 0.0

    def test_to_dict_includes_r_fields(self) -> None:
        """to_dict() serializes R-related fields."""
        pos = _make_position(one_r_pct=1.5)
        pos.current_r_multiple = 2.3
        pos.peak_r_multiple = 3.1
        d = pos.to_dict()
        assert d["one_r_pct"] == 1.5
        assert d["one_r_price"] == 100_000 * 1.5 / 100
        assert d["current_r_multiple"] == 2.3
        assert d["peak_r_multiple"] == 3.1
