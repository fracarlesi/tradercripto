"""Tests for stock/ETF position sizing in RiskManager."""

import pytest
from decimal import Decimal

from ib_bot.config.loader import RiskConfig
from ib_bot.services.risk_manager import RiskManager


@pytest.fixture
def risk_mgr() -> RiskManager:
    """RiskManager with $500 max risk per trade."""
    config = RiskConfig(
        max_risk_per_trade_usd=Decimal("500"),
        max_daily_loss_usd=Decimal("1000"),
        max_contracts_per_trade=2,
        max_trades_per_day=4,
        consecutive_stops_halt=3,
    )
    return RiskManager(config)


class TestSizeStockTrade:
    """Test size_stock_trade with various scenarios."""

    def test_basic_sizing(self, risk_mgr: RiskManager) -> None:
        """$500 risk, $5 risk per share -> 100 shares."""
        shares = risk_mgr.size_stock_trade(
            entry_price=Decimal("150.00"),
            stop_price=Decimal("145.00"),
        )
        assert shares == 100

    def test_fractional_rounds_down(self, risk_mgr: RiskManager) -> None:
        """$500 risk, $3 risk per share -> floor(166.67) = 166 shares."""
        shares = risk_mgr.size_stock_trade(
            entry_price=Decimal("100.00"),
            stop_price=Decimal("97.00"),
        )
        assert shares == 166

    def test_short_direction(self, risk_mgr: RiskManager) -> None:
        """Short trade: stop above entry. Risk = abs(entry - stop)."""
        shares = risk_mgr.size_stock_trade(
            entry_price=Decimal("200.00"),
            stop_price=Decimal("205.00"),
        )
        # $500 / $5 = 100 shares
        assert shares == 100

    def test_tiny_risk_per_share(self, risk_mgr: RiskManager) -> None:
        """Very tight stop -> many shares, capped at max_shares."""
        shares = risk_mgr.size_stock_trade(
            entry_price=Decimal("50.00"),
            stop_price=Decimal("49.90"),
            max_shares=500,
        )
        # $500 / $0.10 = 5000 -> capped at 500
        assert shares == 500

    def test_custom_max_shares(self, risk_mgr: RiskManager) -> None:
        """Custom max_shares cap."""
        shares = risk_mgr.size_stock_trade(
            entry_price=Decimal("50.00"),
            stop_price=Decimal("49.90"),
            max_shares=200,
        )
        assert shares == 200

    def test_custom_risk_budget(self, risk_mgr: RiskManager) -> None:
        """Override max_risk_usd for this trade."""
        shares = risk_mgr.size_stock_trade(
            entry_price=Decimal("100.00"),
            stop_price=Decimal("95.00"),
            max_risk_usd=Decimal("250"),
        )
        # $250 / $5 = 50 shares
        assert shares == 50

    def test_risk_too_high_for_one_share(self, risk_mgr: RiskManager) -> None:
        """When risk per share exceeds budget, returns 0."""
        shares = risk_mgr.size_stock_trade(
            entry_price=Decimal("1000.00"),
            stop_price=Decimal("400.00"),
        )
        # $500 / $600 = 0.83 -> floor = 0
        assert shares == 0

    def test_zero_risk_returns_zero(self, risk_mgr: RiskManager) -> None:
        """Entry == stop -> zero risk per share -> returns 0."""
        shares = risk_mgr.size_stock_trade(
            entry_price=Decimal("100.00"),
            stop_price=Decimal("100.00"),
        )
        assert shares == 0

    def test_minimum_one_share(self, risk_mgr: RiskManager) -> None:
        """Risk barely allows 1 share -> returns 1."""
        shares = risk_mgr.size_stock_trade(
            entry_price=Decimal("100.00"),
            stop_price=Decimal("50.00"),
            max_risk_usd=Decimal("500"),
        )
        # $500 / $50 = 10 shares
        assert shares == 10

    def test_penny_stock_tight_stop(self, risk_mgr: RiskManager) -> None:
        """Low-price stock with tight stop, capped correctly."""
        shares = risk_mgr.size_stock_trade(
            entry_price=Decimal("5.00"),
            stop_price=Decimal("4.95"),
            max_shares=1000,
        )
        # $500 / $0.05 = 10000 -> capped at 1000
        assert shares == 1000

    def test_expensive_stock_wide_stop(self, risk_mgr: RiskManager) -> None:
        """Expensive stock with wide stop."""
        shares = risk_mgr.size_stock_trade(
            entry_price=Decimal("500.00"),
            stop_price=Decimal("490.00"),
        )
        # $500 / $10 = 50 shares
        assert shares == 50

    def test_defaults_to_config_risk(self) -> None:
        """When max_risk_usd is None, uses config value."""
        config = RiskConfig(
            max_risk_per_trade_usd=Decimal("100"),
            max_daily_loss_usd=Decimal("500"),
        )
        rm = RiskManager(config)
        shares = rm.size_stock_trade(
            entry_price=Decimal("50.00"),
            stop_price=Decimal("48.00"),
        )
        # $100 / $2 = 50 shares
        assert shares == 50
