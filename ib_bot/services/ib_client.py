"""
IB Client Service
=================

Wrapper around ib_insync for connecting to Interactive Brokers.
Handles: connection, reconnection, contract qualification, historical data,
order placement (bracket orders), position tracking, and EOD flatten.
"""

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from ib_insync import IB, Contract, Future, MarketOrder, LimitOrder, Order, Trade
from ib_insync import util

from ..config.loader import IBConnectionConfig
from ..core.contracts import CONTRACTS, FuturesSpec
from ..core.enums import Direction

logger = logging.getLogger(__name__)


def _front_month_expiry() -> str:
    """Calculate front-month futures expiry (YYYYMM format).

    Futures roll quarterly: Mar(H), Jun(M), Sep(U), Dec(Z).
    Switch to next quarter when within 2 weeks of expiry.
    """
    now = datetime.now(timezone.utc)
    year = now.year
    month = now.month

    # Quarterly months
    quarters = [3, 6, 9, 12]
    for q in quarters:
        if month < q:
            return f"{year}{q:02d}"
        if month == q and now.day <= 14:
            return f"{year}{q:02d}"

    # Roll to next year Q1
    return f"{year + 1}03"


class IBClient:
    """Async wrapper for Interactive Brokers connection via ib_insync."""

    def __init__(self, config: IBConnectionConfig) -> None:
        self._config = config
        self._ib = IB()
        self._qualified_contracts: Dict[str, Contract] = {}
        self._connected = False
        self._reconnect_count = 0

        # Register disconnect handler
        self._ib.disconnectedEvent += self._on_disconnect

    async def connect(self) -> None:
        """Connect to TWS/IB Gateway with retry logic."""
        for attempt in range(self._config.max_reconnect_attempts):
            try:
                logger.info(
                    "Connecting to IB: %s:%d (client_id=%d, attempt=%d)",
                    self._config.host,
                    self._config.port,
                    self._config.client_id,
                    attempt + 1,
                )
                await self._ib.connectAsync(
                    host=self._config.host,
                    port=self._config.port,
                    clientId=self._config.client_id,
                    timeout=self._config.timeout,
                    readonly=self._config.readonly,
                )
                self._connected = True
                self._reconnect_count = 0
                logger.info("Connected to IB successfully")
                return
            except Exception as e:
                logger.warning(
                    "Connection attempt %d failed: %s", attempt + 1, e
                )
                if attempt < self._config.max_reconnect_attempts - 1:
                    delay = self._config.reconnect_delay * (2 ** min(attempt, 4))
                    logger.info("Retrying in %d seconds...", delay)
                    await asyncio.sleep(delay)

        raise ConnectionError(
            f"Failed to connect after {self._config.max_reconnect_attempts} attempts"
        )

    async def disconnect(self) -> None:
        """Disconnect from IB."""
        if self._connected:
            self._ib.disconnect()
            self._connected = False
            logger.info("Disconnected from IB")

    def _on_disconnect(self) -> None:
        """Handle unexpected disconnection."""
        self._connected = False
        logger.warning("Disconnected from IB unexpectedly")

    async def reconnect(self) -> None:
        """Reconnect and re-qualify contracts."""
        self._reconnect_count += 1
        logger.info("Reconnecting (attempt %d)...", self._reconnect_count)
        await self.connect()

        # Re-qualify existing contracts
        symbols = list(self._qualified_contracts.keys())
        self._qualified_contracts.clear()
        for symbol in symbols:
            await self.qualify_contract(symbol)

    # =========================================================================
    # Contract Management
    # =========================================================================

    async def qualify_contract(self, symbol: str) -> Contract:
        """Qualify a futures contract with IB.

        Args:
            symbol: Contract symbol (e.g., ES, MES)

        Returns:
            Qualified IB Contract object
        """
        if symbol in self._qualified_contracts:
            return self._qualified_contracts[symbol]

        spec = CONTRACTS.get(symbol)
        if not spec:
            raise ValueError(f"Unknown contract: {symbol}. Available: {list(CONTRACTS.keys())}")

        expiry = _front_month_expiry()
        contract = Future(
            symbol=spec.symbol,
            lastTradeDateOrContractMonth=expiry,
            exchange=spec.exchange,
            currency=spec.currency,
        )

        qualified = await self._ib.qualifyContractsAsync(contract)
        if not qualified:
            raise ValueError(f"Could not qualify contract: {symbol} {expiry}")

        self._qualified_contracts[symbol] = qualified[0]
        logger.info("Qualified contract: %s %s on %s", symbol, expiry, spec.exchange)
        return qualified[0]

    def get_spec(self, symbol: str) -> FuturesSpec:
        """Get futures specification for a symbol."""
        spec = CONTRACTS.get(symbol)
        if not spec:
            raise ValueError(f"Unknown contract: {symbol}")
        return spec

    # =========================================================================
    # Market Data
    # =========================================================================

    async def request_historical_bars(
        self,
        symbol: str,
        duration: str = "1 D",
        bar_size: str = "1 min",
        what_to_show: str = "TRADES",
        keep_up_to_date: bool = True,
    ) -> List[Any]:
        """Request historical bars with optional live updates.

        Args:
            symbol: Contract symbol
            duration: How far back (e.g., "1 D", "2 D")
            bar_size: Bar interval (e.g., "1 min", "5 mins")
            what_to_show: Data type (TRADES, MIDPOINT, etc.)
            keep_up_to_date: Continue streaming live bars

        Returns:
            List of BarData objects
        """
        contract = await self.qualify_contract(symbol)
        bars = await self._ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=True,
            keepUpToDate=keep_up_to_date,
        )
        logger.info("Received %d bars for %s", len(bars), symbol)
        return bars

    def cancel_historical_data(self, bars: Any) -> None:
        """Cancel a streaming historical data subscription."""
        if bars:
            self._ib.cancelHistoricalData(bars)

    # =========================================================================
    # Order Management
    # =========================================================================

    async def place_bracket_order(
        self,
        symbol: str,
        direction: Direction,
        contracts: int,
        entry_price: Decimal,
        stop_price: Decimal,
        target_price: Decimal,
    ) -> List[Trade]:
        """Place a bracket order (entry + TP + SL as OCA group).

        IB handles OCA atomically — when one fills, the other cancels.

        Args:
            symbol: Contract symbol
            direction: LONG or SHORT
            contracts: Number of contracts
            entry_price: Limit entry price
            stop_price: Stop loss price
            target_price: Take profit price

        Returns:
            List of Trade objects [entry, take_profit, stop_loss]
        """
        contract = await self.qualify_contract(symbol)
        action = "BUY" if direction == Direction.LONG else "SELL"
        reverse_action = "SELL" if direction == Direction.LONG else "BUY"

        bracket = self._ib.bracketOrder(
            action=action,
            quantity=contracts,
            limitPrice=float(entry_price),
            takeProfitPrice=float(target_price),
            stopLossPrice=float(stop_price),
        )

        trades = []
        for order in bracket:
            trade = self._ib.placeOrder(contract, order)
            trades.append(trade)

        logger.info(
            "Bracket order placed: %s %s x%d @ %.2f, SL=%.2f, TP=%.2f",
            action, symbol, contracts,
            float(entry_price), float(stop_price), float(target_price),
        )
        return trades

    async def place_market_order(
        self,
        symbol: str,
        direction: Direction,
        contracts: int,
    ) -> Trade:
        """Place a market order (used for EOD flatten)."""
        contract = await self.qualify_contract(symbol)
        action = "BUY" if direction == Direction.LONG else "SELL"
        order = MarketOrder(action=action, totalQuantity=contracts)
        trade = self._ib.placeOrder(contract, order)
        logger.info("Market order: %s %s x%d", action, symbol, contracts)
        return trade

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        """Cancel all open orders, optionally filtered by symbol."""
        cancelled = 0
        for trade in self._ib.openTrades():
            if symbol and trade.contract.symbol != symbol:
                continue
            self._ib.cancelOrder(trade.order)
            cancelled += 1

        logger.info("Cancelled %d orders%s", cancelled, f" for {symbol}" if symbol else "")
        return cancelled

    # =========================================================================
    # Position Management
    # =========================================================================

    def get_positions(self) -> List[Any]:
        """Get all current positions."""
        return self._ib.positions()

    def get_portfolio(self) -> List[Any]:
        """Get portfolio items with P&L."""
        return self._ib.portfolio()

    async def flatten_position(self, symbol: str) -> Optional[Trade]:
        """Close entire position for a symbol with a market order."""
        for pos in self._ib.positions():
            if pos.contract.symbol == symbol and pos.position != 0:
                direction = Direction.SHORT if pos.position > 0 else Direction.LONG
                qty = abs(int(pos.position))
                trade = await self.place_market_order(symbol, direction, qty)
                logger.info("Flattened %s: %d contracts", symbol, qty)
                return trade
        return None

    async def flatten_all(self) -> List[Trade]:
        """Flatten all open positions (EOD)."""
        trades = []
        for pos in self._ib.positions():
            if pos.position != 0:
                symbol = pos.contract.symbol
                trade = await self.flatten_position(symbol)
                if trade:
                    trades.append(trade)
        logger.info("Flattened all positions (%d trades)", len(trades))
        return trades

    # =========================================================================
    # Properties
    # =========================================================================

    @property
    def connected(self) -> bool:
        return self._connected and self._ib.isConnected()

    @property
    def ib(self) -> IB:
        """Direct access to ib_insync IB instance for advanced use."""
        return self._ib
