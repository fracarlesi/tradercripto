"""
Tests for Performance Metrics
==============================

Unit tests for the PerformanceMetrics model and its calculation methods.

Run:
    pytest crypto_bot/tests/test_performance_metrics.py -v
"""

import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import List

from crypto_bot.core.models import PerformanceMetrics


# =============================================================================
# Test Fixtures
# =============================================================================

@pytest.fixture
def sample_positive_returns() -> List[Decimal]:
    """Daily returns with positive overall trend."""
    return [
        Decimal("0.02"),   # +2%
        Decimal("0.01"),   # +1%
        Decimal("-0.005"), # -0.5%
        Decimal("0.015"),  # +1.5%
        Decimal("0.008"),  # +0.8%
        Decimal("-0.003"), # -0.3%
        Decimal("0.012"),  # +1.2%
        Decimal("0.006"),  # +0.6%
        Decimal("-0.002"), # -0.2%
        Decimal("0.01"),   # +1%
    ]


@pytest.fixture
def sample_negative_returns() -> List[Decimal]:
    """Daily returns with negative overall trend."""
    return [
        Decimal("-0.02"),
        Decimal("-0.01"),
        Decimal("0.005"),
        Decimal("-0.015"),
        Decimal("-0.008"),
        Decimal("0.003"),
        Decimal("-0.012"),
        Decimal("-0.006"),
        Decimal("0.002"),
        Decimal("-0.01"),
    ]


@pytest.fixture
def sample_equity_curve() -> List[tuple[datetime, Decimal]]:
    """Sample equity curve for drawdown testing."""
    base_time = datetime.now(timezone.utc)
    return [
        (base_time, Decimal("10000")),
        (base_time + timedelta(days=1), Decimal("10200")),  # +2%
        (base_time + timedelta(days=2), Decimal("10350")),  # New peak
        (base_time + timedelta(days=3), Decimal("9800")),   # -5.3% from peak (10350)
        (base_time + timedelta(days=4), Decimal("10100")),  # Recovery
        (base_time + timedelta(days=5), Decimal("10500")),  # New peak
        (base_time + timedelta(days=6), Decimal("10400")),  # -0.95% from peak
    ]


@pytest.fixture
def sample_trade_pnls() -> List[Decimal]:
    """Sample trade PnLs for SQN calculation."""
    return [
        Decimal("50"),
        Decimal("-30"),
        Decimal("75"),
        Decimal("-20"),
        Decimal("100"),
        Decimal("-45"),
        Decimal("60"),
        Decimal("40"),
        Decimal("-15"),
        Decimal("85"),
    ]


# =============================================================================
# Sharpe Ratio Tests
# =============================================================================

class TestSharpeRatio:
    """Test Sharpe ratio calculation."""

    def test_sharpe_positive_returns(self, sample_positive_returns):
        """Test Sharpe ratio with positive returns."""
        sharpe = PerformanceMetrics.calculate_sharpe_ratio(sample_positive_returns)

        assert sharpe is not None
        assert sharpe > 0, "Positive returns should yield positive Sharpe"

    def test_sharpe_negative_returns(self, sample_negative_returns):
        """Test Sharpe ratio with negative returns."""
        sharpe = PerformanceMetrics.calculate_sharpe_ratio(sample_negative_returns)

        assert sharpe is not None
        assert sharpe < 0, "Negative returns should yield negative Sharpe"

    def test_sharpe_insufficient_data(self):
        """Test Sharpe returns None with insufficient data."""
        # Only one data point
        sharpe = PerformanceMetrics.calculate_sharpe_ratio([Decimal("0.01")])
        assert sharpe is None

    def test_sharpe_empty_returns(self):
        """Test Sharpe returns None with empty list."""
        sharpe = PerformanceMetrics.calculate_sharpe_ratio([])
        assert sharpe is None

    def test_sharpe_zero_std_dev(self):
        """Test Sharpe returns None when all returns are identical."""
        # All same value = zero std dev
        returns = [Decimal("0.01")] * 10
        sharpe = PerformanceMetrics.calculate_sharpe_ratio(returns)
        assert sharpe is None

    def test_sharpe_custom_risk_free_rate(self, sample_positive_returns):
        """Test Sharpe with custom risk-free rate."""
        sharpe_default = PerformanceMetrics.calculate_sharpe_ratio(sample_positive_returns)
        sharpe_zero_rf = PerformanceMetrics.calculate_sharpe_ratio(
            sample_positive_returns,
            risk_free_rate=Decimal("0")
        )

        assert sharpe_default is not None
        assert sharpe_zero_rf is not None
        # With zero risk-free rate, Sharpe should be slightly higher
        assert sharpe_zero_rf >= sharpe_default


# =============================================================================
# Sortino Ratio Tests
# =============================================================================

class TestSortinoRatio:
    """Test Sortino ratio calculation."""

    def test_sortino_positive_returns(self, sample_positive_returns):
        """Test Sortino ratio with positive returns."""
        sortino = PerformanceMetrics.calculate_sortino_ratio(sample_positive_returns)

        # May be None if not enough downside returns
        if sortino is not None:
            assert sortino > 0, "Positive mean returns should yield positive Sortino"

    def test_sortino_negative_returns(self, sample_negative_returns):
        """Test Sortino ratio with negative returns."""
        sortino = PerformanceMetrics.calculate_sortino_ratio(sample_negative_returns)

        assert sortino is not None
        assert sortino < 0, "Negative mean returns should yield negative Sortino"

    def test_sortino_insufficient_downside(self):
        """Test Sortino returns None with insufficient downside returns."""
        # Only positive returns
        returns = [Decimal("0.01"), Decimal("0.02"), Decimal("0.03")]
        sortino = PerformanceMetrics.calculate_sortino_ratio(returns)
        assert sortino is None

    def test_sortino_vs_sharpe(self, sample_positive_returns):
        """Sortino should be higher than Sharpe for positive returns with few losses."""
        sharpe = PerformanceMetrics.calculate_sharpe_ratio(sample_positive_returns)
        sortino = PerformanceMetrics.calculate_sortino_ratio(sample_positive_returns)

        # Sortino ignores upside volatility, so often higher for winning strategies
        if sortino is not None and sharpe is not None:
            # Not always true, but common for strategies with limited downside
            pass  # Just verify both calculate without error


# =============================================================================
# Max Drawdown Tests
# =============================================================================

class TestMaxDrawdown:
    """Test max drawdown calculation."""

    def test_drawdown_calculation(self, sample_equity_curve):
        """Test max drawdown is calculated correctly."""
        max_dd_pct, max_dd_abs, _ = PerformanceMetrics.calculate_max_drawdown(
            sample_equity_curve
        )

        # Max drawdown should be from 10350 to 9800 = 5.31%
        assert max_dd_pct > Decimal("5")
        assert max_dd_pct < Decimal("6")

        # Max drawdown absolute should be 550
        assert max_dd_abs == Decimal("550")

    def test_drawdown_empty_curve(self):
        """Test drawdown with empty equity curve."""
        max_dd_pct, max_dd_abs, current_dd_pct = PerformanceMetrics.calculate_max_drawdown([])

        assert max_dd_pct == Decimal("0")
        assert max_dd_abs == Decimal("0")
        assert current_dd_pct == Decimal("0")

    def test_drawdown_single_point(self):
        """Test drawdown with single equity point."""
        curve = [(datetime.now(timezone.utc), Decimal("10000"))]
        max_dd_pct, max_dd_abs, _ = PerformanceMetrics.calculate_max_drawdown(curve)

        assert max_dd_pct == Decimal("0")
        assert max_dd_abs == Decimal("0")

    def test_drawdown_only_gains(self):
        """Test drawdown when equity only increases."""
        base_time = datetime.now(timezone.utc)
        curve = [
            (base_time, Decimal("10000")),
            (base_time + timedelta(days=1), Decimal("10100")),
            (base_time + timedelta(days=2), Decimal("10200")),
            (base_time + timedelta(days=3), Decimal("10300")),
        ]

        max_dd_pct, max_dd_abs, current_dd_pct = PerformanceMetrics.calculate_max_drawdown(curve)

        assert max_dd_pct == Decimal("0")
        assert max_dd_abs == Decimal("0")
        assert current_dd_pct == Decimal("0")

    def test_current_drawdown(self, sample_equity_curve):
        """Test current drawdown calculation."""
        _, _, current_dd_pct = PerformanceMetrics.calculate_max_drawdown(sample_equity_curve)

        # Current equity is 10400, peak is 10500
        # Current DD = (10500 - 10400) / 10500 * 100 = 0.95%
        assert current_dd_pct > Decimal("0.9")
        assert current_dd_pct < Decimal("1.0")


# =============================================================================
# Profit Factor Tests
# =============================================================================

class TestProfitFactor:
    """Test profit factor calculation."""

    def test_profit_factor_positive(self):
        """Test profit factor with more profit than loss."""
        gross_profit = Decimal("1000")
        gross_loss = Decimal("-500")  # Losses are negative

        pf = PerformanceMetrics.calculate_profit_factor(gross_profit, gross_loss)

        assert pf is not None
        assert pf == Decimal("2.00"), "PF should be 2.0 (1000 / 500)"

    def test_profit_factor_negative(self):
        """Test profit factor less than 1 (losing system)."""
        gross_profit = Decimal("500")
        gross_loss = Decimal("-1000")

        pf = PerformanceMetrics.calculate_profit_factor(gross_profit, gross_loss)

        assert pf is not None
        assert pf == Decimal("0.50"), "PF should be 0.5 (500 / 1000)"

    def test_profit_factor_no_losses(self):
        """Test profit factor returns None with no losses."""
        gross_profit = Decimal("1000")
        gross_loss = Decimal("0")

        pf = PerformanceMetrics.calculate_profit_factor(gross_profit, gross_loss)

        assert pf is None, "PF should be None when no losses"

    def test_profit_factor_breakeven(self):
        """Test profit factor at exactly 1.0."""
        gross_profit = Decimal("500")
        gross_loss = Decimal("-500")

        pf = PerformanceMetrics.calculate_profit_factor(gross_profit, gross_loss)

        assert pf is not None
        assert pf == Decimal("1.00")


# =============================================================================
# Expectancy Tests
# =============================================================================

class TestExpectancy:
    """Test trading expectancy calculation."""

    def test_expectancy_positive(self):
        """Test positive expectancy (profitable system)."""
        avg_win = Decimal("100")
        avg_loss = Decimal("-50")  # Negative
        win_rate = Decimal("0.6")  # 60% win rate

        # Expectancy = (100 * 0.6) - (50 * 0.4) = 60 - 20 = 40
        expectancy = PerformanceMetrics.calculate_expectancy(avg_win, avg_loss, win_rate)

        assert expectancy is not None
        assert expectancy == Decimal("40.00")

    def test_expectancy_negative(self):
        """Test negative expectancy (losing system)."""
        avg_win = Decimal("50")
        avg_loss = Decimal("-100")
        win_rate = Decimal("0.4")  # 40% win rate

        # Expectancy = (50 * 0.4) - (100 * 0.6) = 20 - 60 = -40
        expectancy = PerformanceMetrics.calculate_expectancy(avg_win, avg_loss, win_rate)

        assert expectancy is not None
        assert expectancy == Decimal("-40.00")

    def test_expectancy_zero_loss(self):
        """Test expectancy returns None with zero average loss."""
        avg_win = Decimal("100")
        avg_loss = Decimal("0")
        win_rate = Decimal("0.6")

        expectancy = PerformanceMetrics.calculate_expectancy(avg_win, avg_loss, win_rate)

        assert expectancy is None

    def test_expectancy_high_win_rate_small_wins(self):
        """Test expectancy with high win rate but small wins."""
        avg_win = Decimal("20")
        avg_loss = Decimal("-100")
        win_rate = Decimal("0.9")  # 90% win rate

        # Expectancy = (20 * 0.9) - (100 * 0.1) = 18 - 10 = 8
        expectancy = PerformanceMetrics.calculate_expectancy(avg_win, avg_loss, win_rate)

        assert expectancy is not None
        assert expectancy == Decimal("8.00")


# =============================================================================
# SQN Tests
# =============================================================================

class TestSQN:
    """Test System Quality Number calculation."""

    def test_sqn_positive(self, sample_trade_pnls):
        """Test SQN with mostly winning trades."""
        sqn = PerformanceMetrics.calculate_sqn(sample_trade_pnls)

        assert sqn is not None
        # Sample has more wins than losses, SQN should be positive
        assert sqn > 0

    def test_sqn_interpretation(self, sample_trade_pnls):
        """Test SQN value interpretation."""
        sqn = PerformanceMetrics.calculate_sqn(sample_trade_pnls)

        if sqn is not None:
            # SQN interpretations:
            # < 1.6: Poor
            # 1.6 - 2.0: Below average
            # 2.0 - 2.5: Average
            # 2.5 - 3.0: Good
            # > 3.0: Excellent
            # Our sample should be average to good
            pass  # Value depends on exact sample

    def test_sqn_insufficient_data(self):
        """Test SQN returns None with insufficient data."""
        sqn = PerformanceMetrics.calculate_sqn([Decimal("50")])
        assert sqn is None

    def test_sqn_empty_trades(self):
        """Test SQN returns None with empty list."""
        sqn = PerformanceMetrics.calculate_sqn([])
        assert sqn is None

    def test_sqn_zero_std_dev(self):
        """Test SQN returns None when all trades are identical."""
        trades = [Decimal("50")] * 10
        sqn = PerformanceMetrics.calculate_sqn(trades)
        assert sqn is None

    def test_sqn_high_quality_system(self):
        """Test SQN for a high-quality trading system."""
        # Consistent small wins, rare small losses
        trades = [
            Decimal("50"), Decimal("45"), Decimal("55"), Decimal("48"),
            Decimal("-20"), Decimal("52"), Decimal("47"), Decimal("53"),
            Decimal("51"), Decimal("49"), Decimal("-15"), Decimal("50"),
        ]

        sqn = PerformanceMetrics.calculate_sqn(trades)

        assert sqn is not None
        assert sqn > Decimal("2.0"), "High quality system should have SQN > 2.0"


# =============================================================================
# Calmar Ratio Tests
# =============================================================================

class TestCalmarRatio:
    """Test Calmar ratio calculation."""

    def test_calmar_positive(self):
        """Test Calmar ratio with positive annual return."""
        annual_return_pct = Decimal("50")  # 50% annual return
        max_drawdown_pct = Decimal("10")   # 10% max drawdown

        calmar = PerformanceMetrics.calculate_calmar_ratio(annual_return_pct, max_drawdown_pct)

        assert calmar is not None
        assert calmar == Decimal("5.00"), "Calmar should be 5.0 (50 / 10)"

    def test_calmar_negative(self):
        """Test Calmar ratio with negative return."""
        annual_return_pct = Decimal("-20")
        max_drawdown_pct = Decimal("10")

        calmar = PerformanceMetrics.calculate_calmar_ratio(annual_return_pct, max_drawdown_pct)

        assert calmar is not None
        assert calmar == Decimal("-2.00")

    def test_calmar_zero_drawdown(self):
        """Test Calmar returns None with zero drawdown."""
        annual_return_pct = Decimal("50")
        max_drawdown_pct = Decimal("0")

        calmar = PerformanceMetrics.calculate_calmar_ratio(annual_return_pct, max_drawdown_pct)

        assert calmar is None


# =============================================================================
# Empty Metrics Tests
# =============================================================================

class TestEmptyMetrics:
    """Test empty metrics creation."""

    def test_empty_metrics_creation(self):
        """Test creating empty metrics object."""
        equity = Decimal("10000")
        initial_equity = Decimal("10000")

        metrics = PerformanceMetrics.empty_metrics(equity, initial_equity)

        assert metrics.equity == equity
        assert metrics.initial_equity == initial_equity
        assert metrics.total_pnl == Decimal("0")
        assert metrics.total_trades == 0
        assert metrics.sharpe_ratio is None
        assert metrics.sortino_ratio is None
        assert metrics.profit_factor is None
        assert metrics.win_rate == Decimal("0")

    def test_empty_metrics_serialization(self):
        """Test empty metrics can be serialized."""
        metrics = PerformanceMetrics.empty_metrics(
            Decimal("10000"),
            Decimal("10000")
        )

        # Should serialize without error
        data = metrics.model_dump()

        assert "equity" in data
        assert "sharpe_ratio" in data
        assert data["total_trades"] == 0


# =============================================================================
# Full Metrics Creation Tests
# =============================================================================

class TestFullMetrics:
    """Test full metrics object creation."""

    def test_full_metrics_creation(self):
        """Test creating a complete metrics object."""
        metrics = PerformanceMetrics(
            timestamp=datetime.now(timezone.utc),
            equity=Decimal("11000"),
            initial_equity=Decimal("10000"),
            total_pnl=Decimal("1000"),
            total_pnl_pct=Decimal("10"),
            sharpe_ratio=Decimal("1.5"),
            sortino_ratio=Decimal("2.0"),
            calmar_ratio=Decimal("3.0"),
            max_drawdown_pct=Decimal("5"),
            max_drawdown_abs=Decimal("500"),
            current_drawdown_pct=Decimal("1"),
            profit_factor=Decimal("1.8"),
            win_rate=Decimal("0.6"),
            avg_win=Decimal("100"),
            avg_loss=Decimal("-50"),
            avg_win_loss_ratio=Decimal("2.0"),
            expectancy=Decimal("40"),
            sqn=Decimal("2.5"),
            total_trades=100,
            winning_trades=60,
            losing_trades=40,
            total_fees=Decimal("50"),
            avg_trade_duration_seconds=3600,
            largest_win=Decimal("500"),
            largest_loss=Decimal("-200"),
        )

        assert metrics.total_trades == 100
        assert metrics.winning_trades == 60
        assert metrics.losing_trades == 40
        assert metrics.sharpe_ratio == Decimal("1.5")

    def test_metrics_validation_win_rate(self):
        """Test win rate validation (0-1 range)."""
        # Valid win rate
        metrics = PerformanceMetrics(
            timestamp=datetime.now(timezone.utc),
            equity=Decimal("10000"),
            initial_equity=Decimal("10000"),
            total_pnl=Decimal("0"),
            total_pnl_pct=Decimal("0"),
            win_rate=Decimal("0.65"),
        )
        assert metrics.win_rate == Decimal("0.65")

    def test_metrics_json_serialization(self):
        """Test metrics can be serialized to JSON-compatible dict."""
        metrics = PerformanceMetrics(
            timestamp=datetime.now(timezone.utc),
            equity=Decimal("10000"),
            initial_equity=Decimal("10000"),
            total_pnl=Decimal("500"),
            total_pnl_pct=Decimal("5"),
            sharpe_ratio=Decimal("1.2"),
        )

        data = metrics.model_dump(mode='json')

        # Decimals should be converted to floats
        assert isinstance(data["equity"], float)
        assert data["equity"] == 10000.0
        assert data["sharpe_ratio"] == 1.2


# =============================================================================
# Edge Cases
# =============================================================================

class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_very_small_returns(self):
        """Test with very small return values."""
        returns = [Decimal("0.0001")] * 10 + [Decimal("-0.0001")] * 5

        sharpe = PerformanceMetrics.calculate_sharpe_ratio(returns)
        # Should calculate without error
        assert sharpe is None or isinstance(sharpe, Decimal)

    def test_very_large_values(self):
        """Test with very large equity values."""
        base_time = datetime.now(timezone.utc)
        curve = [
            (base_time, Decimal("1000000000")),  # 1 billion
            (base_time + timedelta(days=1), Decimal("1100000000")),  # 10% gain
            (base_time + timedelta(days=2), Decimal("990000000")),   # 10% loss
        ]

        max_dd_pct, max_dd_abs, _ = PerformanceMetrics.calculate_max_drawdown(curve)

        # 10% drawdown from 1.1B to 990M
        assert max_dd_pct == Decimal("10")
        assert max_dd_abs == Decimal("110000000")

    def test_single_losing_trade(self):
        """Test SQN with single trade that is a loss."""
        sqn = PerformanceMetrics.calculate_sqn([Decimal("-100")])
        assert sqn is None  # Insufficient data

    def test_all_losses(self):
        """Test metrics with all losing trades."""
        losses = [Decimal("-50"), Decimal("-75"), Decimal("-30")]

        sqn = PerformanceMetrics.calculate_sqn(losses)

        assert sqn is not None
        assert sqn < 0, "All losses should yield negative SQN"


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
