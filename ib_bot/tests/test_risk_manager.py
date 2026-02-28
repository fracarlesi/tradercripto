"""Tests for IB Bot Risk Manager."""

import pytest
from decimal import Decimal
from datetime import datetime, timezone

from ib_bot.core.enums import Direction, SetupType
from ib_bot.core.models import ORBRange, ORBSetup
from ib_bot.services.risk_manager import RiskManager
from ib_bot.config.loader import RiskConfig


@pytest.fixture
def risk_mgr(risk_config: RiskConfig) -> RiskManager:
    return RiskManager(risk_config)


class TestTickBasedSizing:
    """Test tick-based position sizing."""

    def test_size_mes_trade(
        self, risk_mgr: RiskManager, sample_setup: ORBSetup
    ) -> None:
        """MES: risk_ticks=24, tick_value=$1.25.
        Risk/contract = 24 * $1.25 = $30.
        Contracts = floor($500 / $30) = 16, capped at 2.
        """
        intent = risk_mgr.size_trade(sample_setup)
        assert intent is not None
        assert intent.contracts == 2  # Capped at max_contracts_per_trade
        assert intent.risk_usd == Decimal("60")  # 2 * 24 * $1.25

    def test_size_es_trade(
        self, risk_mgr: RiskManager, sample_or_range: ORBRange
    ) -> None:
        """ES: risk_ticks=20, tick_value=$12.50.
        Risk/contract = 20 * $12.50 = $250.
        Contracts = floor($500 / $250) = 2.
        """
        setup = ORBSetup(
            symbol="ES",
            direction=Direction.LONG,
            setup_type=SetupType.ORB_LONG,
            entry_price=Decimal("5020.00"),
            stop_price=Decimal("5015.00"),
            target_price=Decimal("5027.50"),
            risk_ticks=20,
            reward_ticks=30,
            or_range=sample_or_range,
            confidence=Decimal("0.7"),
        )
        intent = risk_mgr.size_trade(setup)
        assert intent is not None
        assert intent.contracts == 2
        assert intent.risk_usd == Decimal("500")  # 2 * 20 * $12.50

    def test_risk_too_high_for_one_contract(
        self, risk_config: RiskConfig, sample_or_range: ORBRange
    ) -> None:
        """When risk per contract > max_risk, reject."""
        config = RiskConfig(
            max_risk_per_trade_usd=Decimal("100"),  # Very low
            max_daily_loss_usd=Decimal("1000"),
        )
        rm = RiskManager(config)
        setup = ORBSetup(
            symbol="ES",
            direction=Direction.LONG,
            setup_type=SetupType.ORB_LONG,
            entry_price=Decimal("5020.00"),
            stop_price=Decimal("5010.00"),
            target_price=Decimal("5035.00"),
            risk_ticks=40,  # 40 * $12.50 = $500 > $100
            reward_ticks=60,
            or_range=sample_or_range,
            confidence=Decimal("0.7"),
        )
        intent = rm.size_trade(setup)
        assert intent is None


class TestDailyLimits:
    """Test daily trade and loss limits."""

    def test_daily_trade_limit(
        self, risk_mgr: RiskManager, sample_setup: ORBSetup
    ) -> None:
        """After max_trades_per_day, reject new trades."""
        risk_mgr.record_fill(Decimal("10"), is_stop=False)
        risk_mgr.record_fill(Decimal("10"), is_stop=False)
        # 2 trades done, limit is 2
        intent = risk_mgr.size_trade(sample_setup)
        assert intent is None

    def test_daily_loss_limit(
        self, risk_mgr: RiskManager, sample_setup: ORBSetup
    ) -> None:
        """After daily loss exceeds limit, reject."""
        risk_mgr.record_fill(Decimal("-600"), is_stop=True)
        risk_mgr.record_fill(Decimal("-500"), is_stop=True)
        # Total loss = $1100 > $1000 limit
        intent = risk_mgr.size_trade(sample_setup)
        assert intent is None

    def test_consecutive_stops_halt(
        self, risk_mgr: RiskManager, sample_setup: ORBSetup
    ) -> None:
        """After N consecutive stops, halt."""
        risk_mgr.record_fill(Decimal("-30"), is_stop=True)
        risk_mgr.record_fill(Decimal("-30"), is_stop=True)
        # 2 consecutive stops = halt
        intent = risk_mgr.size_trade(sample_setup)
        assert intent is None

    def test_tp_resets_consecutive_stops(
        self, risk_mgr: RiskManager, sample_setup: ORBSetup
    ) -> None:
        """TP resets consecutive stop counter."""
        risk_mgr.record_fill(Decimal("-30"), is_stop=True)
        risk_mgr.record_fill(Decimal("50"), is_stop=False)  # TP resets
        # Only 1 trade left, but consecutive_stops = 0
        assert risk_mgr._consecutive_stops == 0

    def test_daily_reset(
        self, risk_mgr: RiskManager, sample_setup: ORBSetup
    ) -> None:
        """Daily reset clears all counters."""
        risk_mgr.record_fill(Decimal("-500"), is_stop=True)
        risk_mgr.reset_daily()
        assert risk_mgr._daily_trade_count == 0
        assert risk_mgr._daily_loss_usd == Decimal("0")
        assert risk_mgr._consecutive_stops == 0
        assert risk_mgr.is_trading_allowed


class TestTradingAllowed:
    """Test is_trading_allowed property."""

    def test_initially_allowed(self, risk_mgr: RiskManager) -> None:
        assert risk_mgr.is_trading_allowed

    def test_not_allowed_after_max_trades(
        self, risk_mgr: RiskManager
    ) -> None:
        risk_mgr.record_fill(Decimal("10"), is_stop=False)
        risk_mgr.record_fill(Decimal("10"), is_stop=False)
        assert not risk_mgr.is_trading_allowed
