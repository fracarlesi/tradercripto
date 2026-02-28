"""Tests for Kill Switch Service."""

import pytest
from decimal import Decimal

from ib_bot.core.enums import KillSwitchStatus
from ib_bot.config.loader import RiskConfig
from ib_bot.services.kill_switch import KillSwitchService


@pytest.fixture
def kill_switch(risk_config: RiskConfig) -> KillSwitchService:
    return KillSwitchService(config=risk_config)


class TestConsecutiveStops:
    """Test consecutive stop halt logic."""

    def test_initially_active(self, kill_switch: KillSwitchService) -> None:
        assert kill_switch.is_trading_allowed
        assert kill_switch.status == KillSwitchStatus.ACTIVE

    def test_one_stop_still_active(self, kill_switch: KillSwitchService) -> None:
        kill_switch.record_trade_result(Decimal("-30"), is_stop=True)
        assert kill_switch.is_trading_allowed

    def test_two_stops_halted(self, kill_switch: KillSwitchService) -> None:
        kill_switch.record_trade_result(Decimal("-30"), is_stop=True)
        kill_switch.record_trade_result(Decimal("-30"), is_stop=True)
        assert not kill_switch.is_trading_allowed
        assert kill_switch.status == KillSwitchStatus.HALTED

    def test_tp_resets_counter(self, kill_switch: KillSwitchService) -> None:
        kill_switch.record_trade_result(Decimal("-30"), is_stop=True)
        kill_switch.record_trade_result(Decimal("50"), is_stop=False)  # TP
        assert kill_switch.is_trading_allowed
        # After reset, one more stop should be fine
        kill_switch.record_trade_result(Decimal("-30"), is_stop=True)
        assert kill_switch.is_trading_allowed


class TestDailyLoss:
    """Test daily loss halt logic."""

    def test_within_limit(self, kill_switch: KillSwitchService) -> None:
        kill_switch.record_trade_result(Decimal("-500"), is_stop=True)
        assert kill_switch.is_trading_allowed

    def test_exceeds_limit(self, kill_switch: KillSwitchService) -> None:
        kill_switch.record_trade_result(Decimal("-600"), is_stop=True)
        # Reset consecutive since it's only 1 stop, but check loss
        kill_switch._consecutive_stops = 0  # Manually reset for test isolation
        kill_switch.record_trade_result(Decimal("-500"), is_stop=True)
        # Total = $1100 > $1000
        assert not kill_switch.is_trading_allowed


class TestDailyReset:
    """Test daily reset."""

    def test_reset_clears_halt(self, kill_switch: KillSwitchService) -> None:
        kill_switch.record_trade_result(Decimal("-30"), is_stop=True)
        kill_switch.record_trade_result(Decimal("-30"), is_stop=True)
        assert not kill_switch.is_trading_allowed
        kill_switch.reset_daily()
        assert kill_switch.is_trading_allowed
        assert kill_switch.status == KillSwitchStatus.ACTIVE

    def test_reset_clears_loss(self, kill_switch: KillSwitchService) -> None:
        kill_switch.record_trade_result(Decimal("-800"), is_stop=True)
        kill_switch.reset_daily()
        assert kill_switch.metrics["daily_loss_usd"] == 0.0
