"""
HLQuantBot Market Scanner Service
=================================

Scans all perpetual coins on Hyperliquid to collect market data.

Features:
- Scans 200+ perpetual coins every 5 minutes (configurable)
- Collects price, volume, funding rate, open interest, spread
- Respects Hyperliquid rate limits (100 requests/min for info API)
- Filters out stablecoins and low-volume coins
- Stores snapshots in database
- Publishes to Topic.MARKET_DATA for downstream services

Data collected per coin:
- price: Current mark price
- volume_24h: 24-hour notional volume
- change_24h_pct: 24-hour price change percentage
- open_interest: Open interest in USD
- funding_rate: Current funding rate
- predicted_funding: Next predicted funding rate
- spread_pct: Bid/ask spread percentage
- atr_pct: Estimated ATR as percentage of price

Author: Francesco Carlesi
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set

from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.utils import constants

from .base import BaseService, HealthStatus
from .message_bus import MessageBus, Topic

# Try to import Database
try:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from database.db import Database
    DB_AVAILABLE = True
except ImportError:
    Database = None  # type: ignore
    DB_AVAILABLE = False

# Try to import config
try:
    from simple_bot.config.loader import MarketScannerConfig
    CONFIG_AVAILABLE = True
except ImportError:
    MarketScannerConfig = None  # type: ignore
    CONFIG_AVAILABLE = False


logger = logging.getLogger(__name__)


# =============================================================================
# Constants
# =============================================================================

# Default stablecoins to exclude
DEFAULT_EXCLUDE_SYMBOLS: Set[str] = {"USDC", "USDT", "DAI", "BUSD", "TUSD", "USDP", "FRAX"}

# Rate limiting: 100 requests/minute for info API
INFO_API_RATE_LIMIT = 100
INFO_API_RATE_WINDOW_SECONDS = 60

# Batch size for L2 snapshots (to respect rate limits)
L2_BATCH_SIZE = 20
L2_BATCH_DELAY_SECONDS = 1.0


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class CoinData:
    """Market data for a single coin."""
    
    symbol: str
    price: float
    volume_24h: float
    change_24h_pct: float
    open_interest: float
    funding_rate: float
    predicted_funding: float
    spread_pct: float
    atr_pct: float  # Estimated from recent volatility
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "symbol": self.symbol,
            "price": self.price,
            "volume_24h": self.volume_24h,
            "change_24h_pct": self.change_24h_pct,
            "open_interest": self.open_interest,
            "funding_rate": self.funding_rate,
            "predicted_funding": self.predicted_funding,
            "spread_pct": self.spread_pct,
            "atr_pct": self.atr_pct,
        }


@dataclass
class ScanMetrics:
    """Metrics for the scanner service."""
    
    coins_scanned: int = 0
    last_scan_duration_ms: float = 0.0
    last_scan_timestamp: Optional[datetime] = None
    total_scans: int = 0
    failed_scans: int = 0
    api_errors: int = 0


# =============================================================================
# Market Scanner Service
# =============================================================================

class MarketScannerService(BaseService):
    """
    Service that scans all Hyperliquid perpetual markets.
    
    Collects comprehensive market data including prices, volumes,
    funding rates, open interest, and spreads. Publishes snapshots
    to the message bus and stores them in the database.
    
    Configuration (via MarketScannerConfig):
    - interval_seconds: Scan interval (default: 300 = 5 minutes)
    - coins_limit: Maximum coins to scan (default: 200)
    - min_volume_24h: Minimum 24h volume filter (default: 1,000,000)
    - exclude_symbols: Symbols to exclude (default: stablecoins)
    
    Example:
        scanner = MarketScannerService(
            name="market_scanner",
            bus=bus,
            db=db,
            config=scanner_config,
        )
        await scanner.start()
    """
    
    def __init__(
        self,
        name: str = "market_scanner",
        bus: Optional[MessageBus] = None,
        db: Optional["Database"] = None,
        config: Optional["MarketScannerConfig"] = None,
        testnet: bool = True,
    ) -> None:
        """
        Initialize market scanner service.
        
        Args:
            name: Service name for logging
            bus: MessageBus for publishing data
            db: Database for storing snapshots
            config: MarketScannerConfig with service settings
            testnet: Use testnet API (default: True)
        """
        # Set loop interval from config
        interval = 300  # Default 5 minutes
        if config:
            interval = config.interval_seconds
        
        super().__init__(
            name=name,
            bus=bus,
            db=db,
            loop_interval_seconds=interval,
        )
        
        # Configuration
        self._scanner_config = config
        self._testnet = testnet
        
        # Hyperliquid client
        self._info: Optional[Info] = None
        
        # Configuration values (with defaults)
        self._coins_limit = 200
        self._min_volume_24h = 1_000_000.0
        self._exclude_symbols: Set[str] = DEFAULT_EXCLUDE_SYMBOLS.copy()
        
        if config:
            self._coins_limit = config.coins_limit
            self._min_volume_24h = config.min_volume_24h
            self._exclude_symbols = set(config.exclude_symbols)
        
        # Metrics
        self._metrics = ScanMetrics()
        
        # Cache for previous prices (for change calculation)
        self._previous_prices: Dict[str, float] = {}
        
        self._logger.info(
            "MarketScannerService initialized: interval=%ds, limit=%d, min_volume=$%.0f",
            self.loop_interval_seconds,
            self._coins_limit,
            self._min_volume_24h,
        )
    
    # =========================================================================
    # Lifecycle
    # =========================================================================
    
    async def _on_start(self) -> None:
        """Initialize Hyperliquid client on start."""
        self._logger.info("Starting MarketScannerService...")
        
        # Determine API URL
        testnet = self._testnet
        if os.getenv("HYPERLIQUID_TESTNET", "").lower() == "true":
            testnet = True
        elif os.getenv("HYPERLIQUID_TESTNET", "").lower() == "false":
            testnet = False
        
        base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
        self._logger.info(
            "Connecting to Hyperliquid %s: %s",
            "TESTNET" if testnet else "MAINNET",
            base_url,
        )
        
        # Initialize Info client (no authentication needed for market data)
        self._info = Info(base_url, skip_ws=True)
        
        # Verify connection
        try:
            meta = self._info.meta()
            num_perps = len(meta.get("universe", []))
            self._logger.info("Connected to Hyperliquid. Found %d perpetual markets.", num_perps)
        except Exception as e:
            self._logger.error("Failed to connect to Hyperliquid: %s", e)
            raise
        
        # Run initial scan
        await self._run_scan()
    
    async def _on_stop(self) -> None:
        """Cleanup on stop."""
        self._logger.info("Stopping MarketScannerService...")
        self._info = None
    
    async def _run_iteration(self) -> None:
        """Run a market scan iteration."""
        await self._run_scan()
    
    # =========================================================================
    # Health Check
    # =========================================================================
    
    async def _health_check_impl(self) -> bool:
        """
        Check service health.
        
        Unhealthy if:
        - No successful scan in last 10 minutes
        - Hyperliquid client not initialized
        """
        # Check client
        if self._info is None:
            self._logger.warning("Health check failed: Hyperliquid client not initialized")
            return False
        
        # Check last scan time
        if self._metrics.last_scan_timestamp:
            time_since_scan = datetime.now(timezone.utc) - self._metrics.last_scan_timestamp
            if time_since_scan > timedelta(minutes=10):
                self._logger.warning(
                    "Health check failed: Last scan was %s ago",
                    time_since_scan,
                )
                return False
        
        return True
    
    @property
    def metrics(self) -> Dict[str, Any]:
        """Get scanner metrics."""
        return {
            "coins_scanned": self._metrics.coins_scanned,
            "last_scan_duration_ms": self._metrics.last_scan_duration_ms,
            "last_scan_timestamp": (
                self._metrics.last_scan_timestamp.isoformat()
                if self._metrics.last_scan_timestamp
                else None
            ),
            "total_scans": self._metrics.total_scans,
            "failed_scans": self._metrics.failed_scans,
            "api_errors": self._metrics.api_errors,
        }
    
    # =========================================================================
    # Core Scanning Logic
    # =========================================================================
    
    async def _run_scan(self) -> Optional[Dict[str, Any]]:
        """
        Execute a full market scan.
        
        Returns:
            Market snapshot dict or None on failure
        """
        if self._info is None:
            self._logger.error("Cannot scan: Hyperliquid client not initialized")
            return None
        
        start_time = time.perf_counter()
        self._logger.info("Starting market scan...")
        
        try:
            # Step 1: Get all perpetual metadata and asset contexts
            universe, contexts = await self._fetch_meta_and_contexts()
            
            if not universe or not contexts:
                self._logger.error("Failed to fetch metadata")
                self._metrics.failed_scans += 1
                return None
            
            # Step 2: Get all mid prices
            all_mids = await self._fetch_all_mids()
            
            # Step 3: Process and filter coins
            coin_data_list = await self._process_coins(universe, contexts, all_mids)
            
            # Step 4: Optionally fetch spreads for top coins
            await self._enrich_with_spreads(coin_data_list)
            
            # Step 5: Build snapshot
            snapshot_data = {
                coin.symbol: coin.to_dict() for coin in coin_data_list
            }
            
            # Step 6: Store in database
            snapshot_id = await self._store_snapshot(snapshot_data)
            
            # Step 7: Publish to message bus
            await self._publish_snapshot(snapshot_id, snapshot_data)
            
            # Update metrics
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            self._metrics.coins_scanned = len(coin_data_list)
            self._metrics.last_scan_duration_ms = elapsed_ms
            self._metrics.last_scan_timestamp = datetime.now(timezone.utc)
            self._metrics.total_scans += 1
            
            self._logger.info(
                "Market scan complete: %d coins in %.0fms",
                len(coin_data_list),
                elapsed_ms,
            )
            
            return {
                "snapshot_id": snapshot_id,
                "timestamp": datetime.now(timezone.utc),
                "coins_count": len(coin_data_list),
                "data": snapshot_data,
            }
            
        except Exception as e:
            self._logger.error("Market scan failed: %s", e, exc_info=True)
            self._metrics.failed_scans += 1
            self._metrics.api_errors += 1
            return None
    
    async def _fetch_meta_and_contexts(self) -> tuple:
        """
        Fetch perpetual metadata and asset contexts.
        
        Returns:
            Tuple of (universe dict, contexts list)
        """
        try:
            # Run in thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                self._info.meta_and_asset_ctxs,
            )
            
            if isinstance(result, (list, tuple)) and len(result) >= 2:
                return result[0], result[1]
            
            self._logger.error("Unexpected meta_and_asset_ctxs response: %s", type(result))
            return None, None
            
        except Exception as e:
            self._logger.error("Failed to fetch meta_and_asset_ctxs: %s", e)
            self._metrics.api_errors += 1
            return None, None
    
    async def _fetch_all_mids(self) -> Dict[str, str]:
        """
        Fetch all mid prices.
        
        Returns:
            Dict of symbol -> mid price string
        """
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                self._info.all_mids,
            )
        except Exception as e:
            self._logger.error("Failed to fetch all_mids: %s", e)
            self._metrics.api_errors += 1
            return {}
    
    async def _process_coins(
        self,
        universe: Dict[str, Any],
        contexts: List[Dict[str, Any]],
        all_mids: Dict[str, str],
    ) -> List[CoinData]:
        """
        Process coin data from API responses.
        
        Args:
            universe: Metadata with universe list
            contexts: Asset contexts with prices/volumes
            all_mids: Mid prices dict
        
        Returns:
            List of CoinData objects
        """
        coin_data_list: List[CoinData] = []
        assets = universe.get("universe", [])
        
        for idx, asset in enumerate(assets):
            symbol = asset.get("name", "")
            
            # Skip excluded symbols
            if symbol in self._exclude_symbols:
                continue
            
            # Skip if no context
            if idx >= len(contexts):
                continue
            
            ctx = contexts[idx]
            
            # Extract data
            try:
                price = float(ctx.get("markPx", 0))
                volume_24h = float(ctx.get("dayNtlVlm", 0))
                open_interest = float(ctx.get("openInterest", 0)) * price
                
                # Funding rate (hourly rate, annualize by *24*365)
                funding_str = ctx.get("funding", "0")
                funding_rate = float(funding_str) if funding_str else 0.0
                
                # Predicted funding from premium
                premium_str = ctx.get("premium", "0")
                predicted_funding = float(premium_str) if premium_str else funding_rate
                
                # Volume filter
                if volume_24h < self._min_volume_24h:
                    continue
                
                # Calculate 24h change from cached prices
                change_24h_pct = 0.0
                if symbol in self._previous_prices and self._previous_prices[symbol] > 0:
                    prev_price = self._previous_prices[symbol]
                    change_24h_pct = ((price - prev_price) / prev_price) * 100
                
                # Update price cache
                self._previous_prices[symbol] = price
                
                # Estimate ATR from day's price movement (rough estimate)
                # In production, would calculate from actual candles
                prev_day_px = float(ctx.get("prevDayPx", price))
                if prev_day_px > 0:
                    day_range = abs(price - prev_day_px)
                    atr_pct = (day_range / prev_day_px) * 100
                else:
                    atr_pct = 0.0
                
                coin_data = CoinData(
                    symbol=symbol,
                    price=price,
                    volume_24h=volume_24h,
                    change_24h_pct=change_24h_pct,
                    open_interest=open_interest,
                    funding_rate=funding_rate,
                    predicted_funding=predicted_funding,
                    spread_pct=0.0,  # Will be enriched later
                    atr_pct=atr_pct,
                )
                
                coin_data_list.append(coin_data)
                
            except (ValueError, TypeError) as e:
                self._logger.warning("Failed to parse data for %s: %s", symbol, e)
                continue
        
        # Sort by volume and limit
        coin_data_list.sort(key=lambda x: x.volume_24h, reverse=True)
        coin_data_list = coin_data_list[:self._coins_limit]
        
        self._logger.debug(
            "Processed %d coins after filtering (limit: %d)",
            len(coin_data_list),
            self._coins_limit,
        )
        
        return coin_data_list
    
    async def _enrich_with_spreads(self, coin_data_list: List[CoinData]) -> None:
        """
        Enrich top coins with bid/ask spread data.
        
        Fetches L2 order book for top coins to calculate spread.
        Respects rate limits by batching requests.
        
        Args:
            coin_data_list: List of CoinData to enrich
        """
        # Only fetch spreads for top N coins (rate limit conscious)
        top_coins = coin_data_list[:L2_BATCH_SIZE]
        
        for coin in top_coins:
            try:
                loop = asyncio.get_event_loop()
                l2_snapshot = await loop.run_in_executor(
                    None,
                    lambda sym=coin.symbol: self._info.l2_snapshot(sym),
                )
                
                if l2_snapshot and "levels" in l2_snapshot:
                    bids, asks = l2_snapshot["levels"]
                    
                    if bids and asks:
                        best_bid = float(bids[0].get("px", 0))
                        best_ask = float(asks[0].get("px", 0))
                        
                        if best_bid > 0 and best_ask > 0:
                            mid_price = (best_bid + best_ask) / 2
                            spread = best_ask - best_bid
                            coin.spread_pct = (spread / mid_price) * 100
                
                # Small delay to respect rate limits
                await asyncio.sleep(L2_BATCH_DELAY_SECONDS / L2_BATCH_SIZE)
                
            except Exception as e:
                self._logger.warning("Failed to fetch L2 for %s: %s", coin.symbol, e)
    
    async def _store_snapshot(self, snapshot_data: Dict[str, Dict]) -> Optional[int]:
        """
        Store snapshot in database.
        
        Args:
            snapshot_data: Dict of symbol -> coin data
            
        Returns:
            snapshot_id or None if no database
        """
        if not self.db:
            self._logger.debug("No database configured, skipping storage")
            return None
        
        try:
            # Convert to JSON-serializable format
            import json
            json_data = json.dumps(snapshot_data)
            
            snapshot_id = await self.db.insert_market_snapshot(json_data)
            self._logger.debug("Stored snapshot with ID: %s", snapshot_id)
            return snapshot_id
            
        except Exception as e:
            self._logger.error("Failed to store snapshot: %s", e)
            return None
    
    async def _publish_snapshot(
        self,
        snapshot_id: Optional[int],
        snapshot_data: Dict[str, Dict],
    ) -> None:
        """
        Publish snapshot to message bus.
        
        Args:
            snapshot_id: Database snapshot ID (may be None)
            snapshot_data: Dict of symbol -> coin data
        """
        if not self.bus:
            self._logger.debug("No message bus configured, skipping publish")
            return
        
        message = {
            "snapshot_id": snapshot_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "coins_count": len(snapshot_data),
            "data": snapshot_data,
        }
        
        await self.publish(Topic.MARKET_DATA, message)
        self._logger.debug(
            "Published market data snapshot: %d coins",
            len(snapshot_data),
        )
    
    # =========================================================================
    # Public Methods
    # =========================================================================
    
    async def force_scan(self) -> Optional[Dict[str, Any]]:
        """
        Force an immediate scan (for testing or manual triggers).
        
        Returns:
            Snapshot dict or None on failure
        """
        return await self._run_scan()
    
    def get_last_snapshot(self) -> Optional[Dict[str, Any]]:
        """
        Get the most recent scan results (from memory).
        
        Returns:
            Last snapshot data or None
        """
        if self._metrics.last_scan_timestamp:
            return {
                "timestamp": self._metrics.last_scan_timestamp,
                "coins_count": self._metrics.coins_scanned,
            }
        return None


# =============================================================================
# Factory Function
# =============================================================================

def create_market_scanner(
    bus: Optional[MessageBus] = None,
    db: Optional["Database"] = None,
    config: Optional["MarketScannerConfig"] = None,
    testnet: bool = True,
) -> MarketScannerService:
    """
    Factory function to create a MarketScannerService.
    
    Args:
        bus: MessageBus instance
        db: Database instance
        config: MarketScannerConfig instance
        testnet: Use testnet API
        
    Returns:
        Configured MarketScannerService
    """
    return MarketScannerService(
        name="market_scanner",
        bus=bus,
        db=db,
        config=config,
        testnet=testnet,
    )
