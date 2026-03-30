"""Tests for stock/ETF bracket order execution via IBClient."""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from ib_bot.core.enums import Direction
from ib_bot.config.loader import IBConnectionConfig


class FakeStock:
    """Minimal fake Stock contract for testing."""

    def __init__(self, symbol: str, exchange: str, currency: str) -> None:
        self.symbol = symbol
        self.exchange = exchange
        self.currency = currency


class FakeTrade:
    """Minimal fake Trade for testing."""

    def __init__(self, order_id: int = 1) -> None:
        self.order = MagicMock()
        self.order.orderId = order_id


class FakeOrder:
    """Minimal fake bracket order leg."""

    def __init__(self, action: str, order_type: str) -> None:
        self.action = action
        self.orderType = order_type


@pytest.fixture
def ib_config() -> IBConnectionConfig:
    return IBConnectionConfig(
        host="127.0.0.1",
        port=7497,
        client_id=99,
        readonly=True,
    )


@pytest.fixture
def mock_ib_client(ib_config: IBConnectionConfig):
    """Create an IBClient with a mocked IB connection."""
    from ib_bot.services.ib_client import IBClient

    client = IBClient(ib_config)

    # Mock the internal IB instance
    mock_ib = MagicMock()
    client._ib = mock_ib
    client._connected = True

    return client, mock_ib


class TestQualifyStock:
    """Test stock contract qualification."""

    @pytest.mark.asyncio
    async def test_qualify_stock_success(self, mock_ib_client) -> None:
        """Successfully qualify a stock contract."""
        client, mock_ib = mock_ib_client

        fake_contract = FakeStock("AAPL", "SMART", "USD")
        mock_ib.qualifyContractsAsync = AsyncMock(return_value=[fake_contract])

        result = await client.qualify_stock("AAPL")
        assert result.symbol == "AAPL"
        mock_ib.qualifyContractsAsync.assert_called_once()

    @pytest.mark.asyncio
    async def test_qualify_stock_cached(self, mock_ib_client) -> None:
        """Second call returns cached contract, no IB call."""
        client, mock_ib = mock_ib_client

        fake_contract = FakeStock("AAPL", "SMART", "USD")
        mock_ib.qualifyContractsAsync = AsyncMock(return_value=[fake_contract])

        await client.qualify_stock("AAPL")
        await client.qualify_stock("AAPL")

        # Only called once due to caching
        assert mock_ib.qualifyContractsAsync.call_count == 1

    @pytest.mark.asyncio
    async def test_qualify_stock_custom_exchange(self, mock_ib_client) -> None:
        """Qualify on a specific exchange."""
        client, mock_ib = mock_ib_client

        fake_contract = FakeStock("AAPL", "NASDAQ", "USD")
        mock_ib.qualifyContractsAsync = AsyncMock(return_value=[fake_contract])

        result = await client.qualify_stock("AAPL", exchange="NASDAQ")
        assert result.exchange == "NASDAQ"

    @pytest.mark.asyncio
    async def test_qualify_stock_failure(self, mock_ib_client) -> None:
        """Raise ValueError when qualification fails."""
        client, mock_ib = mock_ib_client

        mock_ib.qualifyContractsAsync = AsyncMock(return_value=[])

        with pytest.raises(ValueError, match="Could not qualify stock"):
            await client.qualify_stock("INVALID")

    @pytest.mark.asyncio
    async def test_different_symbols_cached_separately(self, mock_ib_client) -> None:
        """Different symbols get separate cache entries."""
        client, mock_ib = mock_ib_client

        fake_aapl = FakeStock("AAPL", "SMART", "USD")
        fake_spy = FakeStock("SPY", "SMART", "USD")
        mock_ib.qualifyContractsAsync = AsyncMock(side_effect=[[fake_aapl], [fake_spy]])

        result1 = await client.qualify_stock("AAPL")
        result2 = await client.qualify_stock("SPY")

        assert result1.symbol == "AAPL"
        assert result2.symbol == "SPY"
        assert mock_ib.qualifyContractsAsync.call_count == 2


class TestPlaceStockBracketOrder:
    """Test stock bracket order placement."""

    @pytest.mark.asyncio
    async def test_long_bracket_order(self, mock_ib_client) -> None:
        """Place a long bracket order for a stock."""
        client, mock_ib = mock_ib_client

        fake_contract = FakeStock("AAPL", "SMART", "USD")
        mock_ib.qualifyContractsAsync = AsyncMock(return_value=[fake_contract])

        # bracketOrder returns 3 order objects (entry, TP, SL)
        fake_orders = [
            FakeOrder("BUY", "LMT"),
            FakeOrder("SELL", "LMT"),
            FakeOrder("SELL", "STP"),
        ]
        mock_ib.bracketOrder = MagicMock(return_value=fake_orders)
        mock_ib.placeOrder = MagicMock(side_effect=[
            FakeTrade(1), FakeTrade(2), FakeTrade(3),
        ])

        trades = await client.place_stock_bracket_order(
            symbol="AAPL",
            direction=Direction.LONG,
            shares=50,
            entry_price=Decimal("175.00"),
            stop_price=Decimal("170.00"),
            target_price=Decimal("185.00"),
        )

        assert len(trades) == 3
        mock_ib.bracketOrder.assert_called_once_with(
            action="BUY",
            quantity=50,
            limitPrice=175.0,
            takeProfitPrice=185.0,
            stopLossPrice=170.0,
        )
        assert mock_ib.placeOrder.call_count == 3

    @pytest.mark.asyncio
    async def test_short_bracket_order(self, mock_ib_client) -> None:
        """Place a short bracket order for a stock."""
        client, mock_ib = mock_ib_client

        fake_contract = FakeStock("TSLA", "SMART", "USD")
        mock_ib.qualifyContractsAsync = AsyncMock(return_value=[fake_contract])

        fake_orders = [
            FakeOrder("SELL", "LMT"),
            FakeOrder("BUY", "LMT"),
            FakeOrder("BUY", "STP"),
        ]
        mock_ib.bracketOrder = MagicMock(return_value=fake_orders)
        mock_ib.placeOrder = MagicMock(side_effect=[
            FakeTrade(10), FakeTrade(11), FakeTrade(12),
        ])

        trades = await client.place_stock_bracket_order(
            symbol="TSLA",
            direction=Direction.SHORT,
            shares=20,
            entry_price=Decimal("250.00"),
            stop_price=Decimal("260.00"),
            target_price=Decimal("235.00"),
        )

        assert len(trades) == 3
        mock_ib.bracketOrder.assert_called_once_with(
            action="SELL",
            quantity=20,
            limitPrice=250.0,
            takeProfitPrice=235.0,
            stopLossPrice=260.0,
        )

    @pytest.mark.asyncio
    async def test_bracket_order_uses_cached_contract(self, mock_ib_client) -> None:
        """Second bracket order for same symbol uses cached contract."""
        client, mock_ib = mock_ib_client

        fake_contract = FakeStock("AAPL", "SMART", "USD")
        mock_ib.qualifyContractsAsync = AsyncMock(return_value=[fake_contract])
        mock_ib.bracketOrder = MagicMock(return_value=[FakeOrder("BUY", "LMT")])
        mock_ib.placeOrder = MagicMock(return_value=FakeTrade(1))

        await client.place_stock_bracket_order(
            symbol="AAPL", direction=Direction.LONG,
            shares=10, entry_price=Decimal("175.00"),
            stop_price=Decimal("170.00"), target_price=Decimal("185.00"),
        )
        await client.place_stock_bracket_order(
            symbol="AAPL", direction=Direction.LONG,
            shares=20, entry_price=Decimal("176.00"),
            stop_price=Decimal("171.00"), target_price=Decimal("186.00"),
        )

        # qualifyContractsAsync called only once (cached)
        assert mock_ib.qualifyContractsAsync.call_count == 1

    @pytest.mark.asyncio
    async def test_bracket_order_qualify_failure(self, mock_ib_client) -> None:
        """Raise when stock qualification fails during bracket order."""
        client, mock_ib = mock_ib_client

        mock_ib.qualifyContractsAsync = AsyncMock(return_value=[])

        with pytest.raises(ValueError, match="Could not qualify stock"):
            await client.place_stock_bracket_order(
                symbol="INVALID", direction=Direction.LONG,
                shares=10, entry_price=Decimal("100.00"),
                stop_price=Decimal("95.00"), target_price=Decimal("110.00"),
            )
