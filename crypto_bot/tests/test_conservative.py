"""
Tests for HLQuantBot Conservative System
==========================================

Unit and integration tests for the conservative trading system components.

Run:
    pytest crypto_bot/tests/test_conservative.py -v
"""

import asyncio
import pytest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from crypto_bot.core.enums import Topic
from crypto_bot.core.models import (
    MarketState,
    Setup,
    TradeIntent,
    Regime,
    Direction,
    SetupType,
)


# =============================================================================
# Helpers — default field values for Pydantic models in tests
# =============================================================================

# Default Setup fields added by FLAG-Trader refactor
_SETUP_DEFAULTS: dict[str, object] = dict(
    atr_pct=Decimal("0.5"),
    model_tp_pct=2.5,
    model_sl_pct=1.0,
    llm_approved=True,
    llm_confidence=Decimal("0.7"),
    llm_reason="test",
    entry_reason="test",
    entry_confidence=0.7,
    entry_trigger_details="",
)


def _make_setup(**overrides: object) -> Setup:
    """Create a Setup with sensible test defaults.  Override any field via kwargs."""
    defaults: dict[str, object] = dict(
        id="setup_test",
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
    defaults.update(_SETUP_DEFAULTS)
    defaults.update(overrides)
    return Setup(**defaults)  # type: ignore[arg-type]


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
    return _make_setup(id="setup_test_001")


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
            atr_pct=Decimal("0.5"),
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
        from crypto_bot.services import MarketStateConfig

        config = MarketStateConfig()
        assert config.assets == ["BTC", "ETH"]
        assert config.timeframe == "4h"
        assert config.trend_adx_entry_min == 28.0
        assert config.trend_adx_exit_min == 22.0

    def test_risk_config(self):
        """Test RiskConfig defaults."""
        from crypto_bot.services import RiskConfig

        config = RiskConfig()
        assert config.min_per_trade_pct == 10.0
        assert config.max_per_trade_pct == 30.0
        assert config.min_leverage == 2
        assert config.max_leverage == 5



# =============================================================================
# Risk Manager Tests
# =============================================================================

class TestRiskManager:
    """Test RiskManagerService."""

    def test_import(self):
        """Test service can be imported."""
        from crypto_bot.services import RiskManagerService, create_risk_manager
        assert RiskManagerService is not None
        assert create_risk_manager is not None

    def test_position_size_calculation_default_confidence(self):
        """Test risk-based position sizing with default confidence (0.5 -> min risk)."""
        from crypto_bot.services.risk_manager import RiskManagerService, RiskConfig

        config = RiskConfig(min_per_trade_pct=10.0, max_per_trade_pct=30.0)
        service = RiskManagerService(config=config)

        # Set equity
        service._current_equity = Decimal("10000")

        # Setup with default confidence=0.5 (below threshold 0.6 -> min_per_trade_pct)
        setup = _make_setup(
            id="test_setup",
            stop_price=Decimal("94560"),  # 1.5% stop distance
            stop_distance_pct=Decimal("1.5"),
            confidence=Decimal("0.5"),
        )

        # Calculate risk
        params = service._calculate_risk_params(setup)

        # Confidence 0.5 <= threshold 0.6 -> min_per_trade_pct = 10%
        # risk_amount = 10000 * 0.10 = 1000
        assert params.risk_amount == Decimal("1000")
        assert params.size_approved is True

    def test_position_size_no_confidence(self):
        """Test risk-based position sizing without confidence (uses min risk)."""
        from crypto_bot.services.risk_manager import RiskManagerService, RiskConfig

        config = RiskConfig(min_per_trade_pct=10.0, max_per_trade_pct=30.0)
        service = RiskManagerService(config=config)
        service._current_equity = Decimal("10000")

        # Setup with confidence=0 -> uses min_per_trade_pct
        setup = _make_setup(
            id="test_setup_no_conf",
            stop_price=Decimal("94560"),
            stop_distance_pct=Decimal("1.5"),
            confidence=Decimal("0"),
        )

        params = service._calculate_risk_params(setup)

        # Confidence 0 <= threshold 0.6 -> min_per_trade_pct = 10%
        # risk_amount = 10000 * 0.10 = 1000
        assert params.risk_amount == Decimal("1000")
        assert params.size_approved is True


# =============================================================================
# Configuration Loading Tests
# =============================================================================

class TestConfigurationLoading:
    """Test configuration loading from YAML."""

    def test_conservative_config_from_yaml(self, tmp_path):
        """Test loading ConservativeConfig from YAML."""
        from crypto_bot.main import ConservativeConfig

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
  min_per_trade_pct: 10.0
  max_per_trade_pct: 30.0
  max_position_pct: 70
  min_leverage: 2
  max_leverage: 5

regime:
  trend_adx_entry_min: 28
  trend_adx_exit_min: 22

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
            assert config.min_per_trade_pct == 10.0
            assert config.max_per_trade_pct == 30.0
            assert config.min_leverage == 2
            assert config.max_leverage == 5
            assert config.stop_loss_pct == 0.8
            assert config.take_profit_pct == 1.6
            assert config.testnet is True
        finally:
            if old_env is not None:
                os.environ["ENVIRONMENT"] = old_env


# =============================================================================
# Kelly Sizing Tests
# =============================================================================

class TestConfidenceScaling:
    """Test dynamic risk/leverage scaling based on LLM confidence."""

    def _make_service(self, min_pct: float = 10.0, max_pct: float = 30.0):
        from crypto_bot.services.risk_manager import RiskManagerService, RiskConfig
        config = RiskConfig(min_per_trade_pct=min_pct, max_per_trade_pct=max_pct,
                            min_leverage=2, max_leverage=5)
        service = RiskManagerService(config=config)
        service._current_equity = Decimal("10000")
        return service

    def test_scale_by_confidence_at_threshold(self):
        """At confidence == threshold, returns min_val."""
        from crypto_bot.services.risk_manager import scale_by_confidence
        assert scale_by_confidence(0.6, 0.6, 10.0, 30.0) == 10.0

    def test_scale_by_confidence_below_threshold(self):
        """Below threshold, returns min_val."""
        from crypto_bot.services.risk_manager import scale_by_confidence
        assert scale_by_confidence(0.4, 0.6, 10.0, 30.0) == 10.0

    def test_scale_by_confidence_at_max(self):
        """At confidence == 1.0, returns max_val."""
        from crypto_bot.services.risk_manager import scale_by_confidence
        assert scale_by_confidence(1.0, 0.6, 10.0, 30.0) == 30.0

    def test_scale_by_confidence_midpoint(self):
        """At midpoint confidence, returns midpoint value."""
        from crypto_bot.services.risk_manager import scale_by_confidence
        # threshold=0.6, max=1.0, midpoint confidence = 0.8
        # ratio = (0.8 - 0.6) / (1.0 - 0.6) = 0.5
        # result = 10.0 + 0.5 * (30.0 - 10.0) = 20.0
        assert scale_by_confidence(0.8, 0.6, 10.0, 30.0) == 20.0

    def test_high_confidence_gets_larger_risk(self):
        """High confidence setup gets larger risk_amount than low confidence."""
        service = self._make_service()

        setup_high = _make_setup(
            id="test_high",
            setup_type=SetupType.MOMENTUM,
            stop_price=Decimal("95232"),
            stop_distance_pct=Decimal("0.8"),
            confidence=Decimal("0.95"),
        )

        setup_low = _make_setup(
            id="test_low",
            symbol="ETH",
            setup_type=SetupType.MOMENTUM,
            entry_price=Decimal("3000"),
            stop_price=Decimal("2976"),
            stop_distance_pct=Decimal("0.8"),
            atr=Decimal("30"),
            confidence=Decimal("0.65"),
        )

        params_high = service._calculate_risk_params(setup_high)
        params_low = service._calculate_risk_params(setup_low)

        assert params_high.size_approved is True
        assert params_low.size_approved is True
        # High confidence -> larger risk_amount
        assert params_high.risk_amount > params_low.risk_amount
        # High confidence -> higher leverage
        assert params_high.leverage_used > params_low.leverage_used


# =============================================================================
# Correlation Filter Tests
# =============================================================================

class TestCorrelationFilter:
    """Test correlation group filtering."""

    def _make_service(self):
        from crypto_bot.services.risk_manager import RiskManagerService, RiskConfig
        config = RiskConfig(min_per_trade_pct=10.0, max_per_trade_pct=30.0)
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
        setup = _make_setup(
            id="test_corr",
            symbol="ARB",
            setup_type=SetupType.MOMENTUM,
            entry_price=Decimal("1.5"),
            stop_price=Decimal("1.488"),
            stop_distance_pct=Decimal("0.8"),
            atr=Decimal("0.05"),
            adx=Decimal("30"),
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
        from crypto_bot.services.risk_manager import RiskManagerService, RiskConfig
        config = RiskConfig(min_per_trade_pct=10.0, max_per_trade_pct=30.0)
        service = RiskManagerService(config=config)
        service._current_equity = Decimal("10000")
        return service

    def test_no_cooldown_initially(self):
        """First trade on a symbol has no cooldown."""
        service = self._make_service()

        setup = _make_setup(
            id="test_cd1",
            setup_type=SetupType.MOMENTUM,
            stop_price=Decimal("95232"),
            stop_distance_pct=Decimal("0.8"),
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

        setup = _make_setup(
            id="test_cd2",
            setup_type=SetupType.MOMENTUM,
            stop_price=Decimal("95232"),
            stop_distance_pct=Decimal("0.8"),
            confidence=Decimal("0.60"),
        )

        params = service._calculate_risk_params(setup)
        assert params.size_approved is False
        assert "Cooldown" in (params.rejection_reason or "")

    def test_cooldown_expired_allows_trade(self):
        """Trade is allowed after cooldown expires."""
        from datetime import timedelta
        service = self._make_service()

        # Simulate an old trade 15 minutes ago (cooldown is 10 min)
        service._last_trade_time["BTC"] = datetime.now(timezone.utc) - timedelta(minutes=15)

        setup = _make_setup(
            id="test_cd3",
            setup_type=SetupType.MOMENTUM,
            stop_price=Decimal("95232"),
            stop_distance_pct=Decimal("0.8"),
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

        setup = _make_setup(
            id="test_cd4",
            symbol="ETH",
            setup_type=SetupType.MOMENTUM,
            entry_price=Decimal("3000"),
            stop_price=Decimal("2976"),
            stop_distance_pct=Decimal("0.8"),
            atr=Decimal("30"),
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
        from crypto_bot.services.risk_manager import RiskManagerService, RiskConfig

        config = RiskConfig()
        service = RiskManagerService(config=config)

        assert service._trades_today == 0
        service.increment_trade_count()
        assert service._trades_today == 1
        service.increment_trade_count()
        assert service._trades_today == 2

    @pytest.mark.asyncio
    async def test_publish_intent_increments_count(self):
        """Publishing intent increments daily trade count and records cooldown time."""
        from crypto_bot.services.risk_manager import RiskManagerService, RiskConfig
        from crypto_bot.services.message_bus import MessageBus

        config = RiskConfig()
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
            atr_pct=Decimal("0.5"),
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
        from crypto_bot.services.risk_manager import RiskManagerService, RiskConfig
        config = RiskConfig()
        service = RiskManagerService(config=config)
        return service

    @pytest.mark.asyncio
    async def test_decrement_trade_count_on_cancel(self):
        """When an order is cancelled, the daily trade count is decremented."""
        from crypto_bot.services.message_bus import MessageBus, Message

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
        from crypto_bot.services.message_bus import MessageBus, Message

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
        from crypto_bot.core.enums import Topic

        # Check new topics exist
        assert hasattr(Topic, "MARKET_STATE")
        assert hasattr(Topic, "REGIME")
        assert hasattr(Topic, "SETUPS")
        assert hasattr(Topic, "TRADE_INTENT")
        assert hasattr(Topic, "RISK_ALERTS")

    @pytest.mark.asyncio
    async def test_services_can_be_created(self):
        """Test all services can be instantiated."""
        from crypto_bot.services import (
            create_market_state_service,
            create_risk_manager,
        )

        # Create without bus/db (dry run)
        market_state = create_market_state_service(testnet=True)
        risk_manager = create_risk_manager()

        assert market_state is not None
        assert risk_manager is not None


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
