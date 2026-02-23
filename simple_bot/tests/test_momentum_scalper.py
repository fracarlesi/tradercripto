"""
Tests for MomentumScalperStrategy
===================================

Tests EMA9/EMA21 crossover strategy with RSI filter.

Run:
    pytest simple_bot/tests/test_momentum_scalper.py -v
"""

import pytest
from datetime import datetime, timezone
from decimal import Decimal

from simple_bot.core.models import (
    MarketState,
    Regime,
    Direction,
    SetupType,
)
from simple_bot.strategies.momentum_scalper import MomentumScalperStrategy


# =============================================================================
# Fixtures
# =============================================================================

def _make_state(**overrides) -> MarketState:
    """Create a MarketState with sensible BTC defaults."""
    defaults = dict(
        symbol="BTC",
        timeframe="15m",
        timestamp=datetime.now(timezone.utc),
        open=Decimal("97000"),
        high=Decimal("97500"),
        low=Decimal("96800"),
        close=Decimal("97200"),
        volume=Decimal("500"),
        atr=Decimal("150"),
        atr_pct=Decimal("0.15"),
        adx=Decimal("30"),
        rsi=Decimal("50"),
        ema50=Decimal("96000"),
        ema200=Decimal("94000"),
        ema200_slope=Decimal("0.001"),
        sma20=Decimal("96500"),
        sma50=Decimal("95500"),
        ema9=Decimal("97300"),
        ema21=Decimal("97000"),
        regime=Regime.TREND,
        trend_direction=Direction.LONG,
    )
    defaults.update(overrides)
    return MarketState(**defaults)


@pytest.fixture
def strategy():
    """Default momentum scalper with standard config."""
    return MomentumScalperStrategy(config={
        "allow_short": True,
        "min_atr_pct": 0.1,
        "stop_loss_pct": 0.8,
        "take_profit_pct": 1.6,
    })


@pytest.fixture
def long_only_strategy():
    """Momentum scalper with shorts disabled."""
    return MomentumScalperStrategy(config={
        "allow_short": False,
        "min_atr_pct": 0.1,
        "stop_loss_pct": 0.8,
        "take_profit_pct": 1.6,
    })


# =============================================================================
# LONG Signal Tests
# =============================================================================

class TestLongSignal:
    """Test LONG entry conditions."""

    def test_long_signal_basic(self, strategy):
        """LONG when EMA9 > EMA21 and RSI in range."""
        state = _make_state(
            ema9=Decimal("97300"),   # EMA9 > EMA21
            ema21=Decimal("97000"),
            rsi=Decimal("50"),       # In range [30, 65]
            atr_pct=Decimal("0.15"), # Above min 0.1%
        )
        result = strategy.evaluate(state)

        assert result.has_setup is True
        assert result.setup is not None
        assert result.setup.direction == Direction.LONG
        assert result.setup.setup_type == SetupType.MOMENTUM

    def test_long_rsi_at_boundaries(self, strategy):
        """LONG valid at RSI boundaries (30 and 65)."""
        # RSI at lower bound
        state = _make_state(rsi=Decimal("30"))
        result = strategy.evaluate(state)
        assert result.has_setup is True

        # RSI at upper bound
        state = _make_state(rsi=Decimal("65"))
        result = strategy.evaluate(state)
        assert result.has_setup is True

    def test_long_stop_price(self, strategy):
        """LONG stop = entry * (1 - 0.8%)."""
        state = _make_state(close=Decimal("100000"))
        result = strategy.evaluate(state)

        assert result.setup is not None
        expected_stop = Decimal("100000") * (Decimal("1") - Decimal("0.008"))
        assert result.setup.stop_price == expected_stop
        assert result.setup.stop_distance_pct == Decimal("0.8")


# =============================================================================
# SHORT Signal Tests
# =============================================================================

class TestShortSignal:
    """Test SHORT entry conditions."""

    def test_short_signal_basic(self, strategy):
        """SHORT when EMA9 < EMA21 and RSI in range."""
        state = _make_state(
            ema9=Decimal("96800"),   # EMA9 < EMA21
            ema21=Decimal("97200"),
            rsi=Decimal("55"),       # In range [40, 70]
        )
        result = strategy.evaluate(state)

        assert result.has_setup is True
        assert result.setup is not None
        assert result.setup.direction == Direction.SHORT

    def test_short_stop_price(self, strategy):
        """SHORT stop = entry * (1 + 0.8%)."""
        state = _make_state(
            close=Decimal("100000"),
            ema9=Decimal("99800"),
            ema21=Decimal("100200"),
            rsi=Decimal("55"),
        )
        result = strategy.evaluate(state)

        assert result.setup is not None
        expected_stop = Decimal("100000") * (Decimal("1") + Decimal("0.008"))
        assert result.setup.stop_price == expected_stop

    def test_short_disabled(self, long_only_strategy):
        """SHORT blocked when allow_short=False."""
        state = _make_state(
            ema9=Decimal("96800"),
            ema21=Decimal("97200"),
            rsi=Decimal("55"),
        )
        result = long_only_strategy.evaluate(state)

        assert result.has_setup is False
        assert "disabled" in result.reason.lower()


# =============================================================================
# RSI Filter Tests
# =============================================================================

class TestRsiFilter:
    """Test RSI filtering logic."""

    def test_long_rejected_overbought(self, strategy):
        """LONG rejected when RSI > 65 (overbought)."""
        state = _make_state(rsi=Decimal("70"))
        result = strategy.evaluate(state)

        assert result.has_setup is False
        assert "RSI" in result.reason

    def test_long_rejected_oversold(self, strategy):
        """LONG rejected when RSI < 30."""
        state = _make_state(rsi=Decimal("25"))
        result = strategy.evaluate(state)

        assert result.has_setup is False
        assert "RSI" in result.reason

    def test_short_rejected_oversold(self, strategy):
        """SHORT rejected when RSI < 40."""
        state = _make_state(
            ema9=Decimal("96800"),
            ema21=Decimal("97200"),
            rsi=Decimal("25"),
        )
        result = strategy.evaluate(state)

        assert result.has_setup is False
        assert "RSI" in result.reason

    def test_short_rejected_overbought(self, strategy):
        """SHORT rejected when RSI > 70."""
        state = _make_state(
            ema9=Decimal("96800"),
            ema21=Decimal("97200"),
            rsi=Decimal("75"),
        )
        result = strategy.evaluate(state)

        assert result.has_setup is False
        assert "RSI" in result.reason


# =============================================================================
# Volatility Filter Tests
# =============================================================================

class TestVolatilityFilter:
    """Test ATR% minimum filter."""

    def test_rejected_low_atr(self, strategy):
        """Rejected when ATR% below minimum."""
        state = _make_state(atr_pct=Decimal("0.05"))  # Below 0.1%
        result = strategy.evaluate(state)

        assert result.has_setup is False
        assert "ATR" in result.reason

    def test_accepted_boundary_atr(self, strategy):
        """Accepted at exactly minimum ATR%."""
        state = _make_state(atr_pct=Decimal("0.10"))
        result = strategy.evaluate(state)

        assert result.has_setup is True


# =============================================================================
# EMA Availability Tests
# =============================================================================

class TestEmaAvailability:
    """Test handling of missing EMA9/EMA21."""

    def test_no_ema9(self, strategy):
        """Rejected when EMA9 is None."""
        state = _make_state(ema9=None)
        result = strategy.evaluate(state)

        assert result.has_setup is False
        assert "not available" in result.reason.lower()

    def test_no_ema21(self, strategy):
        """Rejected when EMA21 is None."""
        state = _make_state(ema21=None)
        result = strategy.evaluate(state)

        assert result.has_setup is False
        assert "not available" in result.reason.lower()

    def test_equal_emas_flat(self, strategy):
        """FLAT when EMA9 == EMA21 (no crossover)."""
        state = _make_state(
            ema9=Decimal("97000"),
            ema21=Decimal("97000"),
        )
        result = strategy.evaluate(state)

        assert result.has_setup is False
        assert "crossover" in result.reason.lower()


# =============================================================================
# Quality Score Tests
# =============================================================================

class TestQualityScore:
    """Test setup quality calculation."""

    def test_quality_range(self, strategy):
        """Quality score must be between 0 and 1."""
        state = _make_state()
        result = strategy.evaluate(state)

        assert result.setup is not None
        assert Decimal("0") <= result.setup.setup_quality <= Decimal("1")

    def test_higher_quality_with_separation(self, strategy):
        """Wider EMA separation should give higher quality."""
        # Small separation
        state_small = _make_state(
            ema9=Decimal("97010"),
            ema21=Decimal("97000"),
        )
        result_small = strategy.evaluate(state_small)

        # Large separation
        state_large = _make_state(
            ema9=Decimal("97500"),
            ema21=Decimal("97000"),
        )
        result_large = strategy.evaluate(state_large)

        assert result_small.setup is not None
        assert result_large.setup is not None
        assert result_large.setup.setup_quality >= result_small.setup.setup_quality

    def test_quality_with_good_rsi(self, strategy):
        """RSI in optimal range should contribute to quality."""
        # Optimal RSI for LONG (40-55)
        state_optimal = _make_state(rsi=Decimal("48"))
        result_optimal = strategy.evaluate(state_optimal)

        # Edge RSI
        state_edge = _make_state(rsi=Decimal("63"))
        result_edge = strategy.evaluate(state_edge)

        assert result_optimal.setup is not None
        assert result_edge.setup is not None
        assert result_optimal.setup.setup_quality >= result_edge.setup.setup_quality


# =============================================================================
# Can Trade Tests
# =============================================================================

class TestCanTrade:
    """Test regime handling."""

    def test_trades_only_in_trend_regime(self, strategy):
        """Strategy should only trade in TREND regime."""
        assert strategy.can_trade(_make_state(regime=Regime.TREND)) is True
        assert strategy.can_trade(_make_state(regime=Regime.RANGE)) is False
        assert strategy.can_trade(_make_state(regime=Regime.CHAOS)) is False

    def test_name(self, strategy):
        """Strategy name should be trend_momentum."""
        assert strategy.name == "trend_momentum"
