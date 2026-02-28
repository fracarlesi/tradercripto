"""Tests for ORB Strategy."""

import pytest
from decimal import Decimal
from datetime import datetime, timezone

from ib_bot.core.enums import Direction, SessionPhase, SetupType
from ib_bot.core.models import FuturesMarketState, ORBRange
from ib_bot.config.loader import StrategyConfig, StopsConfig
from ib_bot.strategies.orb import ORBStrategy


@pytest.fixture
def strategy(
    strategy_config: StrategyConfig, stops_config: StopsConfig
) -> ORBStrategy:
    return ORBStrategy(strategy_config, stops_config)


class TestLongBreakout:
    """Test long breakout detection."""

    def test_valid_long_breakout(
        self,
        strategy: ORBStrategy,
        sample_or_range: ORBRange,
    ) -> None:
        """Price above OR high + buffer, above VWAP → LONG setup."""
        state = FuturesMarketState(
            symbol="MES",
            last_price=Decimal("5020.75"),  # > 5020.00 + 0.50 buffer
            vwap=Decimal("5014.50"),
            atr_14=Decimal("3.50"),  # 14 ticks > 4 min
            volume=Decimal("50000"),
            session_phase=SessionPhase.ACTIVE_TRADING,
            timestamp=datetime(2026, 2, 28, 10, 0, tzinfo=timezone.utc),
        )
        result = strategy.evaluate(state, sample_or_range)
        assert result.has_setup
        assert result.setup is not None
        assert result.setup.direction == Direction.LONG
        assert result.setup.setup_type == SetupType.ORB_LONG

    def test_long_rejected_below_vwap(
        self,
        strategy: ORBStrategy,
        sample_or_range: ORBRange,
    ) -> None:
        """Price above OR but below VWAP → rejected."""
        state = FuturesMarketState(
            symbol="MES",
            last_price=Decimal("5020.75"),
            vwap=Decimal("5021.00"),  # VWAP above price
            atr_14=Decimal("3.50"),
            volume=Decimal("50000"),
            session_phase=SessionPhase.ACTIVE_TRADING,
            timestamp=datetime(2026, 2, 28, 10, 0, tzinfo=timezone.utc),
        )
        result = strategy.evaluate(state, sample_or_range)
        assert not result.has_setup
        assert "VWAP" in result.reason

    def test_long_stop_at_midpoint(
        self,
        strategy: ORBStrategy,
        sample_or_range: ORBRange,
    ) -> None:
        """Stop should be at OR midpoint - buffer."""
        state = FuturesMarketState(
            symbol="MES",
            last_price=Decimal("5020.75"),
            vwap=Decimal("5014.50"),
            atr_14=Decimal("3.50"),
            volume=Decimal("50000"),
            session_phase=SessionPhase.ACTIVE_TRADING,
            timestamp=datetime(2026, 2, 28, 10, 0, tzinfo=timezone.utc),
        )
        result = strategy.evaluate(state, sample_or_range)
        assert result.setup is not None
        # Stop = midpoint(5015.00) - buffer(2 * 0.25 = 0.50) = 5014.50
        assert result.setup.stop_price == Decimal("5014.50")


class TestShortBreakout:
    """Test short breakout detection."""

    def test_valid_short_breakout(
        self,
        strategy: ORBStrategy,
        sample_or_range: ORBRange,
    ) -> None:
        """Price below OR low - buffer, below VWAP → SHORT setup."""
        state = FuturesMarketState(
            symbol="MES",
            last_price=Decimal("5009.25"),  # < 5010.00 - 0.50 buffer
            vwap=Decimal("5014.50"),
            atr_14=Decimal("3.50"),
            volume=Decimal("50000"),
            session_phase=SessionPhase.ACTIVE_TRADING,
            timestamp=datetime(2026, 2, 28, 10, 0, tzinfo=timezone.utc),
        )
        result = strategy.evaluate(state, sample_or_range)
        assert result.has_setup
        assert result.setup is not None
        assert result.setup.direction == Direction.SHORT
        assert result.setup.setup_type == SetupType.ORB_SHORT

    def test_short_disabled(
        self,
        stops_config: StopsConfig,
        sample_or_range: ORBRange,
    ) -> None:
        """Shorts disabled → no short setup."""
        config = StrategyConfig(allow_short=False)
        strategy = ORBStrategy(config, stops_config)
        state = FuturesMarketState(
            symbol="MES",
            last_price=Decimal("5009.25"),
            vwap=Decimal("5014.50"),
            atr_14=Decimal("3.50"),
            volume=Decimal("50000"),
            session_phase=SessionPhase.ACTIVE_TRADING,
            timestamp=datetime(2026, 2, 28, 10, 0, tzinfo=timezone.utc),
        )
        result = strategy.evaluate(state, sample_or_range)
        assert not result.has_setup


class TestFilters:
    """Test strategy filters."""

    def test_wrong_session_phase(
        self,
        strategy: ORBStrategy,
        sample_or_range: ORBRange,
    ) -> None:
        """Not in ACTIVE_TRADING → rejected."""
        state = FuturesMarketState(
            symbol="MES",
            last_price=Decimal("5020.75"),
            vwap=Decimal("5014.50"),
            atr_14=Decimal("3.50"),
            volume=Decimal("50000"),
            session_phase=SessionPhase.OPENING_RANGE,
            timestamp=datetime(2026, 2, 28, 14, 35, tzinfo=timezone.utc),
        )
        result = strategy.evaluate(state, sample_or_range)
        assert not result.has_setup
        assert "phase" in result.reason.lower()

    def test_invalid_or_range(
        self,
        strategy: ORBStrategy,
    ) -> None:
        """Invalid OR range → rejected."""
        or_range = ORBRange(
            symbol="MES",
            or_high=Decimal("5020.00"),
            or_low=Decimal("5019.00"),
            midpoint=Decimal("5019.50"),
            range_ticks=4,  # Below min_range_ticks
            volume=Decimal("1000"),
            vwap=Decimal("5019.50"),
            timestamp=datetime(2026, 2, 28, 14, 45, tzinfo=timezone.utc),
            valid=False,
        )
        state = FuturesMarketState(
            symbol="MES",
            last_price=Decimal("5020.75"),
            vwap=Decimal("5014.50"),
            atr_14=Decimal("3.50"),
            volume=Decimal("50000"),
            session_phase=SessionPhase.ACTIVE_TRADING,
            timestamp=datetime(2026, 2, 28, 10, 0, tzinfo=timezone.utc),
        )
        result = strategy.evaluate(state, or_range)
        assert not result.has_setup

    def test_atr_too_low(
        self,
        strategy: ORBStrategy,
        sample_or_range: ORBRange,
    ) -> None:
        """ATR below minimum → rejected."""
        state = FuturesMarketState(
            symbol="MES",
            last_price=Decimal("5020.75"),
            vwap=Decimal("5014.50"),
            atr_14=Decimal("0.50"),  # Only 2 ticks < 4 min
            volume=Decimal("50000"),
            session_phase=SessionPhase.ACTIVE_TRADING,
            timestamp=datetime(2026, 2, 28, 10, 0, tzinfo=timezone.utc),
        )
        result = strategy.evaluate(state, sample_or_range)
        assert not result.has_setup
        assert "ATR" in result.reason

    def test_no_breakout_in_range(
        self,
        strategy: ORBStrategy,
        sample_or_range: ORBRange,
    ) -> None:
        """Price within OR range → no setup."""
        state = FuturesMarketState(
            symbol="MES",
            last_price=Decimal("5015.00"),  # Inside range
            vwap=Decimal("5014.50"),
            atr_14=Decimal("3.50"),
            volume=Decimal("50000"),
            session_phase=SessionPhase.ACTIVE_TRADING,
            timestamp=datetime(2026, 2, 28, 10, 0, tzinfo=timezone.utc),
        )
        result = strategy.evaluate(state, sample_or_range)
        assert not result.has_setup


class TestRiskReward:
    """Test R:R calculation."""

    def test_rr_ratio(
        self,
        strategy: ORBStrategy,
        sample_or_range: ORBRange,
    ) -> None:
        """Target should be 1.5x risk from entry."""
        state = FuturesMarketState(
            symbol="MES",
            last_price=Decimal("5020.75"),
            vwap=Decimal("5014.50"),
            atr_14=Decimal("3.50"),
            volume=Decimal("50000"),
            session_phase=SessionPhase.ACTIVE_TRADING,
            timestamp=datetime(2026, 2, 28, 10, 0, tzinfo=timezone.utc),
        )
        result = strategy.evaluate(state, sample_or_range)
        assert result.setup is not None
        risk = result.setup.entry_price - result.setup.stop_price
        reward = result.setup.target_price - result.setup.entry_price
        rr = reward / risk
        assert abs(float(rr) - 1.5) < 0.01
