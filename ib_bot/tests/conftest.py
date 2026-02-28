"""Shared test fixtures for IB Bot."""

import pytest
from decimal import Decimal
from datetime import datetime, timezone

from ib_bot.config.loader import (
    RiskConfig,
    StrategyConfig,
    StopsConfig,
    OpeningRangeConfig,
)
from ib_bot.core.contracts import CONTRACTS
from ib_bot.core.enums import Direction, SessionPhase, SetupType
from ib_bot.core.models import FuturesMarketState, ORBRange, ORBSetup


@pytest.fixture
def risk_config() -> RiskConfig:
    return RiskConfig(
        max_risk_per_trade_usd=Decimal("500"),
        max_daily_loss_usd=Decimal("1000"),
        max_contracts_per_trade=2,
        max_trades_per_day=2,
        consecutive_stops_halt=2,
    )


@pytest.fixture
def strategy_config() -> StrategyConfig:
    return StrategyConfig(
        breakout_buffer_ticks=2,
        vwap_confirmation=True,
        min_atr_ticks=4,
        max_entry_time="11:30",
        allow_short=True,
        no_reentry_after_stop=True,
    )


@pytest.fixture
def stops_config() -> StopsConfig:
    return StopsConfig(
        stop_type="or_midpoint",
        stop_buffer_ticks=2,
        reward_risk_ratio=Decimal("1.5"),
        trailing_enabled=False,
        eod_flatten_time="15:45",
    )


@pytest.fixture
def or_config() -> OpeningRangeConfig:
    return OpeningRangeConfig(
        or_start="09:30",
        or_end="09:45",
        min_range_ticks=8,
        max_range_ticks=80,
    )


@pytest.fixture
def sample_or_range() -> ORBRange:
    """Sample valid OR range for MES (tick_size=0.25, tick_value=1.25)."""
    return ORBRange(
        symbol="MES",
        or_high=Decimal("5020.00"),
        or_low=Decimal("5010.00"),
        midpoint=Decimal("5015.00"),
        range_ticks=40,  # (5020-5010) / 0.25 = 40 ticks
        volume=Decimal("15000"),
        vwap=Decimal("5014.50"),
        timestamp=datetime(2026, 2, 28, 14, 45, tzinfo=timezone.utc),
        valid=True,
    )


@pytest.fixture
def sample_market_state() -> FuturesMarketState:
    """Sample market state during active trading."""
    return FuturesMarketState(
        symbol="MES",
        last_price=Decimal("5021.00"),
        vwap=Decimal("5014.50"),
        atr_14=Decimal("3.50"),
        volume=Decimal("50000"),
        session_phase=SessionPhase.ACTIVE_TRADING,
        timestamp=datetime(2026, 2, 28, 15, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def sample_setup(sample_or_range: ORBRange) -> ORBSetup:
    """Sample ORB long setup."""
    return ORBSetup(
        symbol="MES",
        direction=Direction.LONG,
        setup_type=SetupType.ORB_LONG,
        entry_price=Decimal("5020.50"),
        stop_price=Decimal("5014.50"),
        target_price=Decimal("5029.50"),
        risk_ticks=24,
        reward_ticks=36,
        or_range=sample_or_range,
        confidence=Decimal("0.7"),
    )
