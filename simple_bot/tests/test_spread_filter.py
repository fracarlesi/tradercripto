"""
Tests for bid-ask spread filter
================================

Tests for the pre-trade spread filter that prevents entering trades
on illiquid assets with wide bid-ask spreads.

Run:
    pytest simple_bot/tests/test_spread_filter.py -v
"""

import asyncio
import pytest
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from simple_bot.api.hyperliquid import HyperliquidClient
from simple_bot.core.models import (
    MarketState,
    Setup,
    Regime,
    Direction,
    SetupType,
)
from simple_bot.core.enums import Topic
from simple_bot.main import ConservativeBot, ConservativeConfig


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_client() -> HyperliquidClient:
    """Create a mock HyperliquidClient with connection state."""
    client = MagicMock(spec=HyperliquidClient)
    client._connected = True
    client._info = MagicMock()
    client._rate_limiter = MagicMock()
    client._rate_limiter.info = MagicMock()
    client._rate_limiter.info.__aenter__ = AsyncMock()
    client._rate_limiter.info.__aexit__ = AsyncMock()
    return client


@pytest.fixture
def tight_spread_l2() -> dict:
    """L2 snapshot with tight spread (BTC-like, ~0.01%)."""
    return {
        "levels": [
            [{"px": "95000.0", "sz": "1.5"}, {"px": "94999.0", "sz": "2.0"}],
            [{"px": "95010.0", "sz": "1.2"}, {"px": "95011.0", "sz": "1.8"}],
        ]
    }


@pytest.fixture
def wide_spread_l2() -> dict:
    """L2 snapshot with wide spread (illiquid asset, ~0.5%)."""
    return {
        "levels": [
            [{"px": "1.000", "sz": "5000"}, {"px": "0.999", "sz": "3000"}],
            [{"px": "1.005", "sz": "4000"}, {"px": "1.006", "sz": "2000"}],
        ]
    }


@pytest.fixture
def empty_l2() -> dict:
    """L2 snapshot with no levels."""
    return {"levels": [[], []]}


@pytest.fixture
def market_state_trend() -> MarketState:
    """Market state in TREND regime for testing evaluate_asset."""
    return MarketState(
        symbol="BTC",
        timeframe="15m",
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
        sma20=Decimal("95000"),
        sma50=Decimal("94000"),
        prev_open=Decimal("95500"),
        prev_high=Decimal("95600"),
        prev_low=Decimal("94800"),
        prev_close=Decimal("94900"),
        bullish_engulfing=False,
        bearish_engulfing=False,
        regime=Regime.TREND,
        trend_direction=Direction.LONG,
    )


# =============================================================================
# Tests for HyperliquidClient.get_spread_pct
# =============================================================================

class TestGetSpreadPct:
    """Tests for the get_spread_pct method on HyperliquidClient."""

    @pytest.mark.asyncio
    async def test_tight_spread_calculation(self, tight_spread_l2: dict) -> None:
        """Tight spread (BTC-like) returns correct small percentage."""
        client = HyperliquidClient.__new__(HyperliquidClient)
        client._connected = True
        client._info = MagicMock()
        client._info.l2_snapshot = MagicMock(return_value=tight_spread_l2)
        client._rate_limiter = MagicMock()
        client._rate_limiter.info = MagicMock()
        client._rate_limiter.info.__aenter__ = AsyncMock()
        client._rate_limiter.info.__aexit__ = AsyncMock()

        spread = await client.get_spread_pct("BTC")

        # bid=95000, ask=95010, mid=95005, spread=10/95005*100 ~= 0.01053%
        assert spread > 0
        assert spread < 0.02  # Should be around 0.0105%
        expected = float((Decimal("95010") - Decimal("95000")) / Decimal("95005") * 100)
        assert abs(spread - expected) < 0.0001

    @pytest.mark.asyncio
    async def test_wide_spread_calculation(self, wide_spread_l2: dict) -> None:
        """Wide spread (illiquid asset) returns correct large percentage."""
        client = HyperliquidClient.__new__(HyperliquidClient)
        client._connected = True
        client._info = MagicMock()
        client._info.l2_snapshot = MagicMock(return_value=wide_spread_l2)
        client._rate_limiter = MagicMock()
        client._rate_limiter.info = MagicMock()
        client._rate_limiter.info.__aenter__ = AsyncMock()
        client._rate_limiter.info.__aexit__ = AsyncMock()

        spread = await client.get_spread_pct("ILLIQUID")

        # bid=1.000, ask=1.005, mid=1.0025, spread=0.005/1.0025*100 ~= 0.499%
        assert spread > 0.4
        assert spread < 0.6

    @pytest.mark.asyncio
    async def test_empty_orderbook_returns_zero(self, empty_l2: dict) -> None:
        """Empty orderbook (no bids/asks) returns 0.0 (fail-open)."""
        client = HyperliquidClient.__new__(HyperliquidClient)
        client._connected = True
        client._info = MagicMock()
        client._info.l2_snapshot = MagicMock(return_value=empty_l2)
        client._rate_limiter = MagicMock()
        client._rate_limiter.info = MagicMock()
        client._rate_limiter.info.__aenter__ = AsyncMock()
        client._rate_limiter.info.__aexit__ = AsyncMock()

        spread = await client.get_spread_pct("EMPTY")

        assert spread == 0.0

    @pytest.mark.asyncio
    async def test_api_error_returns_zero(self) -> None:
        """API error returns 0.0 (fail-open, don't block trades)."""
        client = HyperliquidClient.__new__(HyperliquidClient)
        client._connected = True
        client._info = MagicMock()
        client._info.l2_snapshot = MagicMock(side_effect=Exception("API timeout"))
        client._rate_limiter = MagicMock()
        client._rate_limiter.info = MagicMock()
        client._rate_limiter.info.__aenter__ = AsyncMock()
        client._rate_limiter.info.__aexit__ = AsyncMock()

        spread = await client.get_spread_pct("BTC")

        assert spread == 0.0

    @pytest.mark.asyncio
    async def test_not_connected_returns_zero(self) -> None:
        """Not connected returns 0.0 (fail-open)."""
        client = HyperliquidClient.__new__(HyperliquidClient)
        client._connected = False
        client._info = None
        client._rate_limiter = MagicMock()

        spread = await client.get_spread_pct("BTC")

        assert spread == 0.0

    @pytest.mark.asyncio
    async def test_zero_price_returns_zero(self) -> None:
        """Zero bid or ask price returns 0.0 (fail-open)."""
        l2 = {
            "levels": [
                [{"px": "0.0", "sz": "100"}],
                [{"px": "1.005", "sz": "100"}],
            ]
        }
        client = HyperliquidClient.__new__(HyperliquidClient)
        client._connected = True
        client._info = MagicMock()
        client._info.l2_snapshot = MagicMock(return_value=l2)
        client._rate_limiter = MagicMock()
        client._rate_limiter.info = MagicMock()
        client._rate_limiter.info.__aenter__ = AsyncMock()
        client._rate_limiter.info.__aexit__ = AsyncMock()

        spread = await client.get_spread_pct("WEIRD")

        assert spread == 0.0


# =============================================================================
# Tests for spread filter in _evaluate_asset
# =============================================================================

class TestSpreadFilterInEvaluateAsset:
    """Tests for the spread filter integration in _evaluate_asset."""

    def _make_bot(self, max_spread_pct: float = 0.10) -> ConservativeBot:
        """Create a ConservativeBot with mocked dependencies for testing."""
        bot = ConservativeBot.__new__(ConservativeBot)
        bot._config = MagicMock(spec=ConservativeConfig)
        bot._config.max_positions = 3
        bot._config.max_spread_pct = max_spread_pct
        bot._exchange = AsyncMock(spec=HyperliquidClient)
        bot._bus = AsyncMock(spec_set=["publish"])
        bot._bus.publish = AsyncMock()
        bot._strategies = []
        bot._services = {}
        bot._outcome_tracker = None
        return bot

    def _make_strategy(self, setup: Setup) -> MagicMock:
        """Create a mock strategy that returns a setup."""
        strategy = MagicMock()
        strategy.name = "test_strategy"
        strategy.can_trade.return_value = True
        result = MagicMock()
        result.has_setup = True
        result.setup = setup
        strategy.evaluate.return_value = result
        return strategy

    def _make_setup(self, symbol: str = "BTC") -> Setup:
        """Create a minimal Setup for testing."""
        return Setup(
            id="test-setup-001",
            symbol=symbol,
            timestamp=datetime.now(timezone.utc),
            direction=Direction.LONG,
            setup_type=SetupType.MOMENTUM,
            entry_price=Decimal("95000"),
            stop_price=Decimal("94240"),
            stop_distance_pct=Decimal("0.8"),
            atr=Decimal("500"),
            adx=Decimal("35"),
            rsi=Decimal("55"),
            regime=Regime.TREND,
            confidence=Decimal("0.8"),
        )

    @pytest.mark.asyncio
    async def test_narrow_spread_allows_trade(
        self, market_state_trend: MarketState,
    ) -> None:
        """Asset with spread below threshold passes the filter."""
        bot = self._make_bot(max_spread_pct=0.10)
        setup = self._make_setup("BTC")
        bot._strategies = [self._make_strategy(setup)]
        bot._exchange.get_spread_pct = AsyncMock(return_value=0.02)  # 0.02% < 0.10%

        await bot._evaluate_asset(market_state_trend)

        # Setup should be published (not filtered out)
        bot._bus.publish.assert_called_once()
        call_args = bot._bus.publish.call_args
        assert call_args[0][0] == Topic.SETUPS

    @pytest.mark.asyncio
    async def test_wide_spread_blocks_trade(
        self, market_state_trend: MarketState,
    ) -> None:
        """Asset with spread above threshold is skipped."""
        bot = self._make_bot(max_spread_pct=0.10)
        setup = self._make_setup("BTC")
        bot._strategies = [self._make_strategy(setup)]
        bot._exchange.get_spread_pct = AsyncMock(return_value=0.25)  # 0.25% > 0.10%

        await bot._evaluate_asset(market_state_trend)

        # Setup should NOT be published
        bot._bus.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_spread_at_threshold_passes(
        self, market_state_trend: MarketState,
    ) -> None:
        """Asset with spread exactly at threshold passes (not strictly greater)."""
        bot = self._make_bot(max_spread_pct=0.10)
        setup = self._make_setup("BTC")
        bot._strategies = [self._make_strategy(setup)]
        bot._exchange.get_spread_pct = AsyncMock(return_value=0.10)  # exactly at threshold

        await bot._evaluate_asset(market_state_trend)

        # Exactly at threshold should pass (> not >=)
        bot._bus.publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_spread_api_error_allows_trade(
        self, market_state_trend: MarketState,
    ) -> None:
        """API error returns 0.0 which is below threshold (fail-open)."""
        bot = self._make_bot(max_spread_pct=0.10)
        setup = self._make_setup("BTC")
        bot._strategies = [self._make_strategy(setup)]
        # get_spread_pct returns 0.0 on error
        bot._exchange.get_spread_pct = AsyncMock(return_value=0.0)

        await bot._evaluate_asset(market_state_trend)

        # Should pass through (fail-open)
        bot._bus.publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_exchange_allows_trade(
        self, market_state_trend: MarketState,
    ) -> None:
        """When exchange is None, spread check is skipped (e.g., dry run)."""
        bot = self._make_bot(max_spread_pct=0.10)
        setup = self._make_setup("BTC")
        bot._strategies = [self._make_strategy(setup)]
        bot._exchange = None  # No exchange client

        await bot._evaluate_asset(market_state_trend)

        # Should pass through without spread check
        bot._bus.publish.assert_called_once()
