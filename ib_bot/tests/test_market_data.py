"""Tests for Market Data Service (VWAP and ATR calculators)."""

import pytest
from decimal import Decimal

from ib_bot.services.market_data import VWAPCalculator, ATRCalculator


class TestVWAPCalculator:
    """Test incremental VWAP calculation."""

    def test_single_bar(self) -> None:
        vwap = VWAPCalculator()
        result = vwap.update(
            high=Decimal("100"), low=Decimal("98"), close=Decimal("99"),
            volume=Decimal("1000"),
        )
        # typical = (100+98+99)/3 = 99
        # vwap = (99*1000)/1000 = 99
        assert result == Decimal("99")

    def test_two_bars(self) -> None:
        vwap = VWAPCalculator()
        vwap.update(
            high=Decimal("100"), low=Decimal("98"), close=Decimal("99"),
            volume=Decimal("1000"),
        )
        result = vwap.update(
            high=Decimal("102"), low=Decimal("100"), close=Decimal("101"),
            volume=Decimal("2000"),
        )
        # Bar1: tp=99, vol=1000 → tp*vol=99000
        # Bar2: tp=101, vol=2000 → tp*vol=202000
        # VWAP = (99000+202000)/(1000+2000) = 301000/3000 = 100.333...
        expected = Decimal("301000") / Decimal("3000")
        assert abs(result - expected) < Decimal("0.001")

    def test_reset(self) -> None:
        vwap = VWAPCalculator()
        vwap.update(
            high=Decimal("100"), low=Decimal("98"), close=Decimal("99"),
            volume=Decimal("1000"),
        )
        vwap.reset()
        assert vwap.vwap == Decimal("0")

    def test_zero_volume(self) -> None:
        vwap = VWAPCalculator()
        result = vwap.update(
            high=Decimal("100"), low=Decimal("98"), close=Decimal("99"),
            volume=Decimal("0"),
        )
        # With zero volume, returns close
        assert result == Decimal("99")


class TestATRCalculator:
    """Test ATR(14) calculation."""

    def test_single_bar(self) -> None:
        atr = ATRCalculator(period=14)
        result = atr.update(
            high=Decimal("100"), low=Decimal("98"), close=Decimal("99"),
        )
        # First bar: TR = high - low = 2
        assert result == Decimal("2")

    def test_two_bars_with_gap(self) -> None:
        atr = ATRCalculator(period=14)
        atr.update(
            high=Decimal("100"), low=Decimal("98"), close=Decimal("99"),
        )
        result = atr.update(
            high=Decimal("103"), low=Decimal("100"), close=Decimal("102"),
        )
        # Bar2: TR = max(103-100, |103-99|, |100-99|) = max(3, 4, 1) = 4
        # ATR = (2 + 4) / 2 = 3
        assert result == Decimal("3")

    def test_reset(self) -> None:
        atr = ATRCalculator(period=14)
        atr.update(
            high=Decimal("100"), low=Decimal("98"), close=Decimal("99"),
        )
        atr.reset()
        assert atr.atr == Decimal("0")

    def test_wilder_smoothing_after_period(self) -> None:
        """After 14 bars, ATR uses Wilder's smoothing."""
        atr = ATRCalculator(period=3)  # Short period for test
        # 3 bars to complete initial period
        atr.update(high=Decimal("102"), low=Decimal("100"), close=Decimal("101"))  # TR=2
        atr.update(high=Decimal("104"), low=Decimal("101"), close=Decimal("103"))  # TR=3
        atr.update(high=Decimal("105"), low=Decimal("102"), close=Decimal("104"))  # TR=3
        # Initial ATR = (2+3+3)/3 = 2.667

        # 4th bar: Wilder smoothing kicks in
        result = atr.update(
            high=Decimal("106"), low=Decimal("103"), close=Decimal("105"),
        )
        # TR = max(3, |106-104|, |103-104|) = max(3, 2, 1) = 3
        # ATR = (prev_atr * 2 + 3) / 3 = (2.667*2 + 3) / 3 = 8.334/3 = 2.778
        assert result > Decimal("2.7")
        assert result < Decimal("2.9")
