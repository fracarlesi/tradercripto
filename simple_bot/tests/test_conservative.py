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
        assert config.trend_adx_entry_min == 28.0
        assert config.trend_adx_exit_min == 22.0

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

    def test_position_size_calculation_default_confidence(self):
        """Test risk-based position sizing with default confidence (Kelly minimum)."""
        from simple_bot.services.risk_manager import RiskManagerService, RiskConfig

        config = RiskConfig(per_trade_pct=5.0)
        service = RiskManagerService(config=config)

        # Set equity
        service._current_equity = Decimal("10000")

        # Create a setup with default confidence=0.5
        # Kelly at p=0.5 returns minimum 2%, so risk = 10000 * 0.02 = 200
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

        # Kelly minimum at p=0.5: risk_pct = 0.02, risk_amount = 10000 * 0.02 = 200
        assert params.risk_amount == Decimal("200")
        assert params.size_approved is True

    def test_position_size_no_confidence(self):
        """Test risk-based position sizing without confidence (config fallback)."""
        from simple_bot.services.risk_manager import RiskManagerService, RiskConfig

        config = RiskConfig(per_trade_pct=5.0)
        service = RiskManagerService(config=config)
        service._current_equity = Decimal("10000")

        # Setup with confidence=0 -> falls back to per_trade_pct
        setup = Setup(
            id="test_setup_no_conf",
            symbol="BTC",
            timestamp=datetime.now(timezone.utc),
            setup_type=SetupType.TREND_BREAKOUT,
            direction=Direction.LONG,
            regime=Regime.TREND,
            entry_price=Decimal("96000"),
            stop_price=Decimal("94560"),
            stop_distance_pct=Decimal("1.5"),
            atr=Decimal("500"),
            adx=Decimal("35"),
            rsi=Decimal("55"),
            confidence=Decimal("0"),
        )

        params = service._calculate_risk_params(setup)

        # Fallback: risk_pct = 5.0 / 100 = 0.05, risk_amount = 10000 * 0.05 = 500
        assert params.risk_amount == Decimal("500")
        assert params.size_approved is True


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
  per_trade_pct: 5.0
  max_per_trade_pct: 10.0
  max_positions: 2
  max_position_pct: 70
  max_daily_trades: 8

kill_switch:
  daily_loss_pct: 2.0
  weekly_loss_pct: 5.0
  max_drawdown_pct: 15.0

regime:
  trend_adx_entry_min: 28
  trend_adx_exit_min: 22

llm:
  enabled: true
  max_calls_per_day: 6

strategies:
  trend_follow:
    enabled: true
  trend_momentum:
    enabled: true

stops:
  stop_loss_pct: 0.8
  take_profit_pct: 1.6

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
            assert config.per_trade_pct == 5.0
            assert config.max_per_trade_pct == 10.0
            assert config.max_daily_trades == 8
            assert config.max_drawdown_pct == 15.0
            assert config.stop_loss_pct == 0.8
            assert config.take_profit_pct == 1.6
            assert config.testnet is True
        finally:
            if old_env is not None:
                os.environ["ENVIRONMENT"] = old_env


# =============================================================================
# Regime Gate Tests
# =============================================================================

class TestRegimeGate:
    """Test that _evaluate_all_assets skips non-TREND assets."""

    def _make_bot(self) -> "ConservativeBot":
        """Create a ConservativeBot with mocked dependencies for regime gate testing."""
        from simple_bot.main import ConservativeBot, ConservativeConfig
        from simple_bot.services.ml_model import MLTradeModel

        bot = ConservativeBot.__new__(ConservativeBot)
        bot._config = MagicMock(spec=ConservativeConfig)
        bot._config.max_positions = 3
        bot._config.ml_min_probability = 0.55
        bot._config.stop_loss_pct = 0.8
        bot._config.take_profit_pct = 1.6
        bot._config.max_spread_pct = 0.30
        bot._exchange = AsyncMock()
        bot._bus = AsyncMock(spec_set=["publish"])
        bot._bus.publish = AsyncMock()

        # ML model mock: always returns high probability
        bot._ml_model = MagicMock(spec=MLTradeModel)
        bot._ml_model.is_loaded = True
        bot._ml_model.optimal_threshold = 0.60
        bot._ml_model.extract_features = MagicMock(return_value={"feat": 1.0})
        bot._ml_model.predict = MagicMock(return_value=(0.75, "mock reason"))
        # n_features_in_ determines if volume breakout path is active
        bot._ml_model._model = MagicMock()
        bot._ml_model._model.n_features_in_ = 13

        # Volume breakout config
        bot._config.volume_breakout_enabled = True
        bot._config.volume_breakout_min_volume_ratio = 2.0
        bot._config.volume_breakout_min_candle_body_pct = 0.3
        bot._config.volume_breakout_min_atr_pct = 0.15
        bot._config.volume_breakout_rsi_min = 25.0
        bot._config.volume_breakout_rsi_max = 80.0
        bot._config.volume_breakout_allowed_regimes = ["chaos", "trend"]

        # Momentum burst config
        bot._config.momentum_burst_enabled = True
        bot._config.momentum_burst_min_rsi_slope = 8.0
        bot._config.momentum_burst_min_candle_body_pct = 0.3
        bot._config.momentum_burst_max_rsi_entry = 75.0
        bot._config.momentum_burst_min_volume_ratio = 1.2
        bot._config.momentum_burst_allowed_regimes = ["chaos", "trend"]

        # Services: no kill switch, no cooldown, no protections
        bot._services = {}

        return bot

    def _make_state(self, symbol: str, regime: Regime) -> MarketState:
        """Create a MarketState with the given regime."""
        return MarketState(
            symbol=symbol,
            timeframe="15m",
            timestamp=datetime.now(timezone.utc),
            open=Decimal("100"),
            high=Decimal("105"),
            low=Decimal("95"),
            close=Decimal("102"),
            volume=Decimal("1000"),
            atr=Decimal("5"),
            atr_pct=Decimal("0.5"),
            adx=Decimal("30"),
            rsi=Decimal("55"),
            ema50=Decimal("100"),
            ema200=Decimal("98"),
            ema200_slope=Decimal("0.001"),
            sma20=Decimal("101"),
            sma50=Decimal("99"),
            prev_open=Decimal("99"),
            prev_high=Decimal("101"),
            prev_low=Decimal("97"),
            prev_close=Decimal("100"),
            bullish_engulfing=False,
            bearish_engulfing=False,
            regime=regime,
            trend_direction=Direction.LONG if regime == Regime.TREND else Direction.FLAT,
        )

    @pytest.mark.asyncio
    async def test_range_assets_skipped(self) -> None:
        """Assets in RANGE regime are skipped by the regime gate."""
        bot = self._make_bot()
        market_state_svc = MagicMock()
        market_state_svc.get_all_states.return_value = {
            "ETH": self._make_state("ETH", Regime.RANGE),
            "SOL": self._make_state("SOL", Regime.RANGE),
        }
        bot._services = {"market_state": market_state_svc}

        await bot._evaluate_all_assets()

        # ML predict should never be called (all assets are RANGE)
        bot._ml_model.predict.assert_not_called()

    @pytest.mark.asyncio
    async def test_chaos_assets_skipped(self) -> None:
        """Assets in CHAOS regime are skipped by the regime gate."""
        bot = self._make_bot()
        market_state_svc = MagicMock()
        market_state_svc.get_all_states.return_value = {
            "BTC": self._make_state("BTC", Regime.CHAOS),
        }
        bot._services = {"market_state": market_state_svc}

        await bot._evaluate_all_assets()

        bot._ml_model.predict.assert_not_called()

    @pytest.mark.asyncio
    async def test_trend_assets_evaluated(self) -> None:
        """Assets in TREND regime pass the regime gate and reach ML prediction."""
        bot = self._make_bot()
        market_state_svc = MagicMock()
        market_state_svc.get_all_states.return_value = {
            "BTC": self._make_state("BTC", Regime.TREND),
        }
        bot._services = {"market_state": market_state_svc}

        # Mock _execute_setup so it doesn't try real execution
        bot._execute_setup = AsyncMock(return_value=True)

        await bot._evaluate_all_assets()

        # ML predict should have been called once (direction from market state)
        assert bot._ml_model.predict.call_count == 1

    @pytest.mark.asyncio
    async def test_mixed_regimes_only_trend_evaluated(self) -> None:
        """Only TREND assets are evaluated when a mix of regimes is present."""
        bot = self._make_bot()
        market_state_svc = MagicMock()
        market_state_svc.get_all_states.return_value = {
            "BTC": self._make_state("BTC", Regime.TREND),
            "ETH": self._make_state("ETH", Regime.RANGE),
            "SOL": self._make_state("SOL", Regime.CHAOS),
            "DOGE": self._make_state("DOGE", Regime.TREND),
        }
        bot._services = {"market_state": market_state_svc}

        bot._execute_setup = AsyncMock(return_value=True)

        await bot._evaluate_all_assets()

        # 2 TREND assets x 1 predict each = 2 predict calls
        assert bot._ml_model.predict.call_count == 2


# =============================================================================
# ML Threshold Selection Tests
# =============================================================================

class TestMLThresholdSelection:
    """Test that _evaluate_all_assets uses the ML model's calibrated
    optimal_threshold instead of just config.ml_min_probability."""

    def _make_bot(
        self,
        *,
        ml_min_probability: float = 0.50,
        optimal_threshold: float | None = 0.70,
        predict_prob: float = 0.65,
    ) -> "ConservativeBot":
        """Create a ConservativeBot with configurable threshold parameters."""
        from simple_bot.main import ConservativeBot, ConservativeConfig
        from simple_bot.services.ml_model import MLTradeModel

        bot = ConservativeBot.__new__(ConservativeBot)
        bot._config = MagicMock(spec=ConservativeConfig)
        bot._config.max_positions = 3
        bot._config.ml_min_probability = ml_min_probability
        bot._config.stop_loss_pct = 0.8
        bot._config.take_profit_pct = 1.6
        bot._config.max_spread_pct = 0.30
        bot._exchange = AsyncMock()
        bot._bus = AsyncMock(spec_set=["publish"])
        bot._bus.publish = AsyncMock()

        bot._ml_model = MagicMock(spec=MLTradeModel)
        bot._ml_model.is_loaded = True
        bot._ml_model.optimal_threshold = optimal_threshold
        bot._ml_model.extract_features = MagicMock(return_value={"feat": 1.0})
        bot._ml_model.predict = MagicMock(return_value=(predict_prob, "mock reason"))
        # n_features_in_ determines if volume breakout path is active
        bot._ml_model._model = MagicMock()
        bot._ml_model._model.n_features_in_ = 13

        # Volume breakout config
        bot._config.volume_breakout_enabled = True
        bot._config.volume_breakout_min_volume_ratio = 2.0
        bot._config.volume_breakout_min_candle_body_pct = 0.3
        bot._config.volume_breakout_min_atr_pct = 0.15
        bot._config.volume_breakout_rsi_min = 25.0
        bot._config.volume_breakout_rsi_max = 80.0
        bot._config.volume_breakout_allowed_regimes = ["chaos", "trend"]

        # Momentum burst config
        bot._config.momentum_burst_enabled = True
        bot._config.momentum_burst_min_rsi_slope = 8.0
        bot._config.momentum_burst_min_candle_body_pct = 0.3
        bot._config.momentum_burst_max_rsi_entry = 75.0
        bot._config.momentum_burst_min_volume_ratio = 1.2
        bot._config.momentum_burst_allowed_regimes = ["chaos", "trend"]

        bot._services = {}
        return bot

    def _make_trend_state(self, symbol: str) -> MarketState:
        """Create a TREND MarketState."""
        return MarketState(
            symbol=symbol,
            timeframe="15m",
            timestamp=datetime.now(timezone.utc),
            open=Decimal("100"),
            high=Decimal("105"),
            low=Decimal("95"),
            close=Decimal("102"),
            volume=Decimal("1000"),
            atr=Decimal("5"),
            atr_pct=Decimal("0.5"),
            adx=Decimal("30"),
            rsi=Decimal("55"),
            ema50=Decimal("100"),
            ema200=Decimal("98"),
            ema200_slope=Decimal("0.001"),
            sma20=Decimal("101"),
            sma50=Decimal("99"),
            prev_open=Decimal("99"),
            prev_high=Decimal("101"),
            prev_low=Decimal("97"),
            prev_close=Decimal("100"),
            bullish_engulfing=False,
            bearish_engulfing=False,
            regime=Regime.TREND,
            trend_direction=Direction.LONG,
        )

    @pytest.mark.asyncio
    async def test_optimal_threshold_used_over_min_probability(self) -> None:
        """When optimal_threshold (0.70) > min_probability (0.50),
        a signal at 0.65 should be REJECTED."""
        bot = self._make_bot(
            ml_min_probability=0.50,
            optimal_threshold=0.70,
            predict_prob=0.65,
        )
        market_state_svc = MagicMock()
        market_state_svc.get_all_states.return_value = {
            "BTC": self._make_trend_state("BTC"),
        }
        bot._services = {"market_state": market_state_svc}
        bot._execute_setup = AsyncMock(return_value=True)

        await bot._evaluate_all_assets()

        # Signal at 0.65 is below optimal_threshold 0.70 -> rejected
        bot._execute_setup.assert_not_called()

    @pytest.mark.asyncio
    async def test_signal_above_optimal_threshold_accepted(self) -> None:
        """Signal above optimal_threshold should be accepted."""
        bot = self._make_bot(
            ml_min_probability=0.50,
            optimal_threshold=0.70,
            predict_prob=0.75,
        )
        market_state_svc = MagicMock()
        market_state_svc.get_all_states.return_value = {
            "BTC": self._make_trend_state("BTC"),
        }
        bot._services = {"market_state": market_state_svc}
        bot._execute_setup = AsyncMock(return_value=True)

        await bot._evaluate_all_assets()

        bot._execute_setup.assert_called_once()

    @pytest.mark.asyncio
    async def test_min_probability_used_as_floor_when_no_optimal(self) -> None:
        """When optimal_threshold is None, min_probability is the fallback."""
        bot = self._make_bot(
            ml_min_probability=0.50,
            optimal_threshold=None,
            predict_prob=0.55,
        )
        market_state_svc = MagicMock()
        market_state_svc.get_all_states.return_value = {
            "BTC": self._make_trend_state("BTC"),
        }
        bot._services = {"market_state": market_state_svc}
        bot._execute_setup = AsyncMock(return_value=True)

        await bot._evaluate_all_assets()

        # 0.55 > 0.50 -> accepted
        bot._execute_setup.assert_called_once()

    @pytest.mark.asyncio
    async def test_min_probability_is_absolute_floor(self) -> None:
        """min_probability acts as floor even if optimal_threshold is lower
        (should not happen in practice, but guards against edge cases)."""
        bot = self._make_bot(
            ml_min_probability=0.60,
            optimal_threshold=0.55,
            predict_prob=0.57,
        )
        market_state_svc = MagicMock()
        market_state_svc.get_all_states.return_value = {
            "BTC": self._make_trend_state("BTC"),
        }
        bot._services = {"market_state": market_state_svc}
        bot._execute_setup = AsyncMock(return_value=True)

        await bot._evaluate_all_assets()

        # max(0.60, 0.55) = 0.60, signal 0.57 < 0.60 -> rejected
        bot._execute_setup.assert_not_called()

    @pytest.mark.asyncio
    async def test_signal_exactly_at_threshold_accepted(self) -> None:
        """Signal exactly at the effective threshold should pass (not < threshold)."""
        bot = self._make_bot(
            ml_min_probability=0.50,
            optimal_threshold=0.70,
            predict_prob=0.70,
        )
        market_state_svc = MagicMock()
        market_state_svc.get_all_states.return_value = {
            "BTC": self._make_trend_state("BTC"),
        }
        bot._services = {"market_state": market_state_svc}
        bot._execute_setup = AsyncMock(return_value=True)

        await bot._evaluate_all_assets()

        # 0.70 is NOT < 0.70 -> accepted
        bot._execute_setup.assert_called_once()


# =============================================================================
# Kelly Sizing Tests
# =============================================================================

class TestKellySizing:
    """Test Half-Kelly position sizing based on P(TP)."""

    def _make_service(self, per_trade_pct: float = 5.0, max_per_trade_pct: float = 10.0):
        from simple_bot.services.risk_manager import RiskManagerService, RiskConfig
        config = RiskConfig(per_trade_pct=per_trade_pct, max_per_trade_pct=max_per_trade_pct)
        service = RiskManagerService(config=config)
        service._current_equity = Decimal("10000")
        return service

    def test_kelly_at_50pct_returns_minimum(self):
        """At P(TP)=50% (coin flip), Kelly returns minimum 2%."""
        service = self._make_service()
        frac = service._kelly_fraction(0.50)
        assert frac == 0.02

    def test_kelly_below_50pct_returns_minimum(self):
        """Below 50% probability, Kelly returns minimum 2%."""
        service = self._make_service()
        frac = service._kelly_fraction(0.40)
        assert frac == 0.02

    def test_kelly_at_70pct(self):
        """At P(TP)=70% with 2:1 RR, Kelly should give meaningful sizing."""
        service = self._make_service()
        frac = service._kelly_fraction(0.70, rr_ratio=2.0)
        # Full Kelly = (0.7*2 - 0.3)/2 = (1.4-0.3)/2 = 0.55
        # Half-Kelly = 0.275
        # Clamped to max_per_trade_pct/100 = 0.10
        assert frac == 0.10

    def test_kelly_at_60pct(self):
        """At P(TP)=60% with 2:1 RR."""
        service = self._make_service()
        frac = service._kelly_fraction(0.60, rr_ratio=2.0)
        # Full Kelly = (0.6*2 - 0.4)/2 = (1.2-0.4)/2 = 0.4
        # Half-Kelly = 0.2
        # Clamped to max_per_trade_pct/100 = 0.10
        assert frac == 0.10

    def test_kelly_at_55pct(self):
        """At P(TP)=55% with 2:1 RR."""
        service = self._make_service()
        frac = service._kelly_fraction(0.55, rr_ratio=2.0)
        # Full Kelly = (0.55*2 - 0.45)/2 = (1.1-0.45)/2 = 0.325
        # Half-Kelly = 0.1625
        # Clamped to 0.10
        assert frac == 0.10

    def test_kelly_clamped_by_max_per_trade_pct(self):
        """Kelly is clamped to max_per_trade_pct / 100."""
        service = self._make_service(max_per_trade_pct=5.0)
        frac = service._kelly_fraction(0.70, rr_ratio=2.0)
        # Half-Kelly would be 0.275, but clamped to 0.05
        assert frac == 0.05

    def test_kelly_sizing_applied_in_risk_params(self):
        """High confidence setup gets larger position than low confidence."""
        service = self._make_service()

        setup_high = Setup(
            id="test_high",
            symbol="BTC",
            timestamp=datetime.now(timezone.utc),
            setup_type=SetupType.MOMENTUM,
            direction=Direction.LONG,
            regime=Regime.TREND,
            entry_price=Decimal("96000"),
            stop_price=Decimal("95232"),
            stop_distance_pct=Decimal("0.8"),
            atr=Decimal("500"),
            adx=Decimal("35"),
            rsi=Decimal("55"),
            confidence=Decimal("0.70"),
        )

        setup_low = Setup(
            id="test_low",
            symbol="ETH",
            timestamp=datetime.now(timezone.utc),
            setup_type=SetupType.MOMENTUM,
            direction=Direction.LONG,
            regime=Regime.TREND,
            entry_price=Decimal("3000"),
            stop_price=Decimal("2976"),
            stop_distance_pct=Decimal("0.8"),
            atr=Decimal("30"),
            adx=Decimal("35"),
            rsi=Decimal("55"),
            confidence=Decimal("0.50"),
        )

        params_high = service._calculate_risk_params(setup_high)
        params_low = service._calculate_risk_params(setup_low)

        assert params_high.size_approved is True
        assert params_low.size_approved is True
        # High confidence (Kelly=10%) should get larger risk_amount than low (Kelly min=2%)
        assert params_high.risk_amount > params_low.risk_amount


# =============================================================================
# Correlation Filter Tests
# =============================================================================

class TestCorrelationFilter:
    """Test correlation group filtering."""

    def _make_service(self):
        from simple_bot.services.risk_manager import RiskManagerService, RiskConfig
        config = RiskConfig(per_trade_pct=5.0, max_positions=5)
        service = RiskManagerService(config=config)
        service._current_equity = Decimal("10000")
        return service

    def test_same_group_blocked(self):
        """Second symbol in same correlated group is blocked."""
        service = self._make_service()
        # BTC is open
        service._open_positions["BTC"] = {"symbol": "BTC", "notional": 5000}

        # STX is in btc_ecosystem group with BTC
        assert service._check_correlation("STX") is False

    def test_different_group_allowed(self):
        """Symbol in different group is allowed."""
        service = self._make_service()
        service._open_positions["BTC"] = {"symbol": "BTC", "notional": 5000}

        # SOL is in layer1 group, not btc_ecosystem
        assert service._check_correlation("SOL") is True

    def test_ungrouped_symbol_allowed(self):
        """Symbol not in any correlation group is always allowed."""
        service = self._make_service()
        service._open_positions["BTC"] = {"symbol": "BTC", "notional": 5000}

        # LINK is not in any group
        assert service._check_correlation("LINK") is True

    def test_no_open_positions_always_allowed(self):
        """With no open positions, any symbol is allowed."""
        service = self._make_service()
        assert service._check_correlation("BTC") is True
        assert service._check_correlation("ETH") is True

    def test_correlation_blocks_in_risk_params(self):
        """Correlation filter causes rejection in _calculate_risk_params."""
        service = self._make_service()
        service._open_positions["ETH"] = {"symbol": "ETH", "notional": 5000}

        # ARB is in eth_ecosystem with ETH
        setup = Setup(
            id="test_corr",
            symbol="ARB",
            timestamp=datetime.now(timezone.utc),
            setup_type=SetupType.MOMENTUM,
            direction=Direction.LONG,
            regime=Regime.TREND,
            entry_price=Decimal("1.5"),
            stop_price=Decimal("1.488"),
            stop_distance_pct=Decimal("0.8"),
            atr=Decimal("0.05"),
            adx=Decimal("30"),
            rsi=Decimal("55"),
            confidence=Decimal("0.60"),
        )

        params = service._calculate_risk_params(setup)
        assert params.size_approved is False
        assert "Correlation filter" in (params.rejection_reason or "")


# =============================================================================
# Per-Symbol Cooldown Tests
# =============================================================================

class TestPerSymbolCooldown:
    """Test in-memory per-symbol cooldown."""

    def _make_service(self):
        from simple_bot.services.risk_manager import RiskManagerService, RiskConfig
        config = RiskConfig(per_trade_pct=5.0, max_positions=5)
        service = RiskManagerService(config=config)
        service._current_equity = Decimal("10000")
        return service

    def test_no_cooldown_initially(self):
        """First trade on a symbol has no cooldown."""
        service = self._make_service()

        setup = Setup(
            id="test_cd1",
            symbol="BTC",
            timestamp=datetime.now(timezone.utc),
            setup_type=SetupType.MOMENTUM,
            direction=Direction.LONG,
            regime=Regime.TREND,
            entry_price=Decimal("96000"),
            stop_price=Decimal("95232"),
            stop_distance_pct=Decimal("0.8"),
            atr=Decimal("500"),
            adx=Decimal("35"),
            rsi=Decimal("55"),
            confidence=Decimal("0.60"),
        )

        params = service._calculate_risk_params(setup)
        assert params.size_approved is True

    def test_cooldown_blocks_same_symbol(self):
        """Trade is blocked if recent trade on same symbol within cooldown window."""
        from datetime import timedelta
        service = self._make_service()

        # Simulate a recent trade on BTC 1 minute ago
        service._last_trade_time["BTC"] = datetime.now(timezone.utc) - timedelta(minutes=1)

        setup = Setup(
            id="test_cd2",
            symbol="BTC",
            timestamp=datetime.now(timezone.utc),
            setup_type=SetupType.MOMENTUM,
            direction=Direction.LONG,
            regime=Regime.TREND,
            entry_price=Decimal("96000"),
            stop_price=Decimal("95232"),
            stop_distance_pct=Decimal("0.8"),
            atr=Decimal("500"),
            adx=Decimal("35"),
            rsi=Decimal("55"),
            confidence=Decimal("0.60"),
        )

        params = service._calculate_risk_params(setup)
        assert params.size_approved is False
        assert "Cooldown" in (params.rejection_reason or "")

    def test_cooldown_expired_allows_trade(self):
        """Trade is allowed after cooldown expires."""
        from datetime import timedelta
        service = self._make_service()

        # Simulate an old trade 5 minutes ago (cooldown is 3 min)
        service._last_trade_time["BTC"] = datetime.now(timezone.utc) - timedelta(minutes=5)

        setup = Setup(
            id="test_cd3",
            symbol="BTC",
            timestamp=datetime.now(timezone.utc),
            setup_type=SetupType.MOMENTUM,
            direction=Direction.LONG,
            regime=Regime.TREND,
            entry_price=Decimal("96000"),
            stop_price=Decimal("95232"),
            stop_distance_pct=Decimal("0.8"),
            atr=Decimal("500"),
            adx=Decimal("35"),
            rsi=Decimal("55"),
            confidence=Decimal("0.60"),
        )

        params = service._calculate_risk_params(setup)
        assert params.size_approved is True

    def test_cooldown_different_symbol_not_affected(self):
        """Cooldown on BTC does not affect ETH."""
        from datetime import timedelta
        service = self._make_service()

        # BTC has recent trade
        service._last_trade_time["BTC"] = datetime.now(timezone.utc) - timedelta(minutes=1)

        setup = Setup(
            id="test_cd4",
            symbol="ETH",
            timestamp=datetime.now(timezone.utc),
            setup_type=SetupType.MOMENTUM,
            direction=Direction.LONG,
            regime=Regime.TREND,
            entry_price=Decimal("3000"),
            stop_price=Decimal("2976"),
            stop_distance_pct=Decimal("0.8"),
            atr=Decimal("30"),
            adx=Decimal("35"),
            rsi=Decimal("55"),
            confidence=Decimal("0.60"),
        )

        params = service._calculate_risk_params(setup)
        assert params.size_approved is True


# =============================================================================
# Increment Trade Count Tests
# =============================================================================

class TestIncrementTradeCount:
    """Test that increment_trade_count is called on publish."""

    def test_increment_trade_count(self):
        """Test trade count increments correctly."""
        from simple_bot.services.risk_manager import RiskManagerService, RiskConfig

        config = RiskConfig(max_daily_trades=8)
        service = RiskManagerService(config=config)

        assert service._trades_today == 0
        service.increment_trade_count()
        assert service._trades_today == 1
        service.increment_trade_count()
        assert service._trades_today == 2

    @pytest.mark.asyncio
    async def test_publish_intent_increments_count(self):
        """Publishing intent increments daily trade count and records cooldown time."""
        from simple_bot.services.risk_manager import RiskManagerService, RiskConfig
        from simple_bot.services.message_bus import MessageBus

        config = RiskConfig(max_daily_trades=8)
        bus = MessageBus()
        await bus.start()

        service = RiskManagerService(config=config, bus=bus)
        await service.start()

        intent = TradeIntent(
            id="intent_test",
            setup_id="setup_test",
            symbol="BTC",
            timestamp=datetime.now(timezone.utc),
            direction=Direction.LONG,
            setup_type=SetupType.MOMENTUM,
            entry_price=Decimal("96000"),
            position_size=Decimal("0.01"),
            notional_value=Decimal("960"),
            stop_price=Decimal("95232"),
            risk_amount=Decimal("50"),
            risk_pct=Decimal("5.0"),
        )

        assert service._trades_today == 0
        await service._publish_intent(intent)
        assert service._trades_today == 1
        assert "BTC" in service._last_trade_time

        await bus.stop()
        await service.stop()


# =============================================================================
# Decrement Trade Count Tests
# =============================================================================

class TestDecrementTradeCount:
    """Test that cancelled/expired orders decrement the daily trade counter."""

    def _make_service(self):
        from simple_bot.services.risk_manager import RiskManagerService, RiskConfig
        config = RiskConfig(max_daily_trades=8)
        service = RiskManagerService(config=config)
        return service

    @pytest.mark.asyncio
    async def test_decrement_trade_count_on_cancel(self):
        """When an order is cancelled, the daily trade count is decremented."""
        from simple_bot.services.message_bus import MessageBus, Message

        bus = MessageBus()
        await bus.start()

        service = self._make_service()
        service.bus = bus
        await service.start()

        # Simulate: intent published (count goes to 1)
        service.increment_trade_count()
        assert service._trades_today == 1

        # Track a pending intent so clear_pending_intent has something to clear
        service._pending_intents["BTC"] = MagicMock()

        # Simulate order_cancelled event
        msg = Message(
            topic=Topic.ORDERS,
            payload={"event": "order_cancelled", "symbol": "BTC"},
        )
        await service._handle_order_event(msg)

        # Count should be back to 0
        assert service._trades_today == 0

        await bus.stop()
        await service.stop()

    def test_decrement_trade_count_not_below_zero(self):
        """decrement_trade_count() does not go below 0."""
        service = self._make_service()
        assert service._trades_today == 0

        # Decrement when already at 0 - should stay at 0
        service.decrement_trade_count()
        assert service._trades_today == 0

        # Increment once, decrement twice - should stay at 0
        service.increment_trade_count()
        assert service._trades_today == 1
        service.decrement_trade_count()
        assert service._trades_today == 0
        service.decrement_trade_count()
        assert service._trades_today == 0

    @pytest.mark.asyncio
    async def test_trade_count_correct_after_cancel_cycle(self):
        """Simulate: increment (intent) -> cancel event -> verify count restored."""
        from simple_bot.services.message_bus import MessageBus, Message

        bus = MessageBus()
        await bus.start()

        service = self._make_service()
        service.bus = bus
        await service.start()

        # Start at 0
        assert service._trades_today == 0

        # First trade intent: count -> 1
        service.increment_trade_count()
        service._pending_intents["SOL"] = MagicMock()
        assert service._trades_today == 1

        # Second trade intent: count -> 2
        service.increment_trade_count()
        service._pending_intents["ETH"] = MagicMock()
        assert service._trades_today == 2

        # SOL order cancelled: count -> 1
        msg_cancel = Message(
            topic=Topic.ORDERS,
            payload={"event": "order_cancelled", "symbol": "SOL"},
        )
        await service._handle_order_event(msg_cancel)
        assert service._trades_today == 1
        assert "SOL" not in service._pending_intents

        # ETH order error: count -> 0
        msg_error = Message(
            topic=Topic.ORDERS,
            payload={"event": "order_error", "symbol": "ETH"},
        )
        await service._handle_order_event(msg_error)
        assert service._trades_today == 0
        assert "ETH" not in service._pending_intents

        await bus.stop()
        await service.stop()


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
