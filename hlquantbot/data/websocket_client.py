"""WebSocket client for Hyperliquid real-time data."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Callable, Any, Set

import websockets
from websockets.exceptions import ConnectionClosed

from ..core.models import Tick, Bar
from ..core.enums import TimeFrame
from ..core.exceptions import WebSocketError, DataFeedError
from ..config.settings import Settings


logger = logging.getLogger(__name__)


class HyperliquidWebSocket:
    """WebSocket client for Hyperliquid real-time data feeds."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.ws_url = settings.hl_ws_url
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 10
        self._reconnect_delay = 1  # Initial delay in seconds

        # Subscriptions
        self._subscriptions: Set[str] = set()
        self._pending_subscriptions: List[Dict] = []

        # Data stores
        self._ticks: Dict[str, Tick] = {}
        self._bars: Dict[str, Dict[TimeFrame, List[Bar]]] = {}
        self._order_books: Dict[str, Dict] = {}

        # Callbacks
        self._tick_callbacks: List[Callable[[Tick], None]] = []
        self._bar_callbacks: List[Callable[[Bar], None]] = []
        self._trade_callbacks: List[Callable[[Dict], None]] = []
        self._fill_callbacks: List[Callable[[Dict], None]] = []

        # Locks
        self._data_lock = asyncio.Lock()

    # -------------------------------------------------------------------------
    # Connection Management
    # -------------------------------------------------------------------------
    async def connect(self):
        """Establish WebSocket connection."""
        try:
            self._ws = await websockets.connect(
                self.ws_url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
            )
            self._running = True
            self._reconnect_attempts = 0
            logger.info(f"WebSocket connected to {self.ws_url}")

            # Resubscribe if reconnecting
            if self._pending_subscriptions:
                for sub in self._pending_subscriptions:
                    await self._send(sub)
                self._pending_subscriptions = []

        except Exception as e:
            logger.error(f"WebSocket connection failed: {e}")
            raise WebSocketError(f"Connection failed: {e}")

    async def disconnect(self):
        """Close WebSocket connection."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info("WebSocket disconnected")

    async def _reconnect(self):
        """Attempt to reconnect with exponential backoff."""
        if self._reconnect_attempts >= self._max_reconnect_attempts:
            raise WebSocketError(
                "Max reconnection attempts reached",
                reconnect_attempt=self._reconnect_attempts
            )

        self._reconnect_attempts += 1
        delay = self._reconnect_delay * (2 ** (self._reconnect_attempts - 1))
        delay = min(delay, 60)  # Max 60 seconds

        logger.warning(f"Reconnecting in {delay}s (attempt {self._reconnect_attempts})")
        await asyncio.sleep(delay)

        try:
            await self.connect()
            # Resubscribe to all previous subscriptions
            await self._resubscribe_all()
        except Exception as e:
            logger.error(f"Reconnection failed: {e}")
            await self._reconnect()

    async def _resubscribe_all(self):
        """Resubscribe to all previous subscriptions."""
        for sub_key in list(self._subscriptions):
            parts = sub_key.split(":")
            sub_type = parts[0]

            if sub_type == "trades" and len(parts) > 1:
                await self.subscribe_trades(parts[1])
            elif sub_type == "l2Book" and len(parts) > 1:
                await self.subscribe_orderbook(parts[1])
            elif sub_type == "candle" and len(parts) > 2:
                await self.subscribe_candles(parts[1], parts[2])
            elif sub_type == "allMids":
                await self.subscribe_all_mids()

    async def _send(self, message: Dict):
        """Send message to WebSocket."""
        if not self._ws:
            raise WebSocketError("WebSocket not connected")
        await self._ws.send(json.dumps(message))

    # -------------------------------------------------------------------------
    # Subscriptions
    # -------------------------------------------------------------------------
    async def subscribe_trades(self, symbol: str):
        """Subscribe to trade feed for a symbol."""
        sub_key = f"trades:{symbol}"
        if sub_key in self._subscriptions:
            return

        message = {
            "method": "subscribe",
            "subscription": {
                "type": "trades",
                "coin": symbol,
            }
        }
        await self._send(message)
        self._subscriptions.add(sub_key)
        logger.info(f"Subscribed to trades: {symbol}")

    async def subscribe_orderbook(self, symbol: str):
        """Subscribe to L2 order book for a symbol."""
        sub_key = f"l2Book:{symbol}"
        if sub_key in self._subscriptions:
            return

        message = {
            "method": "subscribe",
            "subscription": {
                "type": "l2Book",
                "coin": symbol,
            }
        }
        await self._send(message)
        self._subscriptions.add(sub_key)
        logger.info(f"Subscribed to orderbook: {symbol}")

    async def subscribe_candles(self, symbol: str, interval: str = "1m"):
        """Subscribe to candle feed for a symbol."""
        sub_key = f"candle:{symbol}:{interval}"
        if sub_key in self._subscriptions:
            return

        message = {
            "method": "subscribe",
            "subscription": {
                "type": "candle",
                "coin": symbol,
                "interval": interval,
            }
        }
        await self._send(message)
        self._subscriptions.add(sub_key)
        logger.info(f"Subscribed to candles: {symbol} {interval}")

    async def subscribe_all_mids(self):
        """Subscribe to all mid prices."""
        sub_key = "allMids"
        if sub_key in self._subscriptions:
            return

        message = {
            "method": "subscribe",
            "subscription": {
                "type": "allMids",
            }
        }
        await self._send(message)
        self._subscriptions.add(sub_key)
        logger.info("Subscribed to all mids")

    async def subscribe_user_events(self, address: str):
        """Subscribe to user events (fills, liquidations, funding)."""
        sub_key = f"userEvents:{address}"
        if sub_key in self._subscriptions:
            return

        message = {
            "method": "subscribe",
            "subscription": {
                "type": "userEvents",
                "user": address,
            }
        }
        await self._send(message)
        self._subscriptions.add(sub_key)
        logger.info(f"Subscribed to user events: {address[:8]}...")

    # -------------------------------------------------------------------------
    # Callbacks
    # -------------------------------------------------------------------------
    def on_tick(self, callback: Callable[[Tick], None]):
        """Register callback for tick updates."""
        self._tick_callbacks.append(callback)

    def on_bar(self, callback: Callable[[Bar], None]):
        """Register callback for bar updates."""
        self._bar_callbacks.append(callback)

    def on_trade(self, callback: Callable[[Dict], None]):
        """Register callback for trade updates."""
        self._trade_callbacks.append(callback)

    def on_fill(self, callback: Callable[[Dict], None]):
        """Register callback for order fill events."""
        self._fill_callbacks.append(callback)

    # -------------------------------------------------------------------------
    # Message Processing
    # -------------------------------------------------------------------------
    async def _process_message(self, message: str):
        """Process incoming WebSocket message."""
        try:
            # Handle ping/pong or numeric heartbeats
            if message in ("0", "1", "ping", "pong", ""):
                return

            data = json.loads(message)

            # Handle non-dict messages (heartbeats, etc)
            if not isinstance(data, dict):
                return

            channel = data.get("channel")

            if channel == "subscriptionResponse":
                # Subscription confirmation
                return

            if channel == "error":
                logger.warning(f"WebSocket error from server: {data.get('data', data)}")
                return

            if channel == "allMids":
                await self._handle_all_mids(data.get("data", {}))
            elif channel == "trades":
                await self._handle_trades(data.get("data", []))
            elif channel == "l2Book":
                await self._handle_orderbook(data.get("data", {}))
            elif channel == "candle":
                await self._handle_candle(data.get("data", []))
            elif channel == "userEvents":
                await self._handle_user_events(data.get("data", {}))
            elif channel is not None:
                # Unknown channel - ignore silently
                pass

        except json.JSONDecodeError as e:
            # Likely a heartbeat or non-JSON message - ignore
            if len(message) < 10:
                return
            logger.debug(f"Failed to parse message: {e}")
        except Exception as e:
            logger.error(f"Error processing message: {e} - data: {message[:100]}")

    async def _handle_all_mids(self, data: Dict):
        """Handle allMids update."""
        async with self._data_lock:
            now = datetime.now(timezone.utc)
            for symbol, price in data.get("mids", {}).items():
                mid_price = Decimal(str(price))

                # Update or create tick
                if symbol in self._ticks:
                    tick = self._ticks[symbol]
                    tick.mid_price = mid_price
                    tick.timestamp = now
                else:
                    self._ticks[symbol] = Tick(
                        symbol=symbol,
                        timestamp=now,
                        mid_price=mid_price,
                        best_bid=mid_price,  # Will be updated by orderbook
                        best_ask=mid_price,
                    )

        # Notify callbacks
        for callback in self._tick_callbacks:
            for tick in self._ticks.values():
                try:
                    callback(tick)
                except Exception as e:
                    logger.error(f"Tick callback error: {e}")

    async def _handle_trades(self, trades: List[Dict]):
        """Handle trade updates."""
        for trade in trades:
            # Notify callbacks
            for callback in self._trade_callbacks:
                try:
                    callback(trade)
                except Exception as e:
                    logger.error(f"Trade callback error: {e}")

    async def _handle_orderbook(self, data: Dict):
        """Handle orderbook update."""
        coin = data.get("coin")
        if not coin:
            return

        levels = data.get("levels", [[], []])
        bids = levels[0] if len(levels) > 0 else []
        asks = levels[1] if len(levels) > 1 else []

        async with self._data_lock:
            self._order_books[coin] = {
                "bids": bids,
                "asks": asks,
                "timestamp": datetime.now(timezone.utc),
            }

            # Update tick with best bid/ask
            if coin in self._ticks and bids and asks:
                try:
                    # Handle both formats: {"px": "...", "sz": "..."} or ["price", "size"]
                    best_bid = bids[0]
                    best_ask = asks[0]

                    if isinstance(best_bid, dict):
                        bid_price = best_bid.get("px", best_bid.get("price", "0"))
                        ask_price = best_ask.get("px", best_ask.get("price", "0"))
                    else:
                        bid_price = best_bid[0]
                        ask_price = best_ask[0]

                    self._ticks[coin].best_bid = Decimal(str(bid_price))
                    self._ticks[coin].best_ask = Decimal(str(ask_price))
                except (IndexError, KeyError, TypeError):
                    pass  # Skip if format is unexpected

    async def _handle_candle(self, candles):
        """Handle candle updates."""
        # Handle both single candle dict and list of candles
        if isinstance(candles, dict):
            candles = [candles]
        elif not isinstance(candles, list):
            return

        for candle_data in candles:
            if not isinstance(candle_data, dict):
                continue
            symbol = candle_data.get("s")
            interval = candle_data.get("i")

            if not symbol or not interval:
                continue

            bar = Bar(
                symbol=symbol,
                timeframe=TimeFrame(interval),
                timestamp=datetime.fromtimestamp(candle_data["t"] / 1000, tz=timezone.utc),
                open=Decimal(str(candle_data["o"])),
                high=Decimal(str(candle_data["h"])),
                low=Decimal(str(candle_data["l"])),
                close=Decimal(str(candle_data["c"])),
                volume=Decimal(str(candle_data["v"])),
                trades_count=candle_data.get("n"),
            )

            async with self._data_lock:
                if symbol not in self._bars:
                    self._bars[symbol] = {}
                if bar.timeframe not in self._bars[symbol]:
                    self._bars[symbol][bar.timeframe] = []

                bars_list = self._bars[symbol][bar.timeframe]

                # Update or append
                if bars_list and bars_list[-1].timestamp == bar.timestamp:
                    bars_list[-1] = bar
                else:
                    bars_list.append(bar)
                    # Keep only last 500 bars
                    if len(bars_list) > 500:
                        bars_list.pop(0)

            # Notify callbacks
            for callback in self._bar_callbacks:
                try:
                    callback(bar)
                except Exception as e:
                    logger.error(f"Bar callback error: {e}")

    async def _handle_user_events(self, data: Dict):
        """Handle user events (fills, liquidations, etc.)."""
        # Process fills from user events
        fills = data.get("fills", [])
        for fill in fills:
            logger.info(f"Order fill received: {fill.get('coin')} oid={fill.get('oid')} px={fill.get('px')} sz={fill.get('sz')}")

            # Notify fill callbacks
            for callback in self._fill_callbacks:
                try:
                    callback(fill)
                except Exception as e:
                    logger.error(f"Fill callback error: {e}")

        # Log other user events for debugging
        if "liquidation" in data:
            logger.warning(f"Liquidation event: {data['liquidation']}")
        if "funding" in data:
            logger.debug(f"Funding event: {data['funding']}")

    # -------------------------------------------------------------------------
    # Main Loop
    # -------------------------------------------------------------------------
    async def run(self):
        """Main WebSocket event loop."""
        while self._running:
            try:
                if not self._ws:
                    await self.connect()

                async for message in self._ws:
                    if not self._running:
                        break
                    await self._process_message(message)

            except ConnectionClosed as e:
                logger.warning(f"WebSocket connection closed: {e}")
                if self._running:
                    await self._reconnect()

            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                if self._running:
                    await self._reconnect()

    # -------------------------------------------------------------------------
    # Data Access
    # -------------------------------------------------------------------------
    def get_tick(self, symbol: str) -> Optional[Tick]:
        """Get latest tick for a symbol."""
        return self._ticks.get(symbol)

    def get_all_ticks(self) -> Dict[str, Tick]:
        """Get all latest ticks."""
        return self._ticks.copy()

    def get_bars(self, symbol: str, timeframe: TimeFrame) -> List[Bar]:
        """Get bars for a symbol and timeframe."""
        if symbol in self._bars and timeframe in self._bars[symbol]:
            return self._bars[symbol][timeframe].copy()
        return []

    def get_orderbook(self, symbol: str) -> Optional[Dict]:
        """Get current orderbook for a symbol."""
        return self._order_books.get(symbol)
