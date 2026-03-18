"""Tests for Vigilant Asset Allocation (VAA-G4) ETF Rotation Strategy."""

import json
import pytest
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from ib_bot.strategies.etf_rotation import (
    ETFRotationStrategy,
    MomentumScore,
    RotationAction,
    RotationResult,
    compute_momentum,
    extract_monthly_prices,
    is_last_trading_day_of_month,
    load_state,
    pick_etf,
    save_state,
    _month_offset_date,
    _STATE_FILE,
)


# =============================================================================
# Momentum Score Tests
# =============================================================================


class TestComputeMomentum:
    """Tests for the VAA momentum score formula."""

    def test_positive_momentum(self) -> None:
        """Rising prices produce positive momentum."""
        prices = {
            0: 100.0,  # current
            1: 98.0,   # 1mo ago
            3: 95.0,   # 3mo ago
            6: 90.0,   # 6mo ago
            12: 80.0,  # 12mo ago
        }
        result = compute_momentum("SPY", prices)
        assert result.symbol == "SPY"
        assert result.score > 0
        assert result.p0 == 100.0
        assert result.p12 == 80.0

    def test_negative_momentum(self) -> None:
        """Falling prices produce negative momentum."""
        prices = {
            0: 80.0,   # current
            1: 85.0,   # 1mo ago
            3: 90.0,   # 3mo ago
            6: 95.0,   # 6mo ago
            12: 100.0, # 12mo ago
        }
        result = compute_momentum("EEM", prices)
        assert result.symbol == "EEM"
        assert result.score < 0

    def test_flat_prices_zero_momentum(self) -> None:
        """Identical prices produce zero momentum.

        Score = 12*(1) + 4*(1) + 2*(1) + (1) - 19 = 19 - 19 = 0
        """
        prices = {0: 100.0, 1: 100.0, 3: 100.0, 6: 100.0, 12: 100.0}
        result = compute_momentum("AGG", prices)
        assert abs(result.score) < 1e-10

    def test_formula_correctness(self) -> None:
        """Verify the exact formula: 12*(p0/p1) + 4*(p0/p3) + 2*(p0/p6) + (p0/p12) - 19."""
        prices = {0: 110.0, 1: 100.0, 3: 105.0, 6: 95.0, 12: 90.0}
        result = compute_momentum("SPY", prices)

        expected = (
            12 * (110.0 / 100.0)
            + 4 * (110.0 / 105.0)
            + 2 * (110.0 / 95.0)
            + (110.0 / 90.0)
            - 19
        )
        assert abs(result.score - expected) < 1e-10

    def test_missing_price_raises(self) -> None:
        """Missing required month raises ValueError."""
        prices = {0: 100.0, 1: 98.0, 3: 95.0, 6: 90.0}  # missing 12
        with pytest.raises(ValueError, match="missing price for 12"):
            compute_momentum("SPY", prices)

    def test_zero_price_raises(self) -> None:
        """Zero price raises ValueError."""
        prices = {0: 100.0, 1: 0.0, 3: 95.0, 6: 90.0, 12: 80.0}
        with pytest.raises(ValueError, match="price for 1 months ago is <= 0"):
            compute_momentum("SPY", prices)


# =============================================================================
# Pick ETF Tests
# =============================================================================


class TestPickETF:
    """Tests for the VAA-G4 decision rule."""

    def _make_score(self, symbol: str, score: float) -> MomentumScore:
        return MomentumScore(
            symbol=symbol, score=score,
            p0=100.0, p1=99.0, p3=98.0, p6=97.0, p12=96.0,
        )

    def test_all_offensive_positive_picks_best_offensive(self) -> None:
        """When all offensive have positive momentum, pick the best one."""
        offensive = [
            self._make_score("SPY", 0.5),
            self._make_score("EFA", 0.3),
            self._make_score("EEM", 0.8),  # best
            self._make_score("AGG", 0.1),
        ]
        defensive = [
            self._make_score("BIL", 0.2),
            self._make_score("IEF", 0.4),
            self._make_score("LQD", 0.3),
        ]
        symbol, is_off, all_pos = pick_etf(offensive, defensive)
        assert symbol == "EEM"
        assert is_off is True
        assert all_pos is True

    def test_one_offensive_negative_picks_best_defensive(self) -> None:
        """When any offensive has negative momentum, pick the best defensive."""
        offensive = [
            self._make_score("SPY", 0.5),
            self._make_score("EFA", -0.2),  # negative!
            self._make_score("EEM", 0.8),
            self._make_score("AGG", 0.1),
        ]
        defensive = [
            self._make_score("BIL", 0.2),
            self._make_score("IEF", 0.6),  # best
            self._make_score("LQD", 0.3),
        ]
        symbol, is_off, all_pos = pick_etf(offensive, defensive)
        assert symbol == "IEF"
        assert is_off is False
        assert all_pos is False

    def test_all_offensive_negative_picks_defensive(self) -> None:
        """When all offensive are negative, pick best defensive."""
        offensive = [
            self._make_score("SPY", -0.5),
            self._make_score("EFA", -0.2),
            self._make_score("EEM", -0.8),
            self._make_score("AGG", -0.1),
        ]
        defensive = [
            self._make_score("BIL", 0.05),  # best
            self._make_score("IEF", -0.1),
            self._make_score("LQD", 0.02),
        ]
        symbol, is_off, all_pos = pick_etf(offensive, defensive)
        assert symbol == "BIL"
        assert is_off is False

    def test_zero_momentum_counts_as_not_positive(self) -> None:
        """Exactly zero momentum is not positive (strict > 0)."""
        offensive = [
            self._make_score("SPY", 0.5),
            self._make_score("EFA", 0.0),  # zero!
            self._make_score("EEM", 0.8),
            self._make_score("AGG", 0.1),
        ]
        defensive = [
            self._make_score("BIL", 0.2),
            self._make_score("IEF", 0.4),
            self._make_score("LQD", 0.3),
        ]
        symbol, is_off, all_pos = pick_etf(offensive, defensive)
        # EFA has zero momentum, so not all positive -> go defensive
        assert is_off is False
        assert all_pos is False
        assert symbol == "IEF"


# =============================================================================
# Last Trading Day Tests
# =============================================================================


class TestIsLastTradingDay:
    """Tests for last trading day of month detection."""

    def test_last_weekday_friday(self) -> None:
        """March 28, 2026 is the last Friday of March -> last trading day."""
        # March 31 is Tuesday, so last weekday is March 31
        assert is_last_trading_day_of_month(date(2026, 3, 31)) is True

    def test_last_weekday_when_31st_is_sunday(self) -> None:
        """When month ends on Sunday, last trading day is Friday the 29th."""
        # May 2026: May 31 is a Sunday, so last weekday is May 29 (Friday)
        assert is_last_trading_day_of_month(date(2026, 5, 29)) is True
        assert is_last_trading_day_of_month(date(2026, 5, 28)) is False

    def test_last_weekday_when_31st_is_saturday(self) -> None:
        """When month ends on Saturday, last trading day is Friday the 30th."""
        # January 2026: Jan 31 is Saturday, last weekday is Jan 30 (Friday)
        assert is_last_trading_day_of_month(date(2026, 1, 30)) is True
        assert is_last_trading_day_of_month(date(2026, 1, 29)) is False

    def test_mid_month_is_not_last_day(self) -> None:
        """A random mid-month day is not the last trading day."""
        assert is_last_trading_day_of_month(date(2026, 3, 15)) is False

    def test_february_leap_year(self) -> None:
        """February in a leap year: Feb 28, 2028 is Monday (last weekday of Feb)."""
        # 2028 is a leap year, Feb 29 is Tuesday
        assert is_last_trading_day_of_month(date(2028, 2, 29)) is True

    def test_december_to_january_boundary(self) -> None:
        """December 31, 2025 is a Wednesday -> last trading day."""
        assert is_last_trading_day_of_month(date(2025, 12, 31)) is True


# =============================================================================
# Month Offset Date Tests
# =============================================================================


class TestMonthOffsetDate:
    """Tests for _month_offset_date helper."""

    def test_one_month_back(self) -> None:
        result = _month_offset_date(date(2026, 3, 15), 1)
        assert result == date(2026, 2, 15)

    def test_twelve_months_back(self) -> None:
        result = _month_offset_date(date(2026, 3, 15), 12)
        assert result == date(2025, 3, 15)

    def test_clamp_short_month(self) -> None:
        """March 31 - 1 month = Feb 28 (clamped)."""
        result = _month_offset_date(date(2026, 3, 31), 1)
        assert result == date(2026, 2, 28)

    def test_cross_year_boundary(self) -> None:
        result = _month_offset_date(date(2026, 1, 15), 3)
        assert result == date(2025, 10, 15)


# =============================================================================
# Extract Monthly Prices Tests
# =============================================================================


class TestExtractMonthlyPrices:
    """Tests for extracting prices at monthly intervals from daily bars."""

    def _make_bars(self, start: date, days: int, base_price: float = 100.0) -> list[dict]:
        """Generate synthetic daily bars (weekdays only)."""
        bars = []
        current = start
        price = base_price
        for _ in range(days):
            if current.weekday() < 5:  # Skip weekends
                bars.append({"date": current, "close": price})
                price += 0.1  # Slowly rising
            current += timedelta(days=1)
        return bars

    def test_basic_extraction(self) -> None:
        """Extract prices from 13 months of daily bars."""
        # Create bars from 2025-01-01 to 2026-03-31
        bars = self._make_bars(date(2025, 1, 1), 500)
        ref = date(2026, 3, 15)

        prices = extract_monthly_prices(bars, ref)

        assert 0 in prices
        assert 1 in prices
        assert 3 in prices
        assert 6 in prices
        assert 12 in prices

        # Price at month 0 should be higher than month 12 (rising bars)
        assert prices[0] > prices[12]

    def test_empty_bars_raises(self) -> None:
        """Empty bar list raises ValueError."""
        with pytest.raises(ValueError, match="No daily bars"):
            extract_monthly_prices([], date(2026, 3, 15))

    def test_insufficient_history_raises(self) -> None:
        """Not enough bars to reach 12 months ago raises ValueError."""
        bars = self._make_bars(date(2026, 1, 1), 60)  # Only ~2 months
        with pytest.raises(ValueError, match="No trading day found"):
            extract_monthly_prices(bars, date(2026, 3, 15))

    def test_datetime_bars_handled(self) -> None:
        """Bars with datetime objects (not date) are handled correctly."""
        bars = self._make_bars(date(2025, 1, 1), 500)
        # Convert dates to datetimes
        for b in bars:
            b["date"] = datetime.combine(b["date"], datetime.min.time())

        prices = extract_monthly_prices(bars, date(2026, 3, 15))
        assert len(prices) == 5


# =============================================================================
# Strategy Integration Tests
# =============================================================================


class TestETFRotationStrategy:
    """Integration tests for the strategy class."""

    def _make_bars_for_symbols(
        self,
        symbols: list[str],
        ref: date,
        offensive_positive: bool = True,
    ) -> dict[str, list[dict]]:
        """Create synthetic bar data for all symbols."""
        bars: dict[str, list[dict]] = {}
        start = date(ref.year - 1, ref.month, 1) - timedelta(days=60)

        for i, symbol in enumerate(symbols):
            symbol_bars = []
            current = start
            if offensive_positive:
                # Rising prices -> positive momentum
                base_price = 100.0 + i * 10
            else:
                # Falling prices -> negative momentum for first symbol
                base_price = 200.0 - i * 30 if i == 0 else 100.0 + i * 10

            price = base_price
            for _ in range(500):
                if current.weekday() < 5:
                    if not offensive_positive and i == 0:
                        price -= 0.05  # Falling
                    else:
                        price += 0.1   # Rising
                    symbol_bars.append({"date": current, "close": max(price, 1.0)})
                current += timedelta(days=1)

            bars[symbol] = symbol_bars

        return bars

    def test_evaluate_all_offensive_positive(self) -> None:
        """When all offensive have positive momentum, picks best offensive."""
        strategy = ETFRotationStrategy(
            offensive=["SPY", "EFA", "EEM", "AGG"],
            defensive=["BIL", "IEF", "LQD"],
        )

        all_symbols = strategy.all_symbols
        ref = date(2026, 3, 31)
        bars = self._make_bars_for_symbols(all_symbols, ref, offensive_positive=True)

        # Mock load_state to return no current holding
        with patch("ib_bot.strategies.etf_rotation.load_state", return_value={
            "current_holding": None,
            "last_rebalance_date": None,
            "history": [],
        }):
            result = strategy.evaluate(bars, ref)

        assert result.all_offensive_positive is True
        assert result.is_offensive is True
        assert result.recommended_etf in ["SPY", "EFA", "EEM", "AGG"]
        assert result.action == RotationAction.BUY

    def test_evaluate_some_offensive_negative(self) -> None:
        """When any offensive has negative momentum, picks defensive."""
        strategy = ETFRotationStrategy(
            offensive=["SPY", "EFA", "EEM", "AGG"],
            defensive=["BIL", "IEF", "LQD"],
        )

        all_symbols = strategy.all_symbols
        ref = date(2026, 3, 31)
        bars = self._make_bars_for_symbols(all_symbols, ref, offensive_positive=False)

        with patch("ib_bot.strategies.etf_rotation.load_state", return_value={
            "current_holding": None,
            "last_rebalance_date": None,
            "history": [],
        }):
            result = strategy.evaluate(bars, ref)

        assert result.all_offensive_positive is False
        assert result.is_offensive is False
        assert result.recommended_etf in ["BIL", "IEF", "LQD"]

    def test_evaluate_hold_same_etf(self) -> None:
        """When already holding the recommended ETF, action is HOLD."""
        strategy = ETFRotationStrategy(
            offensive=["SPY", "EFA", "EEM", "AGG"],
            defensive=["BIL", "IEF", "LQD"],
        )

        all_symbols = strategy.all_symbols
        ref = date(2026, 3, 31)
        bars = self._make_bars_for_symbols(all_symbols, ref, offensive_positive=True)

        # First evaluate to get the recommended ETF
        with patch("ib_bot.strategies.etf_rotation.load_state", return_value={
            "current_holding": None,
            "last_rebalance_date": None,
            "history": [],
        }):
            result1 = strategy.evaluate(bars, ref)

        # Now evaluate again with that ETF as current holding
        with patch("ib_bot.strategies.etf_rotation.load_state", return_value={
            "current_holding": result1.recommended_etf,
            "last_rebalance_date": "2026-02-27",
            "history": [],
        }):
            result2 = strategy.evaluate(bars, ref)

        assert result2.action == RotationAction.HOLD
        assert result2.recommended_etf == result1.recommended_etf

    def test_evaluate_switch_etf(self) -> None:
        """When holding a different ETF, action is SWITCH."""
        strategy = ETFRotationStrategy(
            offensive=["SPY", "EFA", "EEM", "AGG"],
            defensive=["BIL", "IEF", "LQD"],
        )

        all_symbols = strategy.all_symbols
        ref = date(2026, 3, 31)
        bars = self._make_bars_for_symbols(all_symbols, ref, offensive_positive=False)

        # Currently holding SPY, but negative momentum should switch to defensive
        with patch("ib_bot.strategies.etf_rotation.load_state", return_value={
            "current_holding": "SPY",
            "last_rebalance_date": "2026-02-27",
            "history": [],
        }):
            result = strategy.evaluate(bars, ref)

        assert result.action == RotationAction.SWITCH
        assert result.current_holding == "SPY"
        assert result.recommended_etf in ["BIL", "IEF", "LQD"]

    def test_should_rebalance(self) -> None:
        """should_rebalance delegates to is_last_trading_day_of_month."""
        strategy = ETFRotationStrategy(
            offensive=["SPY"], defensive=["BIL"],
        )
        # March 31, 2026 is a Tuesday (weekday) — last trading day
        assert strategy.should_rebalance(date(2026, 3, 31)) is True
        assert strategy.should_rebalance(date(2026, 3, 15)) is False

    def test_name(self) -> None:
        strategy = ETFRotationStrategy(
            offensive=["SPY"], defensive=["BIL"],
        )
        assert strategy.name == "vaa_g4"

    def test_all_symbols(self) -> None:
        strategy = ETFRotationStrategy(
            offensive=["SPY", "EFA"],
            defensive=["BIL", "IEF"],
        )
        assert strategy.all_symbols == ["SPY", "EFA", "BIL", "IEF"]


# =============================================================================
# State Persistence Tests
# =============================================================================


class TestStatePersistence:
    """Tests for load_state and save_state."""

    def test_load_state_no_file(self, tmp_path: Path) -> None:
        """Returns default state when file doesn't exist."""
        with patch("ib_bot.strategies.etf_rotation._STATE_FILE", tmp_path / "nope.json"):
            state = load_state()
        assert state["current_holding"] is None
        assert state["last_rebalance_date"] is None
        assert state["history"] == []

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        """State survives a save/load cycle."""
        state_file = tmp_path / "state.json"
        with patch("ib_bot.strategies.etf_rotation._STATE_FILE", state_file):
            save_state({
                "current_holding": "SPY",
                "last_rebalance_date": "2026-03-31",
                "history": [{"date": "2026-03-31", "action": "buy", "etf": "SPY"}],
            })
            loaded = load_state()

        assert loaded["current_holding"] == "SPY"
        assert loaded["last_rebalance_date"] == "2026-03-31"
        assert len(loaded["history"]) == 1

    def test_load_state_corrupted_file(self, tmp_path: Path) -> None:
        """Returns default state when file is corrupted."""
        state_file = tmp_path / "bad.json"
        state_file.write_text("not valid json{{{")
        with patch("ib_bot.strategies.etf_rotation._STATE_FILE", state_file):
            state = load_state()
        assert state["current_holding"] is None
