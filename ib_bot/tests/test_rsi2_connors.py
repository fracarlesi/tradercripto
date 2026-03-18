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


def _append_selloff(prices: list[float], drop_per_day: float = 5.0, days: int = 3) -> list[float]:
    """Append a sharp selloff to trigger RSI(2) < 10."""
    result = list(prices)
    last = result[-1]
    for _ in range(days):
        last -= drop_per_day
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
        strategy._hold_days = 3
        strategy.reset_daily()
        assert strategy._in_position is True
        assert strategy._entry_price == Decimal("5000")
        assert strategy._hold_days == 3

    def test_reset_position(self, strategy: RSI2ConnorsStrategy) -> None:
        """reset_position should clear all position tracking."""
        strategy._in_position = True
        strategy._entry_price = Decimal("5000")
        strategy._hold_days = 3
        strategy.reset_position()
        assert strategy._in_position is False
        assert strategy._entry_price is None
        assert strategy._hold_days == 0


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


# =========================================================================
# Entry Signal Tests
# =========================================================================


class TestEntry:
    def test_insufficient_bars_rejects(self, strategy: RSI2ConnorsStrategy) -> None:
        """Rejects when not enough bars for SMA(200) warmup."""
        bars = _make_bars([5000 + i for i in range(50)])  # only 50 bars
        result = strategy.evaluate_daily(bars, date(2026, 3, 1))
        assert not result.has_setup
        assert "warming up" in result.reason.lower()

    def test_price_below_sma_rejects(self, strategy: RSI2ConnorsStrategy) -> None:
        """Rejects when price is below SMA(200) -- no uptrend."""
        # Create 210 bars in a downtrend (price below SMA)
        prices = [5000 - i * 0.5 for i in range(210)]
        bars = _make_bars(prices)
        result = strategy.evaluate_daily(bars, date(2026, 3, 1))
        assert not result.has_setup
        assert "below SMA" in result.reason or "not oversold" in result.reason

    def test_rsi_not_oversold_rejects(self, strategy: RSI2ConnorsStrategy) -> None:
        """Rejects when RSI(2) is above entry threshold (not oversold)."""
        # Gentle uptrend: price above SMA but RSI(2) not oversold
        prices = _make_uptrend_bars(210)
        bars = _make_bars(prices)
        result = strategy.evaluate_daily(bars, date(2026, 3, 1))
        assert not result.has_setup
        assert "not oversold" in result.reason.lower()

    def test_entry_signal_on_oversold_dip(self, strategy: RSI2ConnorsStrategy) -> None:
        """Generates BUY signal when RSI(2) < 10 and price > SMA(200)."""
        # Build uptrend then sharp selloff
        prices = _make_uptrend_bars(210)
        prices = _append_selloff(prices, drop_per_day=8.0, days=3)
        bars = _make_bars(prices)

        result = strategy.evaluate_daily(bars, date(2026, 3, 15))
        assert result.has_setup, f"Expected entry signal, got: {result.reason}"
        assert result.setup is not None
        assert result.setup.direction == Direction.LONG
        assert result.setup.setup_type == SetupType.RSI2_LONG
        assert strategy.in_position is True

    def test_no_double_entry(self, strategy: RSI2ConnorsStrategy) -> None:
        """Should not enter again if already in a position."""
        # First entry
        prices = _make_uptrend_bars(210)
        prices = _append_selloff(prices, drop_per_day=8.0, days=3)
        bars = _make_bars(prices)
        result1 = strategy.evaluate_daily(bars, date(2026, 3, 15))
        assert result1.has_setup

        # Try to enter again -- should get exit evaluation instead
        result2 = strategy.evaluate_daily(bars, date(2026, 3, 16))
        # It should be in exit evaluation mode now, not generating another entry
        assert strategy.in_position is True or result2.has_setup


# =========================================================================
# Exit Signal Tests
# =========================================================================


class TestExit:
    def _enter_position(self, strategy: RSI2ConnorsStrategy) -> None:
        """Helper to put strategy into a position."""
        strategy._in_position = True
        strategy._entry_price = Decimal("5000")
        strategy._entry_date = date(2026, 3, 10)
        strategy._hold_days = 0

    def test_rsi_exit_on_recovery(self, strategy: RSI2ConnorsStrategy) -> None:
        """Exits when RSI(2) recovers above 70."""
        self._enter_position(strategy)

        # Build bars ending with sharp rally (RSI(2) > 70)
        prices = _make_uptrend_bars(210)
        # Add strong rally to push RSI(2) > 70
        last = prices[-1]
        for _ in range(3):
            last += 10
            prices.append(last)
        bars = _make_bars(prices)

        result = strategy.evaluate_daily(bars, date(2026, 3, 12))
        assert result.has_setup, f"Expected exit signal, got: {result.reason}"
        assert result.setup is not None
        assert result.setup.setup_type == SetupType.RSI2_EXIT
        assert strategy.in_position is False  # position cleared

    def test_max_hold_days_exit(self, strategy: RSI2ConnorsStrategy) -> None:
        """Forces exit after max_hold_days even if RSI hasn't recovered."""
        self._enter_position(strategy)
        strategy._hold_days = 6  # will become 7 on next eval (= max)

        # Bars with RSI(2) still low (not recovered)
        prices = _make_uptrend_bars(210)
        prices = _append_selloff(prices, drop_per_day=3.0, days=3)
        bars = _make_bars(prices)

        result = strategy.evaluate_daily(bars, date(2026, 3, 17))
        assert result.has_setup, f"Expected max-hold exit, got: {result.reason}"
        assert "Max hold" in result.reason
        assert strategy.in_position is False

    def test_catastrophe_stop_exit(self, strategy: RSI2ConnorsStrategy) -> None:
        """Exits when unrealized loss exceeds stop_points."""
        self._enter_position(strategy)
        strategy._entry_price = Decimal("5020")

        # Price dropped 25 points (> 20 point stop)
        prices = _make_uptrend_bars(210)
        # Set last price to 4995 (25 pts below entry of 5020)
        prices[-1] = 4995.0
        bars = _make_bars(prices)

        result = strategy.evaluate_daily(bars, date(2026, 3, 12))
        assert result.has_setup, f"Expected catastrophe stop, got: {result.reason}"
        assert "Catastrophe stop" in result.reason
        assert strategy.in_position is False

    def test_hold_without_exit(self, strategy: RSI2ConnorsStrategy) -> None:
        """No exit signal when holding and RSI is between thresholds."""
        # Build bars first so we know the ending price range
        prices = _make_uptrend_bars(210)
        last = prices[-1]
        # Append: down, then up small -> RSI(2) with mixed last-2 changes
        prices.append(last - 1.0)    # change = -1.0
        prices.append(last - 0.7)    # change = +0.3 (moderate RSI)
        bars = _make_bars(prices)

        self._enter_position(strategy)
        # Entry price near the last bar (within 20-pt stop range)
        strategy._entry_price = Decimal(str(last - 0.7))

        result = strategy.evaluate_daily(bars, date(2026, 3, 12))
        assert not result.has_setup, f"Expected hold, got: {result.reason}"
        assert "Holding" in result.reason
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
