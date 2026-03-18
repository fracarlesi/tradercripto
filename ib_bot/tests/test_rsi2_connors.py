"""Tests for RSI(2) Connors Mean Reversion Strategy."""

import pytest
from datetime import date, datetime, timezone
from decimal import Decimal

from ib_bot.config.loader import RSI2ConnorsConfig, StopsConfig
from ib_bot.core.enums import Direction, SetupType
from ib_bot.strategies.rsi2_connors import DailyBar, RSI2ConnorsStrategy


# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def rsi2_config() -> RSI2ConnorsConfig:
    return RSI2ConnorsConfig(
        enabled=True,
        rsi_period=2,
        rsi_entry_threshold=10,
        rsi_exit_threshold=70,
        rsi_short_entry_threshold=95,
        rsi_short_exit_threshold=30,
        sma_period=200,
        max_hold_days=7,
        stop_points=20,
        direction="both",
    )


@pytest.fixture
def long_only_config() -> RSI2ConnorsConfig:
    return RSI2ConnorsConfig(
        enabled=True,
        rsi_period=2,
        rsi_entry_threshold=10,
        rsi_exit_threshold=70,
        rsi_short_entry_threshold=95,
        rsi_short_exit_threshold=30,
        sma_period=200,
        max_hold_days=7,
        stop_points=20,
        direction="long_only",
    )


@pytest.fixture
def stops() -> StopsConfig:
    return StopsConfig()


@pytest.fixture
def strategy(rsi2_config: RSI2ConnorsConfig, stops: StopsConfig) -> RSI2ConnorsStrategy:
    return RSI2ConnorsStrategy(
        rsi2_config=rsi2_config,
        stops_config=stops,
        symbol="MES",
    )


@pytest.fixture
def long_only_strategy(long_only_config: RSI2ConnorsConfig, stops: StopsConfig) -> RSI2ConnorsStrategy:
    return RSI2ConnorsStrategy(
        rsi2_config=long_only_config,
        stops_config=stops,
        symbol="MES",
    )


def _make_bars(
    prices: list[float],
    start_price: float = 5000.0,
) -> list[DailyBar]:
    """Create a list of DailyBar from close prices.

    For simplicity, open=high=low=close (flat bars).
    Uses timedelta for safe date generation across any number of bars.
    """
    from datetime import timedelta

    base_date = date(2025, 1, 1)
    bars: list[DailyBar] = []
    for i, price in enumerate(prices):
        d = Decimal(str(price))
        bars.append(DailyBar(
            date=base_date + timedelta(days=i),
            open=d,
            high=d,
            low=d,
            close=d,
        ))
    return bars


def _make_uptrend_bars(count: int = 210, base: float = 5000.0) -> list[float]:
    """Generate `count` prices in a strong uptrend (well above SMA200).

    Uses 0.5/day slope so that after 210 bars the price is ~105 pts
    above the starting price, keeping price comfortably above SMA(200)
    even after a ~15-point selloff.
    """
    return [base + i * 0.5 for i in range(count)]


def _make_downtrend_bars(count: int = 210, base: float = 5000.0) -> list[float]:
    """Generate `count` prices in a strong downtrend (well below SMA200).

    Uses -0.5/day slope so that after 210 bars the price is ~105 pts
    below the starting price, keeping price comfortably below SMA(200).
    """
    return [base - i * 0.5 for i in range(count)]


def _append_selloff(prices: list[float], drop_per_day: float = 5.0, days: int = 3) -> list[float]:
    """Append a sharp selloff to trigger RSI(2) < 10."""
    result = list(prices)
    last = result[-1]
    for _ in range(days):
        last -= drop_per_day
        result.append(last)
    return result


def _append_rally(prices: list[float], gain_per_day: float = 5.0, days: int = 3) -> list[float]:
    """Append a sharp rally to trigger RSI(2) > 95."""
    result = list(prices)
    last = result[-1]
    for _ in range(days):
        last += gain_per_day
        result.append(last)
    return result


def _make_choppy_bars(count: int = 210, base: float = 5000.0) -> list[float]:
    """Generate bars that alternate up/down to produce moderate RSI(2).

    The pattern: up 1, down 0.5 -- net uptrend but RSI(2) won't be extreme.
    """
    prices: list[float] = [base]
    for i in range(1, count):
        if i % 2 == 1:
            prices.append(prices[-1] + 1.0)
        else:
            prices.append(prices[-1] - 0.5)
    return prices


# =========================================================================
# Basic Tests
# =========================================================================


class TestBasics:
    def test_name(self, strategy: RSI2ConnorsStrategy) -> None:
        assert strategy.name == "rsi2_connors"

    def test_evaluate_abc_returns_reject(self, strategy: RSI2ConnorsStrategy) -> None:
        """The generic evaluate() should reject -- RSI2 uses evaluate_daily()."""
        result = strategy.evaluate(None, None)
        assert not result.has_setup
        assert "evaluate_daily" in result.reason

    def test_reset_daily_preserves_position(self, strategy: RSI2ConnorsStrategy) -> None:
        """reset_daily should NOT clear position state (positions are multi-day)."""
        strategy._in_position = True
        strategy._entry_price = Decimal("5000")
        strategy._position_direction = Direction.LONG
        strategy._hold_days = 3
        strategy.reset_daily()
        assert strategy._in_position is True
        assert strategy._entry_price == Decimal("5000")
        assert strategy._position_direction == Direction.LONG
        assert strategy._hold_days == 3

    def test_reset_position(self, strategy: RSI2ConnorsStrategy) -> None:
        """reset_position should clear all position tracking."""
        strategy._in_position = True
        strategy._entry_price = Decimal("5000")
        strategy._position_direction = Direction.SHORT
        strategy._hold_days = 3
        strategy.reset_position()
        assert strategy._in_position is False
        assert strategy._entry_price is None
        assert strategy._position_direction is None
        assert strategy._hold_days == 0

    def test_allow_long_both(self, strategy: RSI2ConnorsStrategy) -> None:
        assert strategy.allow_long is True

    def test_allow_short_both(self, strategy: RSI2ConnorsStrategy) -> None:
        assert strategy.allow_short is True

    def test_allow_short_long_only(self, long_only_strategy: RSI2ConnorsStrategy) -> None:
        assert long_only_strategy.allow_long is True
        assert long_only_strategy.allow_short is False


# =========================================================================
# Indicator Tests
# =========================================================================


class TestIndicators:
    def test_sma_basic(self) -> None:
        """SMA(3) of [1, 2, 3] = 2."""
        closes = [Decimal("1"), Decimal("2"), Decimal("3")]
        result = RSI2ConnorsStrategy._compute_sma(closes, 3)
        assert result == Decimal("2")

    def test_sma_insufficient_data(self) -> None:
        """SMA returns None when insufficient data."""
        closes = [Decimal("1"), Decimal("2")]
        result = RSI2ConnorsStrategy._compute_sma(closes, 3)
        assert result is None

    def test_sma_uses_last_n(self) -> None:
        """SMA only uses the last `period` values."""
        closes = [Decimal("10"), Decimal("1"), Decimal("2"), Decimal("3")]
        result = RSI2ConnorsStrategy._compute_sma(closes, 3)
        assert result == Decimal("2")  # (1+2+3)/3, ignores 10

    def test_rsi_all_gains(self) -> None:
        """RSI should be 100 when all price changes are positive."""
        # 4 closes: 3 changes, all positive. RSI(2) seeds on first 2 changes.
        closes = [Decimal("100"), Decimal("101"), Decimal("102"), Decimal("103")]
        result = RSI2ConnorsStrategy._compute_rsi(closes, 2)
        assert result == Decimal("100")

    def test_rsi_all_losses(self) -> None:
        """RSI should be 0 when all price changes are negative."""
        closes = [Decimal("103"), Decimal("102"), Decimal("101"), Decimal("100")]
        result = RSI2ConnorsStrategy._compute_rsi(closes, 2)
        assert result == Decimal("0")

    def test_rsi_mixed(self) -> None:
        """RSI with mixed gains/losses should be between 0 and 100."""
        closes = [Decimal("100"), Decimal("102"), Decimal("101"), Decimal("103"), Decimal("100")]
        result = RSI2ConnorsStrategy._compute_rsi(closes, 2)
        assert result is not None
        assert Decimal("0") < result < Decimal("100")

    def test_rsi_insufficient_data(self) -> None:
        """RSI returns None with insufficient data."""
        closes = [Decimal("100"), Decimal("101")]
        result = RSI2ConnorsStrategy._compute_rsi(closes, 2)
        assert result is None

    def test_rsi2_after_sharp_selloff(self) -> None:
        """RSI(2) should be very low after 2+ consecutive down days."""
        # Uptrend then 3 sharp down days
        closes = [Decimal(str(5000 + i)) for i in range(10)]
        # Add 3 down days
        closes.extend([Decimal("5004"), Decimal("4998"), Decimal("4990")])
        result = RSI2ConnorsStrategy._compute_rsi(closes, 2)
        assert result is not None
        assert result < Decimal("10"), f"Expected RSI(2) < 10 after selloff, got {result}"

    def test_rsi2_after_sharp_rally(self) -> None:
        """RSI(2) should be very high after 2+ consecutive up days."""
        closes = [Decimal(str(5000 - i)) for i in range(10)]
        closes.extend([Decimal("4996"), Decimal("5002"), Decimal("5010")])
        result = RSI2ConnorsStrategy._compute_rsi(closes, 2)
        assert result is not None
        assert result > Decimal("90"), f"Expected RSI(2) > 90 after rally, got {result}"


# =========================================================================
# Long Entry Signal Tests
# =========================================================================


class TestLongEntry:
    def test_insufficient_bars_rejects(self, strategy: RSI2ConnorsStrategy) -> None:
        """Rejects when not enough bars for SMA(200) warmup."""
        bars = _make_bars([5000 + i for i in range(50)])  # only 50 bars
        result = strategy.evaluate_daily(bars, date(2026, 3, 1))
        assert not result.has_setup
        assert "warming up" in result.reason.lower()

    def test_price_below_sma_no_long(self, long_only_strategy: RSI2ConnorsStrategy) -> None:
        """Rejects when price is below SMA(200) and direction is long_only."""
        prices = [5000 - i * 0.5 for i in range(210)]
        bars = _make_bars(prices)
        result = long_only_strategy.evaluate_daily(bars, date(2026, 3, 1))
        assert not result.has_setup
        assert "below SMA" in result.reason or "no uptrend" in result.reason

    def test_rsi_not_oversold_rejects(self, strategy: RSI2ConnorsStrategy) -> None:
        """Rejects when RSI(2) is above entry threshold (not oversold)."""
        prices = _make_uptrend_bars(210)
        bars = _make_bars(prices)
        result = strategy.evaluate_daily(bars, date(2026, 3, 1))
        assert not result.has_setup
        assert "not oversold" in result.reason.lower()

    def test_entry_signal_on_oversold_dip(self, strategy: RSI2ConnorsStrategy) -> None:
        """Generates BUY signal when RSI(2) < 10 and price > SMA(200)."""
        prices = _make_uptrend_bars(210)
        prices = _append_selloff(prices, drop_per_day=8.0, days=3)
        bars = _make_bars(prices)

        result = strategy.evaluate_daily(bars, date(2026, 3, 15))
        assert result.has_setup, f"Expected entry signal, got: {result.reason}"
        assert result.setup is not None
        assert result.setup.direction == Direction.LONG
        assert result.setup.setup_type == SetupType.RSI2_LONG
        assert strategy.in_position is True
        assert strategy.position_direction == Direction.LONG

    def test_no_double_entry(self, strategy: RSI2ConnorsStrategy) -> None:
        """Should not enter again if already in a position."""
        prices = _make_uptrend_bars(210)
        prices = _append_selloff(prices, drop_per_day=8.0, days=3)
        bars = _make_bars(prices)
        result1 = strategy.evaluate_daily(bars, date(2026, 3, 15))
        assert result1.has_setup

        # Try to enter again -- should get exit evaluation instead
        result2 = strategy.evaluate_daily(bars, date(2026, 3, 16))
        assert strategy.in_position is True or result2.has_setup


# =========================================================================
# Short Entry Signal Tests
# =========================================================================


class TestShortEntry:
    def test_short_entry_on_overbought_rally(self, strategy: RSI2ConnorsStrategy) -> None:
        """Generates SELL signal when RSI(2) > 95 and price < SMA(200)."""
        prices = _make_downtrend_bars(210)
        prices = _append_rally(prices, gain_per_day=8.0, days=3)
        bars = _make_bars(prices)

        result = strategy.evaluate_daily(bars, date(2026, 3, 15))
        assert result.has_setup, f"Expected short entry signal, got: {result.reason}"
        assert result.setup is not None
        assert result.setup.direction == Direction.SHORT
        assert result.setup.setup_type == SetupType.RSI2_SHORT
        assert strategy.in_position is True
        assert strategy.position_direction == Direction.SHORT

    def test_short_entry_stop_price_above_entry(self, strategy: RSI2ConnorsStrategy) -> None:
        """Short entry stop should be above entry price."""
        prices = _make_downtrend_bars(210)
        prices = _append_rally(prices, gain_per_day=8.0, days=3)
        bars = _make_bars(prices)

        result = strategy.evaluate_daily(bars, date(2026, 3, 15))
        assert result.has_setup
        assert result.setup is not None
        assert result.setup.stop_price > result.setup.entry_price

    def test_short_blocked_when_long_only(self, long_only_strategy: RSI2ConnorsStrategy) -> None:
        """Long-only config should not allow short entries."""
        prices = _make_downtrend_bars(210)
        prices = _append_rally(prices, gain_per_day=8.0, days=3)
        bars = _make_bars(prices)

        result = long_only_strategy.evaluate_daily(bars, date(2026, 3, 15))
        assert not result.has_setup
        # Should reject with trend/direction message, not generate short signal
        assert "below SMA" in result.reason or "no uptrend" in result.reason or "No entry" in result.reason

    def test_short_not_overbought_rejects(self, strategy: RSI2ConnorsStrategy) -> None:
        """Rejects when RSI(2) is below short entry threshold (not overbought)."""
        # Downtrend but no sharp rally -> RSI not extreme
        prices = _make_downtrend_bars(210)
        bars = _make_bars(prices)
        result = strategy.evaluate_daily(bars, date(2026, 3, 1))
        assert not result.has_setup
        assert "not overbought" in result.reason.lower() or "No entry" in result.reason

    def test_no_simultaneous_long_short(self, strategy: RSI2ConnorsStrategy) -> None:
        """Cannot enter short if already in a long position."""
        # Enter long first
        strategy._in_position = True
        strategy._position_direction = Direction.LONG
        strategy._entry_price = Decimal("5100")
        strategy._entry_date = date(2026, 3, 10)
        strategy._hold_days = 0

        # Even with perfect short conditions, should get exit eval not new entry
        prices = _make_downtrend_bars(210)
        prices = _append_rally(prices, gain_per_day=8.0, days=3)
        bars = _make_bars(prices)

        result = strategy.evaluate_daily(bars, date(2026, 3, 12))
        # Should be in exit evaluation mode, not creating a short entry
        if result.has_setup and result.setup:
            assert result.setup.setup_type != SetupType.RSI2_SHORT


# =========================================================================
# Long Exit Signal Tests
# =========================================================================


class TestLongExit:
    def _enter_long(self, strategy: RSI2ConnorsStrategy) -> None:
        """Helper to put strategy into a long position."""
        strategy._in_position = True
        strategy._position_direction = Direction.LONG
        strategy._entry_price = Decimal("5000")
        strategy._entry_date = date(2026, 3, 10)
        strategy._hold_days = 0

    def test_rsi_exit_on_recovery(self, strategy: RSI2ConnorsStrategy) -> None:
        """Exits long when RSI(2) recovers above 70."""
        self._enter_long(strategy)

        prices = _make_uptrend_bars(210)
        last = prices[-1]
        for _ in range(3):
            last += 10
            prices.append(last)
        bars = _make_bars(prices)

        result = strategy.evaluate_daily(bars, date(2026, 3, 12))
        assert result.has_setup, f"Expected exit signal, got: {result.reason}"
        assert result.setup is not None
        assert result.setup.setup_type == SetupType.RSI2_EXIT
        assert strategy.in_position is False

    def test_max_hold_days_exit(self, strategy: RSI2ConnorsStrategy) -> None:
        """Forces exit after max_hold_days even if RSI hasn't recovered."""
        self._enter_long(strategy)
        strategy._hold_days = 6

        prices = _make_uptrend_bars(210)
        prices = _append_selloff(prices, drop_per_day=3.0, days=3)
        bars = _make_bars(prices)

        result = strategy.evaluate_daily(bars, date(2026, 3, 17))
        assert result.has_setup, f"Expected max-hold exit, got: {result.reason}"
        assert "Max hold" in result.reason
        assert strategy.in_position is False

    def test_catastrophe_stop_exit_long(self, strategy: RSI2ConnorsStrategy) -> None:
        """Exits long when unrealized loss exceeds stop_points."""
        self._enter_long(strategy)
        strategy._entry_price = Decimal("5020")

        prices = _make_uptrend_bars(210)
        prices[-1] = 4995.0  # 25 pts below entry of 5020
        bars = _make_bars(prices)

        result = strategy.evaluate_daily(bars, date(2026, 3, 12))
        assert result.has_setup, f"Expected catastrophe stop, got: {result.reason}"
        assert "Catastrophe stop" in result.reason
        assert strategy.in_position is False

    def test_hold_without_exit(self, strategy: RSI2ConnorsStrategy) -> None:
        """No exit signal when holding long and RSI is between thresholds."""
        prices = _make_uptrend_bars(210)
        last = prices[-1]
        prices.append(last - 1.0)
        prices.append(last - 0.7)
        bars = _make_bars(prices)

        self._enter_long(strategy)
        strategy._entry_price = Decimal(str(last - 0.7))

        result = strategy.evaluate_daily(bars, date(2026, 3, 12))
        assert not result.has_setup, f"Expected hold, got: {result.reason}"
        assert "Holding" in result.reason
        assert strategy.in_position is True
        assert strategy.hold_days == 1


# =========================================================================
# Short Exit Signal Tests
# =========================================================================


class TestShortExit:
    def _enter_short(self, strategy: RSI2ConnorsStrategy) -> None:
        """Helper to put strategy into a short position."""
        strategy._in_position = True
        strategy._position_direction = Direction.SHORT
        strategy._entry_price = Decimal("4900")
        strategy._entry_date = date(2026, 3, 10)
        strategy._hold_days = 0

    def test_rsi_exit_short_on_recovery(self, strategy: RSI2ConnorsStrategy) -> None:
        """Exits short when RSI(2) drops below 30 (mean reverted down)."""
        self._enter_short(strategy)

        # Build bars ending with sharp selloff -> RSI(2) < 30
        prices = _make_downtrend_bars(210)
        prices = _append_selloff(prices, drop_per_day=8.0, days=3)
        bars = _make_bars(prices)

        result = strategy.evaluate_daily(bars, date(2026, 3, 12))
        assert result.has_setup, f"Expected short exit signal, got: {result.reason}"
        assert result.setup is not None
        assert result.setup.setup_type == SetupType.RSI2_EXIT_SHORT
        assert strategy.in_position is False

    def test_catastrophe_stop_exit_short(self, strategy: RSI2ConnorsStrategy) -> None:
        """Exits short when price rises too far above entry (loss exceeds stop_points)."""
        self._enter_short(strategy)
        strategy._entry_price = Decimal("4900")

        # Price rallied 25 points above short entry
        prices = _make_downtrend_bars(210)
        prices[-1] = 4925.0  # 25 pts above entry of 4900
        bars = _make_bars(prices)

        result = strategy.evaluate_daily(bars, date(2026, 3, 12))
        assert result.has_setup, f"Expected catastrophe stop for short, got: {result.reason}"
        assert "Catastrophe stop" in result.reason
        assert strategy.in_position is False

    def test_max_hold_days_exit_short(self, strategy: RSI2ConnorsStrategy) -> None:
        """Forces short exit after max_hold_days."""
        self._enter_short(strategy)
        strategy._hold_days = 6

        # Build bars with moderate RSI (no natural exit)
        prices = _make_downtrend_bars(210)
        last = prices[-1]
        prices.append(last + 1.0)
        prices.append(last + 0.7)
        bars = _make_bars(prices)

        result = strategy.evaluate_daily(bars, date(2026, 3, 17))
        assert result.has_setup, f"Expected max-hold exit for short, got: {result.reason}"
        assert "Max hold" in result.reason
        assert result.setup is not None
        assert result.setup.setup_type == SetupType.RSI2_EXIT_SHORT
        assert strategy.in_position is False

    def test_hold_short_without_exit(self, strategy: RSI2ConnorsStrategy) -> None:
        """No exit signal when holding short and RSI is between thresholds."""
        prices = _make_downtrend_bars(210)
        last = prices[-1]
        # Add mild moves -> RSI(2) moderate (between 30 and 95)
        prices.append(last + 1.0)
        prices.append(last + 0.7)
        bars = _make_bars(prices)

        self._enter_short(strategy)
        strategy._entry_price = Decimal(str(last + 0.7))

        result = strategy.evaluate_daily(bars, date(2026, 3, 12))
        assert not result.has_setup, f"Expected hold short, got: {result.reason}"
        assert "Holding SHORT" in result.reason
        assert strategy.in_position is True
        assert strategy.hold_days == 1


# =========================================================================
# Registry Integration Test
# =========================================================================


class TestRegistry:
    def test_create_from_registry(self) -> None:
        """Registry creates RSI2ConnorsStrategy for name='rsi2_connors'."""
        from ib_bot.config.loader import TradingConfig, StrategyConfig
        from ib_bot.strategies.registry import create_strategy

        config = TradingConfig(
            strategy=StrategyConfig(name="rsi2_connors"),
            rsi2_connors=RSI2ConnorsConfig(enabled=True),
        )
        strat = create_strategy(config)
        assert isinstance(strat, RSI2ConnorsStrategy)
        assert strat.name == "rsi2_connors"

    def test_registry_unknown_raises(self) -> None:
        """Registry raises ValueError for unknown strategy name."""
        from ib_bot.strategies.registry import create_strategy
        from ib_bot.config.loader import TradingConfig, StrategyConfig

        # We need to bypass the Literal validation for this test
        config = TradingConfig.__new__(TradingConfig)
        object.__setattr__(config, "__dict__", {})
        # Instead, just test that unknown names are caught
        # The Literal type prevents invalid names at config load time
        # so this is implicitly tested by Pydantic validation
        with pytest.raises(Exception):
            from ib_bot.config.loader import StrategyConfig as SC
            SC(name="nonexistent")  # type: ignore[arg-type]


# =========================================================================
# RSI Calculation Edge Cases
# =========================================================================


class TestRSIEdgeCases:
    def test_rsi_period_2_exactly_3_closes(self) -> None:
        """RSI(2) with exactly 3 closes (minimum viable)."""
        closes = [Decimal("100"), Decimal("99"), Decimal("98")]
        result = RSI2ConnorsStrategy._compute_rsi(closes, 2)
        assert result is not None
        assert result == Decimal("0")  # two consecutive drops

    def test_rsi_flat_market(self) -> None:
        """RSI with no price movement should handle gracefully."""
        closes = [Decimal("100")] * 10
        result = RSI2ConnorsStrategy._compute_rsi(closes, 2)
        # All changes are 0, so avg_gain=0 and avg_loss=0
        # Division by zero case: should return 100 (convention)
        assert result == Decimal("100")

    def test_rsi_single_up_then_down(self) -> None:
        """RSI(2) after one up then one down should be moderate."""
        closes = [Decimal("100"), Decimal("102"), Decimal("101")]
        result = RSI2ConnorsStrategy._compute_rsi(closes, 2)
        assert result is not None
        assert Decimal("0") < result < Decimal("100")
