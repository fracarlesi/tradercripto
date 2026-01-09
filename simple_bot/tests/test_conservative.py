"""
Tests for HLQuantBot Conservative System
==========================================

Unit and integration tests for the conservative trading system components.

Run:
    pytest simple_bot/tests/test_conservative.py -v
"""

import asyncio
import pytest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from simple_bot.core.enums import Topic
from simple_bot.core.models import (
    MarketState,
    Setup,
    TradeIntent,
    Regime,
    Direction,
    SetupType,
    KillSwitchStatus,
)


# =============================================================================
# Test Fixtures
# =============================================================================

@pytest.fixture
def market_state_trend():
    """Create a market state in TREND regime."""
    return MarketState(
        symbol="BTC",
        timeframe="4h",
        timestamp=datetime.now(timezone.utc),
        open=Decimal("95000"),
        high=Decimal("96000"),
        low=Decimal("94500"),
        close=Decimal("95800"),
        volume=Decimal("1000"),
        atr=Decimal("500"),
        atr_pct=Decimal("0.52"),
        adx=Decimal("35"),
        rsi=Decimal("55"),
        ema50=Decimal("94000"),
        ema200=Decimal("92000"),
        ema200_slope=Decimal("0.002"),
        regime=Regime.TREND,
        trend_direction=Direction.LONG,
    )


@pytest.fixture
def market_state_range():
    """Create a market state in RANGE regime."""
    return MarketState(
        symbol="ETH",
        timeframe="4h",
        timestamp=datetime.now(timezone.utc),
        open=Decimal("3400"),
        high=Decimal("3450"),
        low=Decimal("3380"),
        close=Decimal("3420"),
        volume=Decimal("500"),
        atr=Decimal("30"),
        atr_pct=Decimal("0.88"),
        adx=Decimal("18"),
        rsi=Decimal("45"),
        ema50=Decimal("3420"),
        ema200=Decimal("3400"),
        ema200_slope=Decimal("0.0005"),
        regime=Regime.RANGE,
        trend_direction=Direction.FLAT,
        choppiness=Decimal("65"),
    )


@pytest.fixture
def market_state_chaos():
    """Create a market state in CHAOS regime."""
    return MarketState(
        symbol="BTC",
        timeframe="4h",
        timestamp=datetime.now(timezone.utc),
        open=Decimal("95000"),
        high=Decimal("97000"),
        low=Decimal("93000"),
        close=Decimal("94500"),
        volume=Decimal("2000"),
        atr=Decimal("800"),
        atr_pct=Decimal("0.85"),
        adx=Decimal("22"),
        rsi=Decimal("50"),
        ema50=Decimal("95000"),
        ema200=Decimal("94000"),
        ema200_slope=Decimal("0.0008"),
        regime=Regime.CHAOS,
        trend_direction=Direction.FLAT,
    )


@pytest.fixture
def valid_setup():
    """Create a valid trading setup."""
    return Setup(
        id="setup_test_001",
        symbol="BTC",
        timestamp=datetime.now(timezone.utc),
        setup_type=SetupType.TREND_BREAKOUT,
        direction=Direction.LONG,
        regime=Regime.TREND,
        entry_price=Decimal("96000"),
        stop_price=Decimal("94500"),
        stop_distance_pct=Decimal("1.56"),
        atr=Decimal("500"),
        adx=Decimal("35"),
        rsi=Decimal("55"),
        setup_quality=Decimal("0.75"),
        confidence=Decimal("0.8"),
    )


# =============================================================================
# Model Tests
# =============================================================================

class TestModels:
    """Test Pydantic models."""

    def test_market_state_creation(self, market_state_trend):
        """Test MarketState model can be created."""
        assert market_state_trend.symbol == "BTC"
        assert market_state_trend.regime == Regime.TREND
        assert market_state_trend.trend_direction == Direction.LONG
        assert market_state_trend.adx == Decimal("35")

    def test_market_state_serialization(self, market_state_trend):
        """Test MarketState serialization."""
        data = market_state_trend.model_dump()
        assert data["symbol"] == "BTC"
        assert data["regime"] == Regime.TREND

        # Should be able to recreate from dict
        recreated = MarketState(**data)
        assert recreated.symbol == market_state_trend.symbol
        assert recreated.regime == market_state_trend.regime

    def test_setup_creation(self, valid_setup):
        """Test Setup model can be created."""
        assert valid_setup.symbol == "BTC"
        assert valid_setup.direction == Direction.LONG
        assert valid_setup.stop_distance_pct > 0

    def test_trade_intent_creation(self):
        """Test TradeIntent model."""
        intent = TradeIntent(
            id="intent_001",
            setup_id="setup_001",
            symbol="BTC",
            timestamp=datetime.now(timezone.utc),
            direction=Direction.LONG,
            setup_type=SetupType.TREND_BREAKOUT,
            entry_price=Decimal("96000"),
            position_size=Decimal("0.1"),
            notional_value=Decimal("9600"),
            stop_price=Decimal("94500"),
            risk_amount=Decimal("150"),
            risk_pct=Decimal("0.5"),
        )
        assert intent.position_size == Decimal("0.1")
        assert intent.risk_amount == Decimal("150")


# =============================================================================
# Strategy Tests
# =============================================================================

class TestTrendFollowStrategy:
    """Test TrendFollowStrategy."""

    def test_import(self):
        """Test strategy can be imported."""
        from simple_bot.strategies import TrendFollowStrategy
        assert TrendFollowStrategy is not None

    def test_strategy_initialization(self):
        """Test strategy initialization."""
        from simple_bot.strategies import TrendFollowStrategy

        strategy = TrendFollowStrategy()
        assert strategy.name == "trend_follow"
        assert strategy.required_regime == Regime.TREND

    def test_strategy_with_config(self):
        """Test strategy with custom config."""
        from simple_bot.strategies import TrendFollowStrategy

        config = {
            "breakout_period": 15,
            "stop_atr_mult": 3.0,
            "min_adx": 30,
        }
        strategy = TrendFollowStrategy(config=config)
        assert strategy.breakout_period == 15
        assert strategy.stop_atr_mult == 3.0
        assert strategy.min_adx == 30.0

    def test_can_trade_in_trend(self, market_state_trend):
        """Test strategy can trade in TREND regime."""
        from simple_bot.strategies import TrendFollowStrategy

        strategy = TrendFollowStrategy()
        assert strategy.can_trade(market_state_trend) == True

    def test_cannot_trade_in_range(self, market_state_range):
        """Test strategy cannot trade in RANGE regime."""
        from simple_bot.strategies import TrendFollowStrategy

        strategy = TrendFollowStrategy()
        assert strategy.can_trade(market_state_range) == False

    def test_cannot_trade_in_chaos(self, market_state_chaos):
        """Test strategy cannot trade in CHAOS regime."""
        from simple_bot.strategies import TrendFollowStrategy

        strategy = TrendFollowStrategy()
        assert strategy.can_trade(market_state_chaos) == False

    def test_evaluate_generates_setup(self, market_state_trend):
        """Test strategy generates setup in valid conditions."""
        from simple_bot.strategies import TrendFollowStrategy

        strategy = TrendFollowStrategy(config={"min_adx": 25})
        result = strategy.evaluate(market_state_trend)

        assert result.has_setup == True
        assert result.setup is not None
        assert result.setup.direction == Direction.LONG
        assert result.setup.symbol == "BTC"


class TestMeanReversionStrategy:
    """Test MeanReversionStrategy."""

    def test_import(self):
        """Test strategy can be imported."""
        from simple_bot.strategies import MeanReversionStrategy
        assert MeanReversionStrategy is not None

    def test_strategy_initialization(self):
        """Test strategy initialization."""
        from simple_bot.strategies import MeanReversionStrategy

        strategy = MeanReversionStrategy()
        assert strategy.name == "mean_reversion"
        assert strategy.required_regime == Regime.RANGE

    def test_can_trade_in_range(self, market_state_range):
        """Test strategy can trade in RANGE regime."""
        from simple_bot.strategies import MeanReversionStrategy

        strategy = MeanReversionStrategy()
        assert strategy.can_trade(market_state_range) == True

    def test_cannot_trade_in_trend(self, market_state_trend):
        """Test strategy cannot trade in TREND regime."""
        from simple_bot.strategies import MeanReversionStrategy

        strategy = MeanReversionStrategy()
        assert strategy.can_trade(market_state_trend) == False


# =============================================================================
# Service Configuration Tests
# =============================================================================

class TestServiceConfigs:
    """Test service configuration dataclasses."""

    def test_market_state_config(self):
        """Test MarketStateConfig defaults."""
        from simple_bot.services import MarketStateConfig

        config = MarketStateConfig()
        assert config.assets == ["BTC", "ETH"]
        assert config.timeframe == "4h"
        assert config.trend_adx_min == 25.0

    def test_risk_config(self):
        """Test RiskConfig defaults."""
        from simple_bot.services import RiskConfig

        config = RiskConfig()
        assert config.per_trade_pct == 0.5
        assert config.max_positions == 2
        assert config.leverage == 1.0

    def test_kill_switch_config(self):
        """Test KillSwitchConfig defaults."""
        from simple_bot.services import KillSwitchConfig

        config = KillSwitchConfig()
        assert config.daily_loss_pct == 2.0
        assert config.weekly_loss_pct == 5.0
        assert config.max_drawdown_pct == 15.0

    def test_llm_veto_config(self):
        """Test LLMVetoConfig defaults."""
        from simple_bot.services import LLMVetoConfig

        config = LLMVetoConfig()
        assert config.enabled == True
        assert config.max_calls_per_day == 6
        assert config.fallback_on_error == "allow"


# =============================================================================
# Risk Manager Tests
# =============================================================================

class TestRiskManager:
    """Test RiskManagerService."""

    def test_import(self):
        """Test service can be imported."""
        from simple_bot.services import RiskManagerService, create_risk_manager
        assert RiskManagerService is not None
        assert create_risk_manager is not None

    def test_position_size_calculation(self):
        """Test risk-based position sizing formula."""
        from simple_bot.services.risk_manager import RiskManagerService, RiskConfig

        config = RiskConfig(per_trade_pct=0.5)
        service = RiskManagerService(config=config)

        # Set equity
        service._current_equity = Decimal("10000")

        # Create a setup
        setup = Setup(
            id="test_setup",
            symbol="BTC",
            timestamp=datetime.now(timezone.utc),
            setup_type=SetupType.TREND_BREAKOUT,
            direction=Direction.LONG,
            regime=Regime.TREND,
            entry_price=Decimal("96000"),
            stop_price=Decimal("94560"),  # 1.5% stop distance
            stop_distance_pct=Decimal("1.5"),
            atr=Decimal("500"),
            adx=Decimal("35"),
            rsi=Decimal("55"),
        )

        # Calculate risk
        params = service._calculate_risk_params(setup)

        # Risk should be 0.5% of 10000 = 50
        assert params.risk_amount == Decimal("50")
        assert params.size_approved == True


# =============================================================================
# Kill Switch Tests
# =============================================================================

class TestKillSwitch:
    """Test KillSwitchService."""

    def test_import(self):
        """Test service can be imported."""
        from simple_bot.services import KillSwitchService, create_kill_switch
        assert KillSwitchService is not None
        assert create_kill_switch is not None

    def test_initial_state(self):
        """Test kill switch starts in OK state."""
        from simple_bot.services import KillSwitchService

        service = KillSwitchService()
        assert service.get_status() == KillSwitchStatus.OK
        assert service.is_trading_allowed() == True

    def test_drawdown_calculation(self):
        """Test drawdown percentage calculation."""
        from simple_bot.services import KillSwitchService

        service = KillSwitchService()

        # Set peak and current equity
        service._peak_equity = Decimal("10000")
        service._current_equity = Decimal("9000")

        drawdown = service.get_current_drawdown()
        assert drawdown == 10.0  # 10% drawdown


# =============================================================================
# LLM Veto Tests
# =============================================================================

class TestLLMVeto:
    """Test LLMVetoService."""

    def test_import(self):
        """Test service can be imported."""
        from simple_bot.services import LLMVetoService, create_llm_veto
        assert LLMVetoService is not None
        assert create_llm_veto is not None

    def test_fallback_decision(self, valid_setup):
        """Test fallback decision creation."""
        from simple_bot.services.llm_veto import LLMVetoService

        service = LLMVetoService()

        decision = service._create_fallback_decision(valid_setup, "Test reason")
        assert decision.decision == "ALLOW"  # Default fallback is allow
        assert "Fallback" in decision.reason

    def test_chaos_regime_denied(self, valid_setup):
        """Test CHAOS regime is always denied."""
        from simple_bot.services.llm_veto import LLMVetoService

        # Modify setup to CHAOS regime
        valid_setup.regime = Regime.CHAOS

        service = LLMVetoService()

        # Should create a deny decision
        decision = service._create_fallback_decision(
            valid_setup,
            "CHAOS regime",
            allow=False
        )
        assert decision.decision == "DENY"


# =============================================================================
# Configuration Loading Tests
# =============================================================================

class TestConfigurationLoading:
    """Test configuration loading from YAML."""

    def test_conservative_config_from_yaml(self, tmp_path):
        """Test loading ConservativeConfig from YAML."""
        from simple_bot.main_conservative import ConservativeConfig

        # Create test config
        config_content = """
universe:
  assets:
    - symbol: "BTC"
      enabled: true
    - symbol: "ETH"
      enabled: true
    - symbol: "SOL"
      enabled: false

timeframes:
  primary: "4h"
  bars_to_fetch: 200

risk:
  per_trade_pct: 0.5
  max_positions: 2

kill_switch:
  daily_loss_pct: 2.0
  weekly_loss_pct: 5.0
  max_drawdown_pct: 15.0

regime:
  trend_adx_min: 25

llm:
  enabled: true
  max_calls_per_day: 6

strategies:
  trend_follow:
    enabled: true
  mean_reversion:
    enabled: false

environment: "testnet"
dry_run: false
"""
        config_file = tmp_path / "test_trading.yaml"
        config_file.write_text(config_content)

        # Clear ENVIRONMENT env var to test YAML takes precedence when env var is not set
        import os
        old_env = os.environ.pop("ENVIRONMENT", None)
        try:
            config = ConservativeConfig.from_yaml(str(config_file))

            assert config.assets == ["BTC", "ETH"]  # SOL disabled
            assert config.per_trade_pct == 0.5
            assert config.max_drawdown_pct == 15.0
            assert config.trend_follow_enabled == True
            assert config.mean_reversion_enabled == False
            assert config.testnet == True
        finally:
            if old_env is not None:
                os.environ["ENVIRONMENT"] = old_env


# =============================================================================
# Integration Tests
# =============================================================================

class TestIntegration:
    """Integration tests for conservative system."""

    @pytest.mark.asyncio
    async def test_message_bus_topic_enum(self):
        """Test new topics are available."""
        from simple_bot.core.enums import Topic

        # Check new topics exist
        assert hasattr(Topic, "MARKET_STATE")
        assert hasattr(Topic, "REGIME")
        assert hasattr(Topic, "SETUPS")
        assert hasattr(Topic, "TRADE_INTENT")
        assert hasattr(Topic, "RISK_ALERTS")

    @pytest.mark.asyncio
    async def test_services_can_be_created(self):
        """Test all services can be instantiated."""
        from simple_bot.services import (
            create_market_state_service,
            create_risk_manager,
            create_kill_switch,
            create_llm_veto,
        )

        # Create without bus/db (dry run)
        market_state = create_market_state_service(testnet=True)
        risk_manager = create_risk_manager()
        kill_switch = create_kill_switch()
        llm_veto = create_llm_veto()

        assert market_state is not None
        assert risk_manager is not None
        assert kill_switch is not None
        assert llm_veto is not None


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
