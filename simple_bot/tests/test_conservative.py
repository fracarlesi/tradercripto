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
    """Create a market state in TREND regime with SMA golden cross and bullish engulfing."""
    return MarketState(
        symbol="BTC",
        timeframe="4h",
        timestamp=datetime.now(timezone.utc),
        open=Decimal("95000"),
        high=Decimal("96000"),
        low=Decimal("94500"),
        close=Decimal("95800"),  # Price > SMA20 > SMA50 (golden cross)
        volume=Decimal("1000"),
        atr=Decimal("500"),
        atr_pct=Decimal("0.52"),
        adx=Decimal("35"),
        rsi=Decimal("55"),
        ema50=Decimal("94000"),
        ema200=Decimal("92000"),
        ema200_slope=Decimal("0.002"),
        sma20=Decimal("95000"),  # Price (95800) > SMA20 (95000)
        sma50=Decimal("94000"),  # SMA20 (95000) > SMA50 (94000)
        # Previous candle data - bearish candle that gets engulfed
        prev_open=Decimal("95500"),
        prev_high=Decimal("95600"),
        prev_low=Decimal("94800"),
        prev_close=Decimal("94900"),  # Bearish: close < open
        # Current candle is bullish and engulfs previous
        bullish_engulfing=True,  # Current opens at 95000, closes at 95800 - engulfs prev body
        bearish_engulfing=False,
        regime=Regime.TREND,
        trend_direction=Direction.LONG,
    )


@pytest.fixture
def market_state_range():
    """Create a market state in RANGE regime (no clear SMA signal)."""
    return MarketState(
        symbol="ETH",
        timeframe="4h",
        timestamp=datetime.now(timezone.utc),
        open=Decimal("3400"),
        high=Decimal("3450"),
        low=Decimal("3380"),
        close=Decimal("3420"),  # Price between SMA20 and SMA50
        volume=Decimal("500"),
        atr=Decimal("30"),
        atr_pct=Decimal("0.88"),
        adx=Decimal("18"),
        rsi=Decimal("45"),
        ema50=Decimal("3420"),
        ema200=Decimal("3400"),
        ema200_slope=Decimal("0.0005"),
        sma20=Decimal("3410"),  # Price > SMA20 but SMA20 < SMA50 - no clear signal
        sma50=Decimal("3430"),  # SMA50 > SMA20 - mixed signal
        # Previous candle data
        prev_open=Decimal("3390"),
        prev_high=Decimal("3420"),
        prev_low=Decimal("3380"),
        prev_close=Decimal("3400"),
        bullish_engulfing=False,
        bearish_engulfing=True,  # Bearish engulfing for potential short setup
        regime=Regime.RANGE,
        trend_direction=Direction.FLAT,
        choppiness=Decimal("65"),
    )


@pytest.fixture
def market_state_chaos():
    """Create a market state in CHAOS regime (no clear SMA signal)."""
    return MarketState(
        symbol="BTC",
        timeframe="4h",
        timestamp=datetime.now(timezone.utc),
        open=Decimal("95000"),
        high=Decimal("97000"),
        low=Decimal("93000"),
        close=Decimal("94500"),  # Price < SMA20 but SMA20 > SMA50 - no clear signal
        volume=Decimal("2000"),
        atr=Decimal("800"),
        atr_pct=Decimal("0.85"),
        adx=Decimal("22"),
        rsi=Decimal("50"),
        ema50=Decimal("95000"),
        ema200=Decimal("94000"),
        ema200_slope=Decimal("0.0008"),
        sma20=Decimal("95500"),  # Price (94500) < SMA20 (95500)
        sma50=Decimal("94000"),  # SMA20 (95500) > SMA50 (94000) - mixed signal
        # Previous candle data
        prev_open=Decimal("94000"),
        prev_high=Decimal("96000"),
        prev_low=Decimal("93500"),
        prev_close=Decimal("95000"),
        bullish_engulfing=False,
        bearish_engulfing=False,  # No engulfing pattern in chaos
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
    """Test TrendFollowStrategy (SMA Crossover)."""

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
            "stop_atr_mult": 3.0,
            "allow_short": True,
            "min_atr_pct": 0.5,
        }
        strategy = TrendFollowStrategy(config=config)
        assert strategy.stop_atr_mult == 3.0
        assert strategy.allow_short == True
        assert strategy.min_atr_pct == 0.5

    def test_can_trade_in_any_regime(self, market_state_trend, market_state_range, market_state_chaos):
        """Test SMA strategy can trade in any regime (signals are self-contained)."""
        from simple_bot.strategies import TrendFollowStrategy

        strategy = TrendFollowStrategy()
        # SMA crossover strategy doesn't rely on regime
        assert strategy.can_trade(market_state_trend) == True
        assert strategy.can_trade(market_state_range) == True
        assert strategy.can_trade(market_state_chaos) == True

    def test_evaluate_generates_long_setup_on_golden_cross(self, market_state_trend):
        """Test strategy generates LONG setup on golden cross with candle confirmation."""
        from simple_bot.strategies import TrendFollowStrategy

        strategy = TrendFollowStrategy()
        result = strategy.evaluate(market_state_trend)

        assert result.has_setup == True
        assert result.setup is not None
        assert result.setup.direction == Direction.LONG
        assert result.setup.symbol == "BTC"
        assert "Golden Cross" in result.reason
        assert "bullish engulfing" in result.reason  # Candle confirmation in reason

    def test_evaluate_rejects_without_candle_confirmation(self, market_state_trend):
        """Test strategy rejects setup without candle confirmation when required."""
        from simple_bot.strategies import TrendFollowStrategy

        # Remove the bullish engulfing signal
        market_state_trend.bullish_engulfing = False

        strategy = TrendFollowStrategy()
        result = strategy.evaluate(market_state_trend)

        # Should reject because no bullish engulfing
        assert result.has_setup == False
        assert "No bullish engulfing candle confirmation" in result.reason

    def test_evaluate_generates_setup_without_candle_confirm_disabled(self, market_state_trend):
        """Test strategy generates setup when candle confirmation is disabled."""
        from simple_bot.strategies import TrendFollowStrategy

        # Remove the bullish engulfing signal
        market_state_trend.bullish_engulfing = False

        # Disable candle confirmation requirement
        strategy = TrendFollowStrategy(config={"require_candle_confirm": False})
        result = strategy.evaluate(market_state_trend)

        # Should still generate setup because candle confirmation is disabled
        assert result.has_setup == True
        assert result.setup is not None
        assert result.setup.direction == Direction.LONG
        assert "Golden Cross" in result.reason
        assert "bullish engulfing" not in result.reason  # No candle mention when disabled

    def test_evaluate_rejects_mixed_signal(self, market_state_chaos):
        """Test strategy rejects when SMAs give mixed signal."""
        from simple_bot.strategies import TrendFollowStrategy

        strategy = TrendFollowStrategy()
        result = strategy.evaluate(market_state_chaos)

        # Price < SMA20 but SMA20 > SMA50 - no clear signal
        assert result.has_setup == False
        assert "No SMA crossover signal" in result.reason

    def test_short_disabled_by_default(self, market_state_range):
        """Test short positions are disabled by default."""
        from simple_bot.strategies import TrendFollowStrategy

        # Create a death cross scenario
        market_state_range.close = Decimal("3380")  # Price < SMA20
        market_state_range.sma20 = Decimal("3400")  # SMA20 < SMA50
        market_state_range.sma50 = Decimal("3450")  # Death cross setup

        strategy = TrendFollowStrategy()
        result = strategy.evaluate(market_state_range)

        # Should reject because shorts are disabled
        assert result.has_setup == False
        assert "Short positions disabled" in result.reason


class TestMomentumScalperStrategy:
    """Test MomentumScalperStrategy."""

    def test_import(self):
        """Test strategy can be imported."""
        from simple_bot.strategies import MomentumScalperStrategy
        assert MomentumScalperStrategy is not None

    def test_strategy_initialization(self):
        """Test strategy initialization."""
        from simple_bot.strategies import MomentumScalperStrategy

        strategy = MomentumScalperStrategy()
        assert strategy.name == "trend_momentum"

    def test_can_trade_only_in_trend_regime(self, market_state_range, market_state_trend):
        """Test strategy only trades in TREND regime."""
        from simple_bot.strategies import MomentumScalperStrategy

        strategy = MomentumScalperStrategy()
        assert strategy.can_trade(market_state_range) == False
        assert strategy.can_trade(market_state_trend) == True


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
        assert config.max_calls_per_day == 50000
        assert config.fallback_on_error == "deny"


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
        assert decision.decision == "DENY"  # Default fallback is deny (fail-safe)
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
        from simple_bot.main import ConservativeConfig

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
  trend_momentum:
    enabled: true

stops:
  stop_loss_pct: 0.4
  take_profit_pct: 0.8

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
            assert config.trend_momentum_enabled == True
            assert config.stop_loss_pct == 0.4
            assert config.take_profit_pct == 0.8
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
