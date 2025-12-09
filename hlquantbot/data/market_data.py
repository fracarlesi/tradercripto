"""Unified Market Data Layer combining REST and WebSocket."""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Callable

from ..core.models import Tick, Bar, MarketContext, AccountState
from ..core.enums import TimeFrame
from ..core.exceptions import DataFeedError
from ..config.settings import Settings
from .rest_client import HyperliquidRestClient
from .websocket_client import HyperliquidWebSocket
from .bar_aggregator import BarAggregator


logger = logging.getLogger(__name__)


class MarketDataLayer:
    """
    Unified market data layer.

    Combines WebSocket for real-time data and REST for historical/slow data.
    Provides a single interface for all market data needs.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.symbols = settings.active_symbols

        # Clients
        self.rest = HyperliquidRestClient(settings)
        self.ws = HyperliquidWebSocket(settings)

        # Bar aggregator for custom timeframes
        self.bar_aggregator = BarAggregator(
            symbols=self.symbols,
            timeframes=[TimeFrame.M1, TimeFrame.M5, TimeFrame.M15],
        )

        # Data stores
        self._market_contexts: Dict[str, MarketContext] = {}
        self._account_state: Optional[AccountState] = None
        self._last_account_update: Optional[datetime] = None

        # Locks for thread-safe access
        self._contexts_lock = asyncio.Lock()
        self._account_lock = asyncio.Lock()

        # Update intervals
        self._context_update_interval = 5  # seconds
        self._account_update_interval = 2  # seconds

        # Background tasks
        self._tasks: List[asyncio.Task] = []
        self._running = False

        # Wire up WebSocket to bar aggregator
        self.ws.on_tick(self._on_tick)

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------
    async def start(self):
        """Start the market data layer."""
        logger.info("Starting Market Data Layer...")
        self._running = True

        # Load initial data via REST
        await self._load_initial_data()

        # Connect WebSocket
        await self.ws.connect()

        # Subscribe to all symbols
        for symbol in self.symbols:
            await self.ws.subscribe_trades(symbol)
            await self.ws.subscribe_orderbook(symbol)
            await self.ws.subscribe_candles(symbol, "1m")
            await self.ws.subscribe_candles(symbol, "5m")
            await self.ws.subscribe_candles(symbol, "15m")

        await self.ws.subscribe_all_mids()

        # Subscribe to user events
        if self.settings.hl_wallet_address:
            await self.ws.subscribe_user_events(self.settings.hl_wallet_address)

        # Start background tasks
        self._tasks.append(asyncio.create_task(self.ws.run()))
        self._tasks.append(asyncio.create_task(self._update_contexts_loop()))
        self._tasks.append(asyncio.create_task(self._update_account_loop()))

        logger.info(f"Market Data Layer started for symbols: {self.symbols}")

    async def stop(self):
        """Stop the market data layer."""
        logger.info("Stopping Market Data Layer...")
        self._running = False

        # Cancel background tasks
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Close connections
        await self.ws.disconnect()
        await self.rest.close()

        # Force close any pending bars
        self.bar_aggregator.force_close_bars()

        logger.info("Market Data Layer stopped")

    async def _load_initial_data(self):
        """Load initial data via REST."""
        logger.info("Loading initial market data...")

        # Load market contexts
        self._market_contexts = await self.rest.get_all_market_contexts(self.symbols)

        # Load account state
        self._account_state = await self.rest.get_account_state()
        self._last_account_update = datetime.now(timezone.utc)

        # Load historical bars for each symbol
        for symbol in self.symbols:
            for interval in ["1m", "5m", "15m"]:
                try:
                    bars = await self.rest.get_candles(symbol, interval)
                    # Store in aggregator
                    timeframe = TimeFrame(interval)
                    for bar in bars[-200:]:  # Last 200 bars
                        self.bar_aggregator._completed_bars[symbol][timeframe].append(bar)
                    logger.debug(f"Loaded {len(bars)} {interval} bars for {symbol}")
                except Exception as e:
                    logger.warning(f"Failed to load {interval} bars for {symbol}: {e}")

        logger.info("Initial market data loaded")

    # -------------------------------------------------------------------------
    # Background Update Loops
    # -------------------------------------------------------------------------
    async def _update_contexts_loop(self):
        """Periodically update market contexts via REST."""
        while self._running:
            try:
                await asyncio.sleep(self._context_update_interval)
                contexts = await self.rest.get_all_market_contexts(self.symbols)
                async with self._contexts_lock:
                    self._market_contexts = contexts
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error updating market contexts: {e}")

    async def _update_account_loop(self):
        """Periodically update account state via REST."""
        while self._running:
            try:
                await asyncio.sleep(self._account_update_interval)
                account_state = await self.rest.get_account_state()
                async with self._account_lock:
                    self._account_state = account_state
                    self._last_account_update = datetime.now(timezone.utc)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error updating account state: {e}")

    # -------------------------------------------------------------------------
    # Callbacks
    # -------------------------------------------------------------------------
    def _on_tick(self, tick: Tick):
        """Handle tick from WebSocket."""
        self.bar_aggregator.process_tick(tick)

    def on_bar(self, callback: Callable[[Bar], None]):
        """Register callback for new completed bars."""
        self.bar_aggregator.on_bar_complete(callback)

    def on_tick(self, callback: Callable[[Tick], None]):
        """Register callback for tick updates."""
        self.ws.on_tick(callback)

    def on_fill(self, callback: Callable[[Dict], None]):
        """Register callback for order fill events."""
        self.ws.on_fill(callback)

    # -------------------------------------------------------------------------
    # Data Access - Real-time
    # -------------------------------------------------------------------------
    def get_tick(self, symbol: str) -> Optional[Tick]:
        """Get latest tick for a symbol."""
        return self.ws.get_tick(symbol)

    def get_all_ticks(self) -> Dict[str, Tick]:
        """Get all latest ticks."""
        return self.ws.get_all_ticks()

    def get_mid_price(self, symbol: str) -> Optional[Decimal]:
        """Get current mid price for a symbol."""
        tick = self.ws.get_tick(symbol)
        if tick:
            return tick.mid_price
        # Fallback to context
        ctx = self._market_contexts.get(symbol)
        if ctx:
            return ctx.mid_price
        return None

    def get_spread(self, symbol: str) -> Optional[Decimal]:
        """Get current spread for a symbol."""
        tick = self.ws.get_tick(symbol)
        if tick:
            return tick.spread
        return None

    def get_orderbook(self, symbol: str) -> Optional[Dict]:
        """Get current orderbook for a symbol."""
        return self.ws.get_orderbook(symbol)

    # -------------------------------------------------------------------------
    # Data Access - Bars
    # -------------------------------------------------------------------------
    def get_bars(self, symbol: str, timeframe: TimeFrame, count: int = 100) -> List[Bar]:
        """Get bars for a symbol and timeframe."""
        # Get bars from both sources
        ws_bars = self.ws.get_bars(symbol, timeframe) or []
        agg_bars = self.bar_aggregator.get_bars(symbol, timeframe, count) or []

        # Use the source with more historical data
        # WebSocket typically only has the current bar, while aggregator has historical
        if len(agg_bars) >= len(ws_bars):
            return agg_bars[-count:]
        return ws_bars[-count:]

    def get_current_bar(self, symbol: str, timeframe: TimeFrame) -> Optional[Bar]:
        """Get the currently forming bar."""
        return self.bar_aggregator.get_current_bar(symbol, timeframe)

    async def get_bars_rest(
        self,
        symbol: str,
        timeframe: TimeFrame,
        count: int = 200,
    ) -> List[Bar]:
        """Get bars via REST (for longer history)."""
        return await self.rest.get_candles(symbol, timeframe.value)

    # -------------------------------------------------------------------------
    # Data Access - Market Context
    # -------------------------------------------------------------------------
    def get_market_context(self, symbol: str) -> Optional[MarketContext]:
        """Get market context for a symbol."""
        # Note: Reading dict.get() is atomic in Python, but for consistency
        # with the locking pattern, we could add a lock here too if needed
        return self._market_contexts.get(symbol)

    def get_all_market_contexts(self) -> Dict[str, MarketContext]:
        """Get all market contexts, enriched with live spread from WebSocket."""
        contexts = self._market_contexts.copy()
        # Enrich with live spread from WebSocket ticks
        for symbol, ctx in contexts.items():
            tick = self.ws.get_tick(symbol)
            if tick:
                ctx.spread = tick.spread
        return contexts

    def get_funding_rate(self, symbol: str) -> Optional[Decimal]:
        """Get current funding rate for a symbol."""
        ctx = self._market_contexts.get(symbol)
        if ctx:
            return ctx.funding_rate
        return None

    def get_open_interest(self, symbol: str) -> Optional[Decimal]:
        """Get open interest for a symbol."""
        ctx = self._market_contexts.get(symbol)
        if ctx:
            return ctx.open_interest
        return None

    async def get_funding_history(
        self,
        symbol: str,
        hours: int = 24,
    ) -> List[Dict]:
        """Get funding rate history."""
        end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_time = end_time - (hours * 3600 * 1000)
        return await self.rest.get_funding_history(symbol, start_time, end_time)

    async def get_predicted_funding(self) -> Dict[str, Decimal]:
        """Get predicted funding rates."""
        data = await self.rest.get_predicted_fundings()
        result = {}
        for item in data:
            # Parse the response based on actual API format
            if isinstance(item, dict):
                symbol = item.get("coin") or item.get("symbol")
                funding = item.get("predictedFunding") or item.get("funding")
                if symbol and funding:
                    result[symbol] = Decimal(str(funding))
        return result

    # -------------------------------------------------------------------------
    # Data Access - Account
    # -------------------------------------------------------------------------
    def get_account_state(self) -> Optional[AccountState]:
        """Get current account state."""
        # Note: Reading reference is atomic in Python, but the object itself
        # may be in the process of being replaced during updates
        return self._account_state

    async def refresh_account_state(self) -> AccountState:
        """Force refresh account state."""
        account_state = await self.rest.get_account_state()
        async with self._account_lock:
            self._account_state = account_state
            self._last_account_update = datetime.now(timezone.utc)
        return account_state

    def get_equity(self) -> Decimal:
        """Get current account equity."""
        if self._account_state:
            return self._account_state.equity
        return Decimal(0)

    def get_available_balance(self) -> Decimal:
        """Get available balance."""
        if self._account_state:
            return self._account_state.available_balance
        return Decimal(0)

    def get_positions(self) -> List:
        """Get open positions."""
        if self._account_state:
            return self._account_state.positions
        return []

    def get_position(self, symbol: str):
        """Get position for a specific symbol."""
        if self._account_state:
            return self._account_state.get_position(symbol)
        return None

    # -------------------------------------------------------------------------
    # Utility
    # -------------------------------------------------------------------------
    async def wait_for_data(self, timeout: float = 10.0) -> bool:
        """Wait until we have data for all symbols."""
        start = datetime.now(timezone.utc)
        while (datetime.now(timezone.utc) - start).total_seconds() < timeout:
            all_ready = True
            for symbol in self.symbols:
                if not self.get_tick(symbol) and not self.get_market_context(symbol):
                    all_ready = False
                    break
            if all_ready:
                return True
            await asyncio.sleep(0.1)
        return False

    def is_data_stale(self, max_age_seconds: float = 30) -> bool:
        """Check if data is stale."""
        if not self._last_account_update:
            return True
        age = (datetime.now(timezone.utc) - self._last_account_update).total_seconds()
        return age > max_age_seconds

    # -------------------------------------------------------------------------
    # ATR Calculation
    # -------------------------------------------------------------------------
    def calculate_atr(self, symbol: str, period: int = 14, timeframe: TimeFrame = TimeFrame.M1) -> Optional[Decimal]:
        """
        Calculate Average True Range (ATR) for a symbol.

        Args:
            symbol: The trading symbol
            period: ATR period (default 14)
            timeframe: Bar timeframe to use (default 1m)

        Returns:
            ATR value as Decimal, or None if insufficient data
        """
        bars = self.get_bars(symbol, timeframe, count=period + 1)

        if not bars or len(bars) < period + 1:
            return None

        tr_values = []
        for i in range(-period, 0):
            high = bars[i].high
            low = bars[i].low
            prev_close = bars[i - 1].close

            # True Range = max(high-low, |high-prev_close|, |low-prev_close|)
            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close)
            )
            tr_values.append(tr)

        if not tr_values:
            return None

        return sum(tr_values) / len(tr_values)

    def get_atr(self, symbol: str) -> Optional[Decimal]:
        """Get ATR(14) for a symbol using 1-minute bars."""
        return self.calculate_atr(symbol, period=14, timeframe=TimeFrame.M1)

    def get_all_market_contexts_with_atr(self) -> Dict[str, MarketContext]:
        """
        Get all market contexts enriched with live spread AND ATR.

        This should be used instead of get_all_market_contexts() when
        dynamic TP/SL based on volatility is needed.
        """
        contexts = self._market_contexts.copy()

        for symbol, ctx in contexts.items():
            # Enrich with live spread from WebSocket ticks
            tick = self.ws.get_tick(symbol)
            if tick:
                ctx.spread = tick.spread

            # Calculate and set ATR
            atr = self.calculate_atr(symbol)
            if atr:
                ctx.atr_14 = atr

        return contexts
