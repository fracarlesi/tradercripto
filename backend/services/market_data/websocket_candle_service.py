"""
WebSocket Candle Service - Real-time candle streaming via Hyperliquid WebSocket API.

This service maintains a persistent WebSocket connection to Hyperliquid to receive
real-time 1h candle updates for all tradeable symbols.

Architecture:
- Persistent WebSocket connection with automatic reconnection
- Local cache: 24 most recent 1h candles per symbol (collections.deque)
- Event-driven: Triggers momentum calculation on candle close
- Thread-safe: Locks for concurrent access to cache
- State persistence: Saves cache to disk on shutdown, loads on startup

Performance:
- Zero rate limiting (WebSocket streaming)
- Sub-second latency for new candles
- Memory usage: ~220 symbols × 70 candles × 200 bytes = ~3 MB

Usage:
    service = WebsocketCandleService()
    await service.start()

    # Read candles from cache
    btc_candles = service.get_candles("BTC")

    # Stop service
    await service.stop()
"""

import asyncio
import json
import logging
import time
from collections import deque, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional, Callable, Any

from hyperliquid.info import Info
from hyperliquid.utils import constants

logger = logging.getLogger(__name__)


class WebsocketCandleService:
    """
    Manages persistent WebSocket connection to Hyperliquid for real-time 1h candle updates.

    Features:
    - Automatic reconnection with exponential backoff
    - Local cache (24 candles per symbol)
    - State persistence (disk)
    - Thread-safe cache access
    - Event callbacks on candle updates
    """

    def __init__(
        self,
        cache_dir: str = "./websocket_cache",
        max_candles_per_symbol: int = 70,
        reconnect_delay_base: float = 1.0,
        reconnect_delay_max: float = 60.0,
        use_testnet: bool = False,
    ):
        """
        Initialize WebSocket candle service.

        Args:
            cache_dir: Directory for cache persistence
            max_candles_per_symbol: Number of candles to keep per symbol (default 70 = ~3 days for technical analysis)
            reconnect_delay_base: Base delay for exponential backoff (seconds)
            reconnect_delay_max: Max delay between reconnection attempts (seconds)
            use_testnet: Use Hyperliquid testnet instead of mainnet
        """
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.max_candles = max_candles_per_symbol
        self.reconnect_delay_base = reconnect_delay_base
        self.reconnect_delay_max = reconnect_delay_max
        self.use_testnet = use_testnet

        # Hyperliquid Info client
        self.info = Info(constants.TESTNET_API_URL if use_testnet else constants.MAINNET_API_URL, skip_ws=True)

        # WebSocket connection (will be initialized in start())
        self.ws = None
        self.ws_task = None

        # Local cache: {symbol: deque([candle1, candle2, ...])}
        # Each candle: {"t": timestamp_ms, "o": open, "h": high, "l": low, "c": close, "v": volume}
        self.candle_cache: Dict[str, deque] = defaultdict(lambda: deque(maxlen=self.max_candles))
        self.cache_lock = Lock()

        # Price cache (allMids): {symbol: price}
        # Updated in real-time from WebSocket allMids subscription
        self.price_cache: Dict[str, float] = {}
        self.price_cache_lock = Lock()
        self.price_cache_last_update: Optional[float] = None  # Timestamp of last update

        # Event callbacks: Called when new candle arrives
        # Signature: callback(symbol: str, candle: dict)
        self.callbacks: List[Callable[[str, dict], None]] = []

        # Connection state
        self.running = False
        self.connected = False
        self.reconnect_attempts = 0

        # Subscribed symbols
        self.subscribed_symbols: List[str] = []

        logger.info(f"WebsocketCandleService initialized (cache_dir={cache_dir}, max_candles={max_candles_per_symbol})")

    async def start(self, symbols: Optional[List[str]] = None):
        """
        Start WebSocket service and subscribe to symbols.

        Args:
            symbols: List of symbols to subscribe to (None = auto-fetch all from meta())
        """
        if self.running:
            logger.warning("WebSocket service already running")
            return

        self.running = True

        # Get symbols list
        if symbols is None:
            logger.info("Fetching all available symbols from Hyperliquid...")
            meta = self.info.meta()
            symbols = [asset["name"] for asset in meta["universe"]]
            logger.info(f"Found {len(symbols)} symbols")

        self.subscribed_symbols = symbols

        # Load cache from disk
        await self._load_cache()

        # Start WebSocket connection task
        self.ws_task = asyncio.create_task(self._websocket_loop())

        logger.info(f"✅ WebSocket service started with {len(symbols)} symbols")

    async def stop(self):
        """Stop WebSocket service and save cache to disk."""
        if not self.running:
            return

        logger.info("Stopping WebSocket service...")
        self.running = False

        # Cancel WebSocket task
        if self.ws_task:
            self.ws_task.cancel()
            try:
                await self.ws_task
            except asyncio.CancelledError:
                pass

        # Close WebSocket connection
        if self.ws:
            await self.ws.close()

        # Save cache to disk
        await self._save_cache()

        logger.info("✅ WebSocket service stopped")

    def get_candles(self, symbol: str, limit: Optional[int] = None) -> List[dict]:
        """
        Get cached candles for a symbol.

        Args:
            symbol: Symbol to get candles for (e.g., "BTC")
            limit: Max number of candles to return (default: all cached)

        Returns:
            List of candles (most recent first)
        """
        with self.cache_lock:
            candles = list(self.candle_cache.get(symbol, []))

            if not candles:
                return []

            # Sort by timestamp descending (most recent first)
            candles_sorted = sorted(candles, key=lambda c: c["t"], reverse=True)

            if limit:
                return candles_sorted[:limit]

            return candles_sorted

    def get_latest_candle(self, symbol: str) -> Optional[dict]:
        """
        Get most recent candle for a symbol.

        Args:
            symbol: Symbol to get candle for

        Returns:
            Latest candle dict or None if no data
        """
        candles = self.get_candles(symbol, limit=1)
        return candles[0] if candles else None

    def get_price(self, symbol: str) -> Optional[float]:
        """
        Get current mid price for a symbol from WebSocket cache.

        Args:
            symbol: Symbol to get price for (e.g., "BTC")

        Returns:
            Current mid price or None if not available
        """
        with self.price_cache_lock:
            return self.price_cache.get(symbol)

    def get_all_prices(self) -> Dict[str, float]:
        """
        Get all current mid prices from WebSocket cache.

        Returns:
            Dict mapping symbol -> price
        """
        with self.price_cache_lock:
            return self.price_cache.copy()

    def register_callback(self, callback: Callable[[str, dict], None]):
        """
        Register callback to be called when new candle arrives.

        Args:
            callback: Function with signature (symbol: str, candle: dict) -> None
        """
        self.callbacks.append(callback)
        logger.info(f"Registered callback: {callback.__name__}")

    def get_cache_stats(self) -> dict:
        """
        Get cache statistics.

        Returns:
            Dict with cache stats (symbols_cached, total_candles, memory_mb, prices_cached, etc.)
        """
        with self.cache_lock:
            symbols_cached = len(self.candle_cache)
            total_candles = sum(len(candles) for candles in self.candle_cache.values())

            # Estimate memory usage (rough)
            memory_bytes = total_candles * 200  # ~200 bytes per candle
            memory_mb = memory_bytes / (1024 * 1024)

        with self.price_cache_lock:
            prices_cached = len(self.price_cache)
            price_memory_bytes = prices_cached * 16  # ~16 bytes per float + key
            price_memory_mb = price_memory_bytes / (1024 * 1024)

            return {
                "symbols_cached": symbols_cached,
                "total_candles": total_candles,
                "memory_mb": round(memory_mb, 2),
                "prices_cached": prices_cached,
                "price_memory_mb": round(price_memory_mb, 2),
                "price_last_update": self.price_cache_last_update,
                "connected": self.connected,
                "running": self.running,
            }

    # ===================================
    # WebSocket Connection Management
    # ===================================

    async def _websocket_loop(self):
        """
        Main WebSocket loop with automatic reconnection.

        Runs until self.running = False.
        """
        while self.running:
            try:
                logger.info("Connecting to Hyperliquid WebSocket...")

                # Create WebSocket connection
                # NOTE: Hyperliquid SDK's WebsocketManager is sync-only, so we use websockets library
                import websockets

                # Determine WebSocket URL based on environment
                # Mainnet: wss://api.hyperliquid.xyz/ws
                # Testnet: wss://api.hyperliquid-testnet.xyz/ws
                if self.use_testnet:
                    ws_url = "wss://api.hyperliquid-testnet.xyz/ws"
                else:
                    ws_url = "wss://api.hyperliquid.xyz/ws"

                async with websockets.connect(ws_url) as websocket:
                    self.ws = websocket
                    self.connected = True
                    self.reconnect_attempts = 0

                    logger.info(f"✅ WebSocket connected to {ws_url}")

                    # Subscribe to all symbols
                    await self._subscribe_all()

                    # Receive messages
                    async for message in websocket:
                        await self._handle_message(message)

            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"WebSocket connection closed: {e}")
                self.connected = False
                await self._handle_reconnection()

            except Exception as e:
                logger.error(f"WebSocket error: {e}", exc_info=True)
                self.connected = False
                await self._handle_reconnection()

    async def _subscribe_all(self):
        """Subscribe to 1h candles for all symbols and allMids for real-time prices."""
        if not self.ws:
            return

        logger.info(f"Subscribing to {len(self.subscribed_symbols)} symbols...")

        # Hyperliquid WebSocket subscription format
        # Reference: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions
        # Format: {"method": "subscribe", "subscription": {"type": "candle", "coin": "BTC", "interval": "1h"}}
        for i, symbol in enumerate(self.subscribed_symbols):
            subscription = {
                "method": "subscribe",
                "subscription": {
                    "type": "candle",
                    "coin": symbol,
                    "interval": "1h",
                }
            }

            await self.ws.send(json.dumps(subscription))

            # Small delay to avoid flooding (10ms per subscription)
            await asyncio.sleep(0.01)

            # Progress logging
            if (i + 1) % 50 == 0:
                logger.debug(f"Subscribed to {i+1}/{len(self.subscribed_symbols)} symbols...")

        logger.info(f"✅ Subscribed to {len(self.subscribed_symbols)} symbols")

        # Subscribe to allMids for real-time prices (single subscription for all symbols)
        allmids_subscription = {
            "method": "subscribe",
            "subscription": {"type": "allMids"}
        }
        await self.ws.send(json.dumps(allmids_subscription))
        logger.info("✅ Subscribed to allMids (real-time prices)")

    async def _handle_message(self, message: str):
        """
        Handle incoming WebSocket message.

        Message format from Hyperliquid:
        {
          "channel": "candle",
          "data": [
            {
              "t": <open_time_ms>,
              "T": <close_time_ms>,
              "s": <symbol>,
              "i": <interval>,
              "o": <open>,
              "c": <close>,
              "h": <high>,
              "l": <low>,
              "v": <volume>,
              "n": <num_trades>
            }
          ]
        }

        Args:
            message: Raw JSON message from WebSocket
        """
        try:
            data = json.loads(message)

            # Check message type
            msg_type = data.get("channel")

            if msg_type == "candle":
                # Candle update - data is a SINGLE candle dict (NOT an array!)
                candle_data = data.get("data", {})

                if not isinstance(candle_data, dict):
                    logger.error(f"candle_data is {type(candle_data)}, not dict. Skipping.")
                    return

                symbol = candle_data.get("s")  # Symbol

                # Extract OHLCV (matching Hyperliquid format)
                candle = {
                    "t": candle_data.get("t"),  # Open time (ms)
                    "T": candle_data.get("T"),  # Close time (ms)
                    "o": float(candle_data.get("o")),  # Open
                    "h": float(candle_data.get("h")),  # High
                    "l": float(candle_data.get("l")),  # Low
                    "c": float(candle_data.get("c")),  # Close
                    "v": float(candle_data.get("v")),  # Volume
                    "n": candle_data.get("n"),  # Number of trades
                }

                # Update cache
                with self.cache_lock:
                    self.candle_cache[symbol].append(candle)

                logger.debug(f"Added candle for {symbol}: close={candle['c']}, time={candle['t']}")

                # Trigger callbacks
                for callback in self.callbacks:
                    try:
                        callback(symbol, candle)
                    except Exception as e:
                        logger.error(f"Callback {callback.__name__} failed: {e}", exc_info=True)

            elif msg_type == "allMids":
                # All mids update - nested dict structure
                # data.get("data") returns {"mids": {"BTC": "96378.5", ...}}
                payload = data.get("data", {})
                mids_data = payload.get("mids", {})

                if not isinstance(mids_data, dict):
                    logger.error(f"allMids mids is {type(mids_data)}, not dict. Skipping.")
                    return

                # Update price cache with all prices
                with self.price_cache_lock:
                    for symbol, price_str in mids_data.items():
                        try:
                            self.price_cache[symbol] = float(price_str)
                        except (ValueError, TypeError) as e:
                            logger.warning(f"Failed to parse price for {symbol}: {price_str} - {e}")

                    self.price_cache_last_update = time.time()

                logger.debug(f"Updated {len(mids_data)} prices from allMids")

            elif msg_type == "subscriptionResponse":
                # Subscription confirmation
                logger.debug(f"Subscription confirmed: {data}")

            else:
                # Unknown message type (or subscription response)
                logger.debug(f"Received message: {msg_type}")

        except Exception as e:
            logger.error(f"Failed to handle WebSocket message: {e}", exc_info=True)
            logger.debug(f"Message content: {message[:500]}")  # Log first 500 chars for debugging

    async def _handle_reconnection(self):
        """Handle reconnection with exponential backoff."""
        if not self.running:
            return

        self.reconnect_attempts += 1

        # Calculate delay with exponential backoff
        delay = min(
            self.reconnect_delay_base * (2 ** self.reconnect_attempts),
            self.reconnect_delay_max,
        )

        logger.info(f"Reconnecting in {delay:.1f}s (attempt {self.reconnect_attempts})...")
        await asyncio.sleep(delay)

    # ===================================
    # Cache Persistence
    # ===================================

    async def _save_cache(self):
        """Save candle cache to disk."""
        try:
            cache_file = self.cache_dir / "candle_cache.json"

            with self.cache_lock:
                # Convert deque to list for JSON serialization
                cache_data = {
                    symbol: list(candles)
                    for symbol, candles in self.candle_cache.items()
                }

            # Write to disk
            with open(cache_file, "w") as f:
                json.dump(cache_data, f)

            logger.info(f"💾 Saved cache to {cache_file} ({len(cache_data)} symbols)")

        except Exception as e:
            logger.error(f"Failed to save cache: {e}", exc_info=True)

    async def _load_cache(self):
        """Load candle cache from disk."""
        try:
            cache_file = self.cache_dir / "candle_cache.json"

            if not cache_file.exists():
                logger.info("No cache file found, starting fresh")
                return

            # Read from disk
            with open(cache_file, "r") as f:
                cache_data = json.load(f)

            # Restore cache
            with self.cache_lock:
                for symbol, candles in cache_data.items():
                    self.candle_cache[symbol] = deque(candles, maxlen=self.max_candles)

            logger.info(f"📂 Loaded cache from {cache_file} ({len(cache_data)} symbols)")

        except Exception as e:
            logger.error(f"Failed to load cache: {e}", exc_info=True)


# ===================================
# Global Singleton Instance
# ===================================

_websocket_candle_service: Optional[WebsocketCandleService] = None


def get_websocket_candle_service() -> WebsocketCandleService:
    """
    Get global WebSocket candle service instance (singleton).

    Returns:
        WebsocketCandleService instance
    """
    global _websocket_candle_service

    if _websocket_candle_service is None:
        _websocket_candle_service = WebsocketCandleService()

    return _websocket_candle_service
