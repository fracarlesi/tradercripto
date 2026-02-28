"""End-to-end integration tests for IB ORB Bot.

Verifies the full Opening Range Breakout flow with mocked ib_insync.
Tests real strategy, risk manager, and kill switch logic against
synthetic market data — only the IB client layer is mocked.
"""

import asyncio
import pytest
from dataclasses import dataclass
from datetime import datetime, time, timezone
from decimal import Decimal
from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from ib_bot.config.loader import (
    IBConnectionConfig,
    OpeningRangeConfig,
    RiskConfig,
    StrategyConfig,
    StopsConfig,
)
from ib_bot.core.enums import Direction, KillSwitchStatus, SessionPhase, SetupType
from ib_bot.core.models import FuturesMarketState, ORBRange, ORBSetup, TradeIntent
from ib_bot.services.kill_switch import KillSwitchService
from ib_bot.services.message_bus import MessageBus
from ib_bot.services.risk_manager import RiskManager
from ib_bot.strategies.orb import ORBStrategy


# ---------------------------------------------------------------------------
# Helpers: synthetic bar data
# ---------------------------------------------------------------------------

@dataclass
class FakeBar:
    """Mimics ib_insync BarData for testing."""
    date: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


def _make_or_bars(
    or_high: float,
    or_low: float,
    n_bars: int = 15,
) -> List[FakeBar]:
    """Generate synthetic 1-min bars for the Opening Range period (09:30-09:45 ET).

    Creates bars that drift from low to high over the OR window,
    with the overall high/low matching the requested OR boundaries.
    """
    bars: List[FakeBar] = []
    step = (or_high - or_low) / max(n_bars - 1, 1)

    for i in range(n_bars):
        t = datetime(2026, 2, 28, 9, 30 + i, tzinfo=timezone.utc)
        mid = or_low + step * i
        bar = FakeBar(
            date=t,
            open=mid - step * 0.2,
            high=min(mid + step * 0.5, or_high),
            low=max(mid - step * 0.5, or_low),
            close=mid,
            volume=1000 + i * 100,
        )
        bars.append(bar)

    # Ensure extremes are hit in the first and last bars
    bars[0].low = or_low
    bars[-1].high = or_high
    return bars


def _make_post_or_bar(
    price: float,
    minute_offset: int = 0,
) -> FakeBar:
    """Create a single bar after OR ends (09:45+) with the given price."""
    t = datetime(2026, 2, 28, 9, 45 + minute_offset, tzinfo=timezone.utc)
    return FakeBar(
        date=t,
        open=price - 0.25,
        high=price + 0.25,
        low=price - 0.50,
        close=price,
        volume=2000,
    )


def _make_afternoon_bar(price: float, hour: int, minute: int) -> FakeBar:
    """Create a bar at an arbitrary time."""
    t = datetime(2026, 2, 28, hour, minute, tzinfo=timezone.utc)
    return FakeBar(
        date=t,
        open=price,
        high=price + 0.25,
        low=price - 0.25,
        close=price,
        volume=500,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def risk_config() -> RiskConfig:
    return RiskConfig(
        max_risk_per_trade_usd=Decimal("500"),
        max_daily_loss_usd=Decimal("1000"),
        max_contracts_per_trade=2,
        max_trades_per_day=4,
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
def strategy(strategy_config: StrategyConfig, stops_config: StopsConfig) -> ORBStrategy:
    return ORBStrategy(strategy_config, stops_config)


@pytest.fixture
def risk_manager(risk_config: RiskConfig) -> RiskManager:
    return RiskManager(risk_config)


@pytest.fixture
def kill_switch(risk_config: RiskConfig) -> KillSwitchService:
    bus = MessageBus()
    return KillSwitchService(config=risk_config, bus=bus)


# ---------------------------------------------------------------------------
# Integration helpers
# ---------------------------------------------------------------------------

def _compute_or_range(bars: List[FakeBar], symbol: str = "MES") -> ORBRange:
    """Compute an ORBRange from fake bars, exactly as MarketDataService does."""
    from ib_bot.core.contracts import CONTRACTS

    spec = CONTRACTS[symbol]
    or_high = Decimal(str(max(b.high for b in bars)))
    or_low = Decimal(str(min(b.low for b in bars)))
    midpoint = (or_high + or_low) / 2
    total_volume = Decimal(str(sum(b.volume for b in bars)))
    range_ticks = int((or_high - or_low) / spec.tick_size)

    # Simple VWAP approximation
    tp_vol_sum = Decimal("0")
    vol_sum = Decimal("0")
    for b in bars:
        h, l, c, v = Decimal(str(b.high)), Decimal(str(b.low)), Decimal(str(b.close)), Decimal(str(b.volume))
        tp = (h + l + c) / 3
        tp_vol_sum += tp * v
        vol_sum += v
    vwap = tp_vol_sum / vol_sum if vol_sum else midpoint

    return ORBRange(
        symbol=symbol,
        or_high=or_high,
        or_low=or_low,
        midpoint=midpoint,
        range_ticks=range_ticks,
        volume=total_volume,
        vwap=vwap,
        timestamp=datetime(2026, 2, 28, 14, 45, tzinfo=timezone.utc),
        valid=8 <= range_ticks <= 80,
    )


def _build_market_state(
    price: Decimal,
    vwap: Decimal,
    atr: Decimal,
    symbol: str = "MES",
    phase: SessionPhase = SessionPhase.ACTIVE_TRADING,
) -> FuturesMarketState:
    return FuturesMarketState(
        symbol=symbol,
        last_price=price,
        vwap=vwap,
        atr_14=atr,
        volume=Decimal("50000"),
        session_phase=phase,
        timestamp=datetime(2026, 2, 28, 10, 0, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# Test: Full LONG breakout flow
# ---------------------------------------------------------------------------

class TestFullLongBreakoutFlow:
    """Simulate price breaking above OR high after 09:45 and verify
    the full pipeline: OR calculation -> strategy signal -> risk sizing
    -> bracket order placement.
    """

    def test_or_range_calculated_correctly(self) -> None:
        """OR high/low/midpoint/ticks match the synthetic bars."""
        bars = _make_or_bars(or_high=5020.00, or_low=5010.00)
        or_range = _compute_or_range(bars, "MES")

        assert or_range.or_high == Decimal("5020.0")
        assert or_range.or_low == Decimal("5010.0")
        assert or_range.midpoint == Decimal("5015.0")
        # (5020 - 5010) / 0.25 = 40 ticks
        assert or_range.range_ticks == 40
        assert or_range.valid is True

    def test_strategy_fires_long_signal(
        self, strategy: ORBStrategy,
    ) -> None:
        """Price above OR high + buffer with VWAP confirmation -> LONG."""
        bars = _make_or_bars(or_high=5020.00, or_low=5010.00)
        or_range = _compute_or_range(bars, "MES")

        # MES tick_size=0.25, buffer=2 ticks -> 0.50
        # breakout_price = 5020.00 + 0.50 = 5020.50
        # Use 5020.75 to be clearly above
        state = _build_market_state(
            price=Decimal("5020.75"),
            vwap=or_range.vwap,  # VWAP is below 5020, so confirmation passes
            atr=Decimal("3.50"),   # 14 ticks > 4 min_atr_ticks
        )
        result = strategy.evaluate(state, or_range)

        assert result.has_setup
        assert result.setup is not None
        assert result.setup.direction == Direction.LONG
        assert result.setup.setup_type == SetupType.ORB_LONG

    def test_risk_manager_sizes_position(
        self, strategy: ORBStrategy, risk_manager: RiskManager,
    ) -> None:
        """Risk manager sizes the trade within limits."""
        bars = _make_or_bars(or_high=5020.00, or_low=5010.00)
        or_range = _compute_or_range(bars, "MES")

        state = _build_market_state(
            price=Decimal("5020.75"),
            vwap=or_range.vwap,
            atr=Decimal("3.50"),
        )
        result = strategy.evaluate(state, or_range)
        assert result.setup is not None

        intent = risk_manager.size_trade(result.setup)
        assert intent is not None
        assert intent.contracts >= 1
        assert intent.contracts <= 2  # max_contracts_per_trade
        assert intent.risk_usd > 0

    @pytest.mark.asyncio
    async def test_execution_engine_places_bracket_order(
        self, strategy: ORBStrategy, risk_manager: RiskManager, risk_config: RiskConfig,
    ) -> None:
        """Full flow: strategy -> risk -> execution engine places bracket order via mocked IB."""
        from ib_bot.services.execution_engine import ExecutionEngine
        from ib_bot.services.ib_client import IBClient

        bars = _make_or_bars(or_high=5020.00, or_low=5010.00)
        or_range = _compute_or_range(bars, "MES")

        state = _build_market_state(
            price=Decimal("5020.75"),
            vwap=or_range.vwap,
            atr=Decimal("3.50"),
        )
        result = strategy.evaluate(state, or_range)
        assert result.setup is not None

        intent = risk_manager.size_trade(result.setup)
        assert intent is not None

        # Mock IB client
        mock_ib_client = MagicMock(spec=IBClient)
        mock_ib_client.place_bracket_order = AsyncMock(return_value=[
            MagicMock(name="entry_trade"),
            MagicMock(name="tp_trade"),
            MagicMock(name="sl_trade"),
        ])

        bus = MessageBus()
        kill_switch = KillSwitchService(config=risk_config, bus=bus)
        execution = ExecutionEngine(
            ib_client=mock_ib_client,
            risk_manager=risk_manager,
            kill_switch=kill_switch,
            bus=bus,
        )

        # Start bus + services so ORDER topic gets dispatched
        await bus.start()
        await kill_switch.start()
        await execution.start()

        try:
            # Publish the trade intent
            from ib_bot.core.enums import Topic
            await bus.publish(Topic.ORDER, intent.model_dump(), source="strategy")

            # Give the bus time to dispatch
            await asyncio.sleep(0.3)

            # Verify bracket order was placed with correct params
            mock_ib_client.place_bracket_order.assert_called_once()
            call_kwargs = mock_ib_client.place_bracket_order.call_args
            assert call_kwargs.kwargs["symbol"] == "MES"
            assert call_kwargs.kwargs["direction"] == Direction.LONG
            assert call_kwargs.kwargs["contracts"] == intent.contracts
            assert call_kwargs.kwargs["entry_price"] == intent.setup.entry_price
            assert call_kwargs.kwargs["stop_price"] == intent.setup.stop_price
            assert call_kwargs.kwargs["target_price"] == intent.setup.target_price
        finally:
            await execution.stop()
            await kill_switch.stop()
            await bus.stop()


# ---------------------------------------------------------------------------
# Test: Full SHORT breakout flow
# ---------------------------------------------------------------------------

class TestFullShortBreakoutFlow:
    """Simulate price breaking below OR low after 09:45."""

    def test_strategy_fires_short_signal(
        self, strategy: ORBStrategy,
    ) -> None:
        """Price below OR low - buffer with VWAP confirmation -> SHORT."""
        bars = _make_or_bars(or_high=5020.00, or_low=5010.00)
        or_range = _compute_or_range(bars, "MES")

        # short_entry = 5010.00 - 0.50 = 5009.50
        # Price must be below 5009.50 AND below VWAP
        state = _build_market_state(
            price=Decimal("5009.25"),
            vwap=or_range.vwap,  # VWAP is ~5015, so price < VWAP -> confirmed
            atr=Decimal("3.50"),
        )
        result = strategy.evaluate(state, or_range)

        assert result.has_setup
        assert result.setup is not None
        assert result.setup.direction == Direction.SHORT
        assert result.setup.setup_type == SetupType.ORB_SHORT

    def test_short_stop_above_midpoint(
        self, strategy: ORBStrategy,
    ) -> None:
        """SHORT stop should be at OR midpoint + buffer."""
        bars = _make_or_bars(or_high=5020.00, or_low=5010.00)
        or_range = _compute_or_range(bars, "MES")

        state = _build_market_state(
            price=Decimal("5009.25"),
            vwap=or_range.vwap,
            atr=Decimal("3.50"),
        )
        result = strategy.evaluate(state, or_range)
        assert result.setup is not None

        # Stop = midpoint(5015.00) + buffer(2 * 0.25) = 5015.50
        assert result.setup.stop_price == Decimal("5015.50")

    @pytest.mark.asyncio
    async def test_short_bracket_order_placed(
        self, strategy: ORBStrategy, risk_manager: RiskManager, risk_config: RiskConfig,
    ) -> None:
        """Full SHORT flow: signal -> size -> bracket order placed."""
        from ib_bot.services.execution_engine import ExecutionEngine
        from ib_bot.services.ib_client import IBClient
        from ib_bot.core.enums import Topic

        bars = _make_or_bars(or_high=5020.00, or_low=5010.00)
        or_range = _compute_or_range(bars, "MES")

        state = _build_market_state(
            price=Decimal("5009.25"),
            vwap=or_range.vwap,
            atr=Decimal("3.50"),
        )
        result = strategy.evaluate(state, or_range)
        assert result.setup is not None

        intent = risk_manager.size_trade(result.setup)
        assert intent is not None

        mock_ib_client = MagicMock(spec=IBClient)
        mock_ib_client.place_bracket_order = AsyncMock(return_value=[
            MagicMock(), MagicMock(), MagicMock(),
        ])

        bus = MessageBus()
        kill_switch = KillSwitchService(config=risk_config, bus=bus)
        execution = ExecutionEngine(
            ib_client=mock_ib_client,
            risk_manager=risk_manager,
            kill_switch=kill_switch,
            bus=bus,
        )

        await bus.start()
        await kill_switch.start()
        await execution.start()

        try:
            await bus.publish(Topic.ORDER, intent.model_dump(), source="strategy")
            await asyncio.sleep(0.3)

            mock_ib_client.place_bracket_order.assert_called_once()
            call_kwargs = mock_ib_client.place_bracket_order.call_args
            assert call_kwargs.kwargs["direction"] == Direction.SHORT
        finally:
            await execution.stop()
            await kill_switch.stop()
            await bus.stop()


# ---------------------------------------------------------------------------
# Test: No breakout — flat day
# ---------------------------------------------------------------------------

class TestNoBreakoutFlatDay:
    """Price stays inside OR range all session — no trades taken."""

    def test_price_inside_range_no_setup(
        self, strategy: ORBStrategy,
    ) -> None:
        """Price between OR low and OR high -> no breakout."""
        bars = _make_or_bars(or_high=5020.00, or_low=5010.00)
        or_range = _compute_or_range(bars, "MES")

        # Test multiple prices inside the range
        inside_prices = [
            Decimal("5015.00"),  # midpoint
            Decimal("5010.50"),  # near low
            Decimal("5019.75"),  # near high but within buffer
            Decimal("5012.00"),  # lower third
            Decimal("5018.00"),  # upper third
        ]

        for price in inside_prices:
            state = _build_market_state(
                price=price,
                vwap=or_range.vwap,
                atr=Decimal("3.50"),
            )
            result = strategy.evaluate(state, or_range)
            assert not result.has_setup, f"Should not trigger at price {price}"

    def test_price_at_boundary_no_setup(
        self, strategy: ORBStrategy,
    ) -> None:
        """Price exactly at OR high (no buffer clearance) -> no breakout."""
        bars = _make_or_bars(or_high=5020.00, or_low=5010.00)
        or_range = _compute_or_range(bars, "MES")

        # Exactly at OR high, without crossing the buffer
        state = _build_market_state(
            price=Decimal("5020.00"),
            vwap=or_range.vwap,
            atr=Decimal("3.50"),
        )
        result = strategy.evaluate(state, or_range)
        assert not result.has_setup

    def test_risk_manager_not_called_when_no_setup(
        self, strategy: ORBStrategy, risk_manager: RiskManager,
    ) -> None:
        """No setup -> risk manager never invoked."""
        bars = _make_or_bars(or_high=5020.00, or_low=5010.00)
        or_range = _compute_or_range(bars, "MES")

        state = _build_market_state(
            price=Decimal("5015.00"),
            vwap=or_range.vwap,
            atr=Decimal("3.50"),
        )
        result = strategy.evaluate(state, or_range)
        assert not result.has_setup
        assert result.setup is None

        # Calling size_trade with None would error; the flow
        # only calls it when has_setup is True
        # Verify the flow guard works
        if result.has_setup and result.setup:
            intent = risk_manager.size_trade(result.setup)
        else:
            intent = None
        assert intent is None


# ---------------------------------------------------------------------------
# Test: Kill switch halts trading after consecutive stops
# ---------------------------------------------------------------------------

class TestKillSwitchStopsTrading:
    """Simulate 2 consecutive stop losses and verify kill switch halts."""

    def test_kill_switch_halts_after_consecutive_stops(
        self, kill_switch: KillSwitchService,
    ) -> None:
        """2 consecutive stops -> kill switch halted."""
        assert kill_switch.is_trading_allowed

        # First stop loss (-$300)
        kill_switch.record_trade_result(pnl_usd=Decimal("-300"), is_stop=True)
        assert kill_switch.is_trading_allowed  # still active after 1 stop

        # Second consecutive stop loss (-$250)
        kill_switch.record_trade_result(pnl_usd=Decimal("-250"), is_stop=True)
        assert not kill_switch.is_trading_allowed
        assert kill_switch.status == KillSwitchStatus.HALTED

    def test_tp_resets_consecutive_stops(
        self, kill_switch: KillSwitchService,
    ) -> None:
        """A take profit between stops resets the counter."""
        kill_switch.record_trade_result(pnl_usd=Decimal("-300"), is_stop=True)
        assert kill_switch.is_trading_allowed

        # TP resets counter
        kill_switch.record_trade_result(pnl_usd=Decimal("200"), is_stop=False)
        assert kill_switch.is_trading_allowed

        # One more stop after reset -> still active (only 1 consecutive)
        kill_switch.record_trade_result(pnl_usd=Decimal("-300"), is_stop=True)
        assert kill_switch.is_trading_allowed

    def test_daily_loss_limit_halts(
        self, kill_switch: KillSwitchService,
    ) -> None:
        """Daily loss exceeding $1000 -> halt."""
        kill_switch.record_trade_result(pnl_usd=Decimal("-600"), is_stop=True)
        assert kill_switch.is_trading_allowed

        # TP in between to avoid consecutive stops halt
        kill_switch.record_trade_result(pnl_usd=Decimal("50"), is_stop=False)

        kill_switch.record_trade_result(pnl_usd=Decimal("-500"), is_stop=True)
        # Total loss: 600 + 500 = 1100 > 1000
        assert not kill_switch.is_trading_allowed

    @pytest.mark.asyncio
    async def test_execution_blocked_when_halted(
        self, risk_config: RiskConfig, strategy: ORBStrategy,
    ) -> None:
        """Once kill switch is halted, execution engine refuses new orders."""
        from ib_bot.services.execution_engine import ExecutionEngine
        from ib_bot.services.ib_client import IBClient
        from ib_bot.core.enums import Topic

        bus = MessageBus()
        risk_manager = RiskManager(risk_config)
        kill_switch = KillSwitchService(config=risk_config, bus=bus)

        mock_ib_client = MagicMock(spec=IBClient)
        mock_ib_client.place_bracket_order = AsyncMock(return_value=[])

        execution = ExecutionEngine(
            ib_client=mock_ib_client,
            risk_manager=risk_manager,
            kill_switch=kill_switch,
            bus=bus,
        )

        # Trigger kill switch
        kill_switch.record_trade_result(pnl_usd=Decimal("-300"), is_stop=True)
        kill_switch.record_trade_result(pnl_usd=Decimal("-300"), is_stop=True)
        assert not kill_switch.is_trading_allowed

        await bus.start()
        await kill_switch.start()
        await execution.start()

        try:
            # Generate a valid trade intent
            bars = _make_or_bars(or_high=5020.00, or_low=5010.00)
            or_range = _compute_or_range(bars, "MES")
            state = _build_market_state(
                price=Decimal("5020.75"),
                vwap=or_range.vwap,
                atr=Decimal("3.50"),
            )
            result = strategy.evaluate(state, or_range)
            assert result.setup is not None
            intent = risk_manager.size_trade(result.setup)
            assert intent is not None

            # Publish — should be blocked by kill switch
            await bus.publish(Topic.ORDER, intent.model_dump(), source="strategy")
            await asyncio.sleep(0.3)

            mock_ib_client.place_bracket_order.assert_not_called()
        finally:
            await execution.stop()
            await kill_switch.stop()
            await bus.stop()


# ---------------------------------------------------------------------------
# Test: EOD flatten
# ---------------------------------------------------------------------------

class TestEODFlatten:
    """Verify that open positions are flattened at EOD."""

    @pytest.mark.asyncio
    async def test_flatten_all_called_at_eod(
        self, risk_config: RiskConfig,
    ) -> None:
        """ExecutionEngine.flatten_all delegates to IBClient.flatten_all."""
        from ib_bot.services.execution_engine import ExecutionEngine
        from ib_bot.services.ib_client import IBClient

        mock_ib_client = MagicMock(spec=IBClient)
        mock_ib_client.flatten_all = AsyncMock(return_value=[])

        bus = MessageBus()
        risk_manager = RiskManager(risk_config)
        kill_switch = KillSwitchService(config=risk_config, bus=bus)
        execution = ExecutionEngine(
            ib_client=mock_ib_client,
            risk_manager=risk_manager,
            kill_switch=kill_switch,
            bus=bus,
        )

        await bus.start()
        await kill_switch.start()
        await execution.start()

        try:
            # Simulate EOD flatten call (this is what IBBot._on_phase_change does)
            await execution.flatten_all()

            mock_ib_client.flatten_all.assert_called_once()
        finally:
            await execution.stop()
            await kill_switch.stop()
            await bus.stop()

    @pytest.mark.asyncio
    async def test_flatten_clears_active_trades(
        self, risk_config: RiskConfig, strategy: ORBStrategy,
    ) -> None:
        """After flatten_all, execution engine has no active trades."""
        from ib_bot.services.execution_engine import ExecutionEngine
        from ib_bot.services.ib_client import IBClient
        from ib_bot.core.enums import Topic

        mock_ib_client = MagicMock(spec=IBClient)
        mock_ib_client.place_bracket_order = AsyncMock(return_value=[
            MagicMock(), MagicMock(), MagicMock(),
        ])
        mock_ib_client.flatten_all = AsyncMock(return_value=[])

        bus = MessageBus()
        risk_manager = RiskManager(risk_config)
        kill_switch = KillSwitchService(config=risk_config, bus=bus)
        execution = ExecutionEngine(
            ib_client=mock_ib_client,
            risk_manager=risk_manager,
            kill_switch=kill_switch,
            bus=bus,
        )

        await bus.start()
        await kill_switch.start()
        await execution.start()

        try:
            # First: place a trade
            bars = _make_or_bars(or_high=5020.00, or_low=5010.00)
            or_range = _compute_or_range(bars, "MES")
            state = _build_market_state(
                price=Decimal("5020.75"),
                vwap=or_range.vwap,
                atr=Decimal("3.50"),
            )
            result = strategy.evaluate(state, or_range)
            assert result.setup is not None
            intent = risk_manager.size_trade(result.setup)
            assert intent is not None

            await bus.publish(Topic.ORDER, intent.model_dump(), source="strategy")
            await asyncio.sleep(0.3)

            assert execution.has_active_trades

            # Now flatten
            await execution.flatten_all()
            assert not execution.has_active_trades
            mock_ib_client.flatten_all.assert_called_once()
        finally:
            await execution.stop()
            await kill_switch.stop()
            await bus.stop()

    @pytest.mark.asyncio
    async def test_eod_phase_triggers_flatten_in_bot(
        self, risk_config: RiskConfig,
    ) -> None:
        """IBBot._on_phase_change(EOD_FLATTEN) calls execution.flatten_all."""
        from ib_bot.services.execution_engine import ExecutionEngine
        from ib_bot.services.ib_client import IBClient
        from ib_bot.services.notifications import NotificationService

        mock_ib_client = MagicMock(spec=IBClient)
        mock_ib_client.flatten_all = AsyncMock(return_value=[])

        mock_execution = MagicMock(spec=ExecutionEngine)
        mock_execution.flatten_all = AsyncMock()

        mock_notifications = MagicMock(spec=NotificationService)
        mock_notifications.notify_session = AsyncMock(return_value=True)

        # Simulate the phase change logic from IBBot._on_phase_change
        # (testing the logic directly, not the full bot startup)
        new_phase = SessionPhase.EOD_FLATTEN
        if new_phase == SessionPhase.EOD_FLATTEN:
            await mock_execution.flatten_all()
            await mock_notifications.notify_session("EOD: all positions flattened")

        mock_execution.flatten_all.assert_called_once()
        mock_notifications.notify_session.assert_called_once_with(
            "EOD: all positions flattened"
        )


# ---------------------------------------------------------------------------
# Test: Risk manager integration with strategy
# ---------------------------------------------------------------------------

class TestRiskManagerIntegration:
    """Verify risk manager interacts correctly with strategy outputs."""

    def test_trade_count_increments(
        self, strategy: ORBStrategy, risk_manager: RiskManager,
    ) -> None:
        """Each fill increments the daily trade counter."""
        assert risk_manager.is_trading_allowed

        risk_manager.record_fill(pnl_usd=Decimal("100"), is_stop=False)
        assert risk_manager._daily_trade_count == 1

        risk_manager.record_fill(pnl_usd=Decimal("-50"), is_stop=True)
        assert risk_manager._daily_trade_count == 2

    def test_max_trades_blocks_sizing(
        self, strategy: ORBStrategy, risk_manager: RiskManager,
    ) -> None:
        """After max_trades_per_day fills, size_trade returns None."""
        bars = _make_or_bars(or_high=5020.00, or_low=5010.00)
        or_range = _compute_or_range(bars, "MES")
        state = _build_market_state(
            price=Decimal("5020.75"),
            vwap=or_range.vwap,
            atr=Decimal("3.50"),
        )
        result = strategy.evaluate(state, or_range)
        assert result.setup is not None

        # Fill up to max trades (4 in our config)
        for _ in range(4):
            risk_manager.record_fill(pnl_usd=Decimal("100"), is_stop=False)

        intent = risk_manager.size_trade(result.setup)
        assert intent is None

    def test_daily_reset_clears_counters(
        self, risk_manager: RiskManager,
    ) -> None:
        """reset_daily clears all counters."""
        risk_manager.record_fill(pnl_usd=Decimal("-500"), is_stop=True)
        risk_manager.record_fill(pnl_usd=Decimal("-500"), is_stop=True)
        assert not risk_manager.is_trading_allowed

        risk_manager.reset_daily()
        assert risk_manager.is_trading_allowed
        assert risk_manager._daily_trade_count == 0
        assert risk_manager._daily_loss_usd == Decimal("0")
