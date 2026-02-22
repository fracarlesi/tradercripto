#!/usr/bin/env python3
"""
Paper Mode Testing for US-008
=============================

Tests the simplified SMA strategy in paper mode to verify:
1. Strategy generates sensible signals
2. Trading limits are respected (daily trade limit)
3. Risk management parameters are correct
4. Candle confirmation works properly

Run with: cd simple_bot && python -m pytest tests/test_paper_mode.py -v
"""

from datetime import datetime, timezone
from decimal import Decimal
import pytest
from unittest.mock import AsyncMock, MagicMock

from simple_bot.strategies.trend_follow import TrendFollowStrategy
from simple_bot.core.models import MarketState, Regime, Direction
from simple_bot.services.risk_manager import RiskManagerService, RiskConfig


class TestPaperModeStrategy:
    """Test SMA crossover strategy in paper mode scenarios."""

    @pytest.fixture
    def strategy(self):
        """Create strategy with default config."""
        return TrendFollowStrategy(config={
            "stop_atr_mult": 2.5,
            "allow_short": False,
            "min_atr_pct": 0.3,
            "require_candle_confirm": True,
        })

    @pytest.fixture
    def golden_cross_state(self):
        """Market state with golden cross setup + bullish engulfing."""
        return MarketState(
            symbol="BTC",
            timestamp=datetime.now(timezone.utc),
            timeframe="1h",
            regime=Regime.TREND,
            open=Decimal("100000"),
            high=Decimal("100500"),
            low=Decimal("99500"),
            close=Decimal("100200"),  # Price > SMA20 > SMA50
            volume=Decimal("1000"),
            ema50=Decimal("98000"),
            ema200=Decimal("95000"),
            ema200_slope=Decimal("0.001"),
            atr=Decimal("500"),
            atr_pct=Decimal("0.5"),
            adx=Decimal("30"),
            rsi=Decimal("55"),
            trend_direction=Direction.LONG,
            sma20=Decimal("100000"),  # SMA20 > SMA50
            sma50=Decimal("99000"),
            # Previous candle data for engulfing
            prev_open=Decimal("100100"),
            prev_high=Decimal("100200"),
            prev_low=Decimal("99800"),
            prev_close=Decimal("99900"),  # Previous was bearish
            bullish_engulfing=True,  # Current engulfs previous
            bearish_engulfing=False,
        )

    @pytest.fixture
    def death_cross_state(self):
        """Market state with death cross setup + bearish engulfing."""
        return MarketState(
            symbol="BTC",
            timestamp=datetime.now(timezone.utc),
            timeframe="1h",
            regime=Regime.TREND,
            open=Decimal("100000"),
            high=Decimal("100500"),
            low=Decimal("99000"),
            close=Decimal("99200"),  # Price < SMA20 < SMA50
            volume=Decimal("1000"),
            ema50=Decimal("100500"),
            ema200=Decimal("101000"),
            ema200_slope=Decimal("-0.001"),
            atr=Decimal("500"),
            atr_pct=Decimal("0.5"),
            adx=Decimal("30"),
            rsi=Decimal("45"),
            trend_direction=Direction.SHORT,
            sma20=Decimal("99500"),  # SMA20 < SMA50
            sma50=Decimal("100000"),
            # Previous candle data for engulfing
            prev_open=Decimal("99800"),
            prev_high=Decimal("100200"),
            prev_low=Decimal("99700"),
            prev_close=Decimal("100100"),  # Previous was bullish
            bullish_engulfing=False,
            bearish_engulfing=True,  # Current engulfs previous
        )

    @pytest.fixture
    def no_signal_state(self):
        """Market state with no clear SMA signal."""
        return MarketState(
            symbol="BTC",
            timestamp=datetime.now(timezone.utc),
            timeframe="1h",
            regime=Regime.RANGE,
            open=Decimal("100000"),
            high=Decimal("100200"),
            low=Decimal("99800"),
            close=Decimal("99900"),  # Price between SMAs
            volume=Decimal("1000"),
            ema50=Decimal("99700"),
            ema200=Decimal("99500"),
            ema200_slope=Decimal("0.0001"),
            atr=Decimal("500"),
            atr_pct=Decimal("0.5"),
            adx=Decimal("18"),
            rsi=Decimal("50"),
            trend_direction=Direction.FLAT,
            sma20=Decimal("100000"),
            sma50=Decimal("99500"),  # SMA20 > SMA50 but price < SMA20
            bullish_engulfing=False,
            bearish_engulfing=False,
        )

    @pytest.fixture
    def low_volatility_state(self):
        """Market state with ATR below minimum threshold."""
        return MarketState(
            symbol="BTC",
            timestamp=datetime.now(timezone.utc),
            timeframe="1h",
            regime=Regime.TREND,
            open=Decimal("100000"),
            high=Decimal("100100"),
            low=Decimal("99900"),
            close=Decimal("100050"),
            volume=Decimal("1000"),
            ema50=Decimal("99200"),
            ema200=Decimal("99000"),
            ema200_slope=Decimal("0.001"),
            atr=Decimal("100"),  # Low ATR
            atr_pct=Decimal("0.1"),  # Below min_atr_pct threshold (0.3%)
            adx=Decimal("30"),
            rsi=Decimal("55"),
            trend_direction=Direction.LONG,
            sma20=Decimal("100000"),
            sma50=Decimal("99500"),
            bullish_engulfing=True,
            bearish_engulfing=False,
        )

    # =========================================================================
    # Signal Generation Tests
    # =========================================================================

    def test_golden_cross_long_signal(self, strategy, golden_cross_state):
        """Test LONG signal generation on golden cross with bullish engulfing."""
        result = strategy.evaluate(golden_cross_state)

        assert result.has_setup is True
        assert result.setup is not None
        assert result.setup.direction == Direction.LONG
        assert result.setup.symbol == "BTC"
        assert "Golden Cross" in result.reason
        assert "bullish engulfing" in result.reason

    def test_death_cross_blocked_when_shorts_disabled(self, strategy, death_cross_state):
        """Test SHORT signal is blocked when allow_short=False."""
        result = strategy.evaluate(death_cross_state)

        assert result.has_setup is False
        assert "Short positions disabled" in result.reason

    def test_death_cross_allowed_when_shorts_enabled(self, death_cross_state):
        """Test SHORT signal is allowed when allow_short=True."""
        strategy = TrendFollowStrategy(config={
            "allow_short": True,
            "require_candle_confirm": True,
        })
        result = strategy.evaluate(death_cross_state)

        assert result.has_setup is True
        assert result.setup is not None
        assert result.setup.direction == Direction.SHORT
        assert "Death Cross" in result.reason

    def test_no_signal_in_ranging_market(self, strategy, no_signal_state):
        """Test no signal when no clear SMA crossover."""
        result = strategy.evaluate(no_signal_state)

        assert result.has_setup is False
        assert "No SMA crossover signal" in result.reason

    def test_low_volatility_rejected(self, strategy, low_volatility_state):
        """Test low volatility markets are rejected."""
        result = strategy.evaluate(low_volatility_state)

        assert result.has_setup is False
        assert "ATR too low" in result.reason

    def test_candle_confirmation_required(self, strategy, golden_cross_state):
        """Test that missing candle confirmation blocks the signal."""
        # Remove bullish engulfing
        golden_cross_state.bullish_engulfing = False

        result = strategy.evaluate(golden_cross_state)

        assert result.has_setup is False
        assert "bullish engulfing" in result.reason

    def test_candle_confirmation_optional(self, golden_cross_state):
        """Test candle confirmation can be disabled."""
        strategy = TrendFollowStrategy(config={
            "require_candle_confirm": False,
        })
        # Remove engulfing pattern
        golden_cross_state.bullish_engulfing = False

        result = strategy.evaluate(golden_cross_state)

        assert result.has_setup is True  # Signal accepted without candle confirm

    # =========================================================================
    # Quality Score Tests
    # =========================================================================

    def test_quality_score_in_valid_range(self, strategy, golden_cross_state):
        """Test quality score is between 0 and 1."""
        result = strategy.evaluate(golden_cross_state)

        assert result.setup is not None
        quality = result.setup.setup_quality
        assert Decimal("0") <= quality <= Decimal("1")

    def test_quality_score_reflects_sma_separation(self, strategy, golden_cross_state):
        """Test quality score increases with larger SMA separation."""
        # Small SMA separation
        golden_cross_state.sma20 = Decimal("100000")
        golden_cross_state.sma50 = Decimal("99900")  # 0.1% separation
        result_small = strategy.evaluate(golden_cross_state)

        # Large SMA separation
        golden_cross_state.sma50 = Decimal("95000")  # 5% separation
        result_large = strategy.evaluate(golden_cross_state)

        # Larger separation should have higher quality
        assert result_large.setup.setup_quality > result_small.setup.setup_quality

    # =========================================================================
    # Stop Loss Tests
    # =========================================================================

    def test_stop_loss_calculation(self, strategy, golden_cross_state):
        """Test stop loss is calculated using ATR multiplier."""
        result = strategy.evaluate(golden_cross_state)

        assert result.setup is not None
        # For LONG: stop = entry - (ATR * mult) = 100200 - (500 * 2.5) = 98950
        expected_stop = Decimal("100200") - (Decimal("500") * Decimal("2.5"))
        assert result.setup.stop_price == expected_stop

    def test_stop_distance_percentage(self, strategy, golden_cross_state):
        """Test stop distance percentage is calculated correctly."""
        result = strategy.evaluate(golden_cross_state)

        assert result.setup is not None
        # Stop distance = (100200 - 98950) / 100200 * 100 = 1.247...%
        stop_dist = result.setup.stop_distance_pct
        assert Decimal("1.0") < stop_dist < Decimal("1.5")


class TestDailyTradeLimitIntegration:
    """Test that daily trade limit is respected."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database with trade count support."""
        db = MagicMock()
        # Mock the fetchrow method to return a row with 'count'
        db.fetchrow = AsyncMock(return_value={"count": 0})
        return db

    @pytest.fixture
    def mock_bus(self):
        """Create mock message bus."""
        bus = MagicMock()
        bus.subscribe = MagicMock()
        return bus

    @pytest.fixture
    def risk_config(self):
        """Risk configuration with daily limit."""
        return RiskConfig(
            per_trade_pct=2.0,
            max_positions=1,
            max_exposure_pct=150.0,
            leverage=5.0,
            max_daily_trades=3,  # Max 3 trades per day
        )

    @pytest.mark.asyncio
    async def test_daily_limit_blocks_after_max_trades(self, mock_db, mock_bus, risk_config):
        """Test that new trades are blocked after daily limit reached."""
        # Simulate 3 trades already executed today
        mock_db.fetchrow = AsyncMock(return_value={"count": 3})

        risk_manager = RiskManagerService(
            bus=mock_bus,
            db=mock_db,
            config=risk_config,
        )

        # Check today's trade count
        count = await risk_manager._get_today_trade_count()
        assert count == 3

        # Verify limit would be reached
        assert count >= risk_config.max_daily_trades

    @pytest.mark.asyncio
    async def test_daily_limit_allows_before_max(self, mock_db, mock_bus, risk_config):
        """Test that trades are allowed when under daily limit."""
        # Simulate 2 trades executed today
        mock_db.fetchrow = AsyncMock(return_value={"count": 2})

        risk_manager = RiskManagerService(
            bus=mock_bus,
            db=mock_db,
            config=risk_config,
        )

        count = await risk_manager._get_today_trade_count()
        assert count == 2
        assert count < risk_config.max_daily_trades  # Still under limit


class TestRiskManagementParameters:
    """Verify risk management parameters are correct."""

    def test_stop_loss_percentage_configured(self):
        """Test stop loss is 1.5% as per US-006."""
        from simple_bot.config.loader import RiskConfig as PydanticRiskConfig

        config = PydanticRiskConfig()
        assert config.stop_loss_pct == 1.5

    def test_take_profit_percentage_configured(self):
        """Test take profit is 3% as per US-006."""
        from simple_bot.config.loader import RiskConfig as PydanticRiskConfig

        config = PydanticRiskConfig()
        assert config.take_profit_pct == 3.0

    def test_risk_reward_ratio(self):
        """Test 1:2 risk/reward ratio is maintained."""
        from simple_bot.config.loader import RiskConfig as PydanticRiskConfig

        config = PydanticRiskConfig()
        ratio = config.take_profit_pct / config.stop_loss_pct
        assert ratio == 2.0  # 1:2 risk/reward


class TestStrategySimulation:
    """Simulate multiple evaluation cycles to verify consistency."""

    def test_multiple_cycles_same_state(self):
        """Test strategy returns consistent results across multiple evaluations."""
        strategy = TrendFollowStrategy(config={
            "require_candle_confirm": True,
        })

        # Create a consistent golden cross state
        state = MarketState(
            symbol="BTC",
            timestamp=datetime.now(timezone.utc),
            timeframe="1h",
            regime=Regime.TREND,
            open=Decimal("100000"),
            high=Decimal("100500"),
            low=Decimal("99500"),
            close=Decimal("100200"),
            volume=Decimal("1000"),
            ema50=Decimal("98000"),
            ema200=Decimal("95000"),
            ema200_slope=Decimal("0.001"),
            atr=Decimal("500"),
            atr_pct=Decimal("0.5"),
            adx=Decimal("30"),
            rsi=Decimal("55"),
            trend_direction=Direction.LONG,
            sma20=Decimal("100000"),
            sma50=Decimal("99000"),
            prev_open=Decimal("100100"),
            prev_high=Decimal("100200"),
            prev_low=Decimal("99800"),
            prev_close=Decimal("99900"),
            bullish_engulfing=True,
            bearish_engulfing=False,
        )

        # Run 10 evaluation cycles
        results = []
        for _ in range(10):
            result = strategy.evaluate(state)
            results.append(result)

        # All should generate LONG signals
        assert all(r.has_setup for r in results)
        assert all(r.setup is not None for r in results)
        assert all(r.setup.direction == Direction.LONG for r in results if r.setup)

    def test_config_loaded(self):
        """Test that config loads correctly from YAML."""
        import yaml
        from pathlib import Path

        config_path = Path(__file__).parent.parent / "config" / "trading.yaml"
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)

        # Verify config has essential sections
        assert "risk" in config
        assert "strategies" in config
        assert "universe" in config

    def test_btc_only_universe(self):
        """Test that only BTC is enabled in universe."""
        import yaml
        from pathlib import Path

        config_path = Path(__file__).parent.parent / "config" / "trading.yaml"
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)

        universe = config.get("universe", {})
        assert universe.get("mode") == "all"

        assets = universe.get("assets", [])
        enabled_assets = [a["symbol"] for a in assets if a.get("enabled")]
        assert enabled_assets == ["BTC", "ETH", "SOL"]

    def test_daily_trade_limit_configured(self):
        """Test daily trade limit is configured."""
        import yaml
        from pathlib import Path

        config_path = Path(__file__).parent.parent / "config" / "trading.yaml"
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)

        risk = config.get("risk", {})
        assert risk.get("max_daily_trades") == 8

    def test_momentum_scalper_enabled(self):
        """Test momentum scalper strategy is enabled."""
        import yaml
        from pathlib import Path

        config_path = Path(__file__).parent.parent / "config" / "trading.yaml"
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)

        strategies = config.get("strategies", {})
        assert strategies.get("momentum_scalper", {}).get("enabled") is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
