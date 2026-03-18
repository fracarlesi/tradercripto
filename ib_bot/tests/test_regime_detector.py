"""Tests for regime detector."""

import pytest
from decimal import Decimal

from ib_bot.services.regime_detector import MarketRegime, RegimeDetector


@pytest.fixture
def detector() -> RegimeDetector:
    return RegimeDetector(
        atr_lookback=5,
        price_window=5,
        vwap_cross_threshold=2,
        trend_threshold=4,
    )


def test_default_regime(detector: RegimeDetector) -> None:
    """Default regime is MEAN_REVERT."""
    assert detector.regime == MarketRegime.MEAN_REVERT


def test_reset(detector: RegimeDetector) -> None:
    """Reset clears close/VWAP history."""
    detector.update(Decimal("100"), Decimal("100"), Decimal("5"))
    detector.reset()
    assert detector.regime == MarketRegime.MEAN_REVERT


def test_trend_up(detector: RegimeDetector) -> None:
    """Consistent price above VWAP classifies as TREND_UP."""
    vwap = Decimal("100")
    atr = Decimal("5")
    # Feed enough ATR history first
    for _ in range(5):
        detector.update(Decimal("105"), vwap, atr)
    # Now with price_window=5, all closes above VWAP -> TREND_UP
    regime = detector.regime
    assert regime == MarketRegime.TREND_UP


def test_trend_down(detector: RegimeDetector) -> None:
    """Consistent price below VWAP classifies as TREND_DOWN."""
    vwap = Decimal("100")
    atr = Decimal("5")
    for _ in range(5):
        detector.update(Decimal("95"), vwap, atr)
    assert detector.regime == MarketRegime.TREND_DOWN


def test_high_volatility(detector: RegimeDetector) -> None:
    """ATR spike classifies as HIGH_VOLATILITY."""
    # Build average with normal ATR
    for _ in range(5):
        detector.update(Decimal("100"), Decimal("100"), Decimal("5"))
    # Now spike ATR to 2x average
    regime = detector.update(Decimal("100"), Decimal("100"), Decimal("10"))
    assert regime == MarketRegime.HIGH_VOLATILITY


def test_low_volatility(detector: RegimeDetector) -> None:
    """Very low ATR classifies as LOW_VOLATILITY."""
    for _ in range(5):
        detector.update(Decimal("100"), Decimal("100"), Decimal("5"))
    regime = detector.update(Decimal("100"), Decimal("100"), Decimal("2"))
    assert regime == MarketRegime.LOW_VOLATILITY


def test_choppy(detector: RegimeDetector) -> None:
    """Frequent VWAP crosses classify as CHOPPY."""
    vwap = Decimal("100")
    atr = Decimal("5")
    # Build ATR history
    for _ in range(5):
        detector.update(Decimal("100"), vwap, atr)
    detector.reset()
    # Alternate above/below VWAP to create crosses (>2 in 5 bars)
    prices = [Decimal("105"), Decimal("95"), Decimal("105"), Decimal("95"), Decimal("105")]
    for p in prices:
        detector.update(p, vwap, atr)
    assert detector.regime == MarketRegime.CHOPPY
