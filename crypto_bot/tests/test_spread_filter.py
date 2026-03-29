"""
Tests for bid-ask spread filter
================================

Tests for the pre-trade spread filter that prevents entering trades
on illiquid assets with wide bid-ask spreads.

Run:
    pytest crypto_bot/tests/test_spread_filter.py -v
"""

import asyncio
import pytest
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from crypto_bot.api.hyperliquid import HyperliquidClient
from crypto_bot.core.models import (
    MarketState,
    Setup,
    Regime,
    Direction,
    SetupType,
)
from crypto_bot.core.enums import Topic


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
