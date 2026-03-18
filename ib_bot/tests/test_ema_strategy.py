"""Tests for EMA Momentum live strategy."""

import pytest
from decimal import Decimal
from datetime import datetime, timezone

from ib_bot.config.loader import EMAStrategyConfig, StopsConfig
from ib_bot.core.enums import Direction, SessionPhase, SetupType
from ib_bot.core.models import FuturesMarketState, ORBRange
from ib_bot.strategies.ema_momentum import EMAMomentumStrategy


@pytest.fixture
def ema_config() -> EMAStrategyConfig:
    return EMAStrategyConfig(
        ema_fast=9,
        ema_slow=21,
        rsi_period=14,
        max_trades_per_day=4,
    )


@pytest.fixture
def stops() -> StopsConfig:
    return StopsConfig()


@pytest.fixture
def strategy(ema_config: EMAStrategyConfig, stops: StopsConfig) -> EMAMomentumStrategy:
    return EMAMomentumStrategy(ema_config=ema_config, stops_config=stops)


@pytest.fixture
def dummy_or_range() -> ORBRange:
    return ORBRange(
        symbol="MES",
        or_high=Decimal("5020"),
        or_low=Decimal("5010"),
        midpoint=Decimal("5015"),
        range_ticks=40,
        volume=Decimal("10000"),
        vwap=Decimal("5015"),
        timestamp=datetime(2026, 3, 1, 14, 45, tzinfo=timezone.utc),
        valid=True,
    )


def test_name(strategy: EMAMomentumStrategy) -> None:
    assert strategy.name == "ema_momentum"


def test_reset_daily(strategy: EMAMomentumStrategy) -> None:
    """reset_daily clears indicator state and trade count."""
    strategy._daily_trade_count = 3
    strategy._indicators["MES"] = object()  # type: ignore
    strategy.reset_daily()
    assert strategy._daily_trade_count == 0
    assert len(strategy._indicators) == 0


def test_reject_wrong_phase(
    strategy: EMAMomentumStrategy,
    dummy_or_range: ORBRange,
) -> None:
    """Rejects signals during wrong session phase."""
    state = FuturesMarketState(
        symbol="MES",
        last_price=Decimal("5020"),
        vwap=Decimal("5015"),
        atr_14=Decimal("3.5"),
        volume=Decimal("50000"),
        session_phase=SessionPhase.PRE_MARKET,
        timestamp=datetime(2026, 3, 1, 14, 0, tzinfo=timezone.utc),
    )
    result = strategy.evaluate(state, dummy_or_range)
    assert not result.has_setup
    assert "Wrong phase" in result.reason


def test_indicators_warmup(
    strategy: EMAMomentumStrategy,
    dummy_or_range: ORBRange,
) -> None:
    """Returns reject while indicators are warming up."""
    state = FuturesMarketState(
        symbol="MES",
        last_price=Decimal("5020"),
        vwap=Decimal("5015"),
        atr_14=Decimal("3.5"),
        volume=Decimal("50000"),
        session_phase=SessionPhase.ACTIVE_TRADING,
        timestamp=datetime(2026, 3, 1, 15, 0, tzinfo=timezone.utc),
    )
    # First call should reject (not enough data)
    result = strategy.evaluate(state, dummy_or_range)
    assert not result.has_setup
    assert "warming up" in result.reason.lower() or "No EMA" in result.reason


def test_daily_trade_limit(
    strategy: EMAMomentumStrategy,
    dummy_or_range: ORBRange,
) -> None:
    """Rejects when daily trade limit reached."""
    strategy._daily_trade_count = 4  # max
    state = FuturesMarketState(
        symbol="MES",
        last_price=Decimal("5020"),
        vwap=Decimal("5015"),
        atr_14=Decimal("3.5"),
        volume=Decimal("50000"),
        session_phase=SessionPhase.ACTIVE_TRADING,
        timestamp=datetime(2026, 3, 1, 15, 0, tzinfo=timezone.utc),
    )
    result = strategy.evaluate(state, dummy_or_range)
    assert not result.has_setup
    assert "limit" in result.reason.lower()
