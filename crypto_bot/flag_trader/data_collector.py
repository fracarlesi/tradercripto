"""
Hyperliquid Data Collector
===========================

Downloads and caches historical candle data from Hyperliquid Info API.
Stores data as Parquet files for efficient local access.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import pandas as pd
from hyperliquid.info import Info
from hyperliquid.utils import constants

logger = logging.getLogger(__name__)

# Hyperliquid candle intervals
VALID_INTERVALS = {"1m", "5m", "15m", "1h", "4h", "1d"}

# Max candles per API request
MAX_CANDLES_PER_REQUEST = 5000

# Rate limit: ~100 requests/minute for info API
REQUEST_DELAY_S = 0.7


class HyperliquidDataCollector:
    """Fetches and caches historical candle data from Hyperliquid.

    Args:
        data_dir: Directory to store Parquet files.
    """

    def __init__(self, data_dir: Path = Path("data/candles")) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._info: Info | None = None

    def _get_info(self) -> Info:
        """Lazy-init Hyperliquid Info client."""
        if self._info is None:
            self._info = Info(constants.MAINNET_API_URL, skip_ws=True)
        return self._info

    async def fetch_candles(
        self,
        symbol: str,
        interval: str = "15m",
        days: int = 180,
    ) -> pd.DataFrame:
        """Download candle data for a single asset.

        Args:
            symbol: Trading pair symbol (e.g. "BTC", "ETH").
            interval: Candle interval (1m, 5m, 15m, 1h, 4h, 1d).
            days: Number of days of history to fetch.

        Returns:
            DataFrame with columns: timestamp, open, high, low, close, volume.
        """
        if interval not in VALID_INTERVALS:
            raise ValueError(f"Invalid interval '{interval}'. Must be one of {VALID_INTERVALS}")

        info = self._get_info()
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - (days * 24 * 60 * 60 * 1000)

        all_candles: list[dict[str, Any]] = []
        cursor_ms = start_ms

        logger.info(f"Fetching {symbol} {interval} candles for {days} days...")

        while cursor_ms < end_ms:
            # Hyperliquid candles_snapshot: returns candles from start_time
            raw = await asyncio.to_thread(
                info.candles_snapshot,
                symbol,
                interval,
                cursor_ms,
                end_ms,
            )

            if not raw:
                break

            all_candles.extend(raw)

            # Move cursor past last candle
            last_ts = raw[-1]["t"]
            if last_ts <= cursor_ms:
                break
            cursor_ms = last_ts + 1

            # Rate limiting
            await asyncio.sleep(REQUEST_DELAY_S)

            # If we got fewer than max, we've reached the end
            if len(raw) < MAX_CANDLES_PER_REQUEST:
                break

        if not all_candles:
            logger.warning(f"No candle data returned for {symbol}")
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        df = pd.DataFrame(all_candles)
        df = df.rename(columns={"t": "timestamp", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        df = df[["timestamp", "open", "high", "low", "close", "volume"]]

        # Convert types
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")

        # Remove duplicates and sort
        df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

        # Save to parquet
        path = self._parquet_path(symbol, interval)
        df.to_parquet(path, index=False)
        logger.info(f"Saved {len(df)} candles for {symbol} to {path}")

        return df

    async def fetch_all_assets(
        self,
        min_volume_24h: float = 500_000,
        interval: str = "15m",
        days: int = 180,
    ) -> dict[str, pd.DataFrame]:
        """Download candles for all assets above volume threshold.

        Args:
            min_volume_24h: Minimum 24h USD volume to include.
            interval: Candle interval.
            days: Days of history.

        Returns:
            Dict mapping symbol → DataFrame.
        """
        info = self._get_info()

        # Get all asset contexts for volume filtering
        meta_and_ctxs = await asyncio.to_thread(info.meta_and_asset_ctxs)
        meta = meta_and_ctxs[0]
        asset_ctxs = meta_and_ctxs[1]

        symbols: list[str] = []
        for asset_info, ctx in zip(meta["universe"], asset_ctxs):
            symbol = asset_info["name"]
            vol_24h = float(ctx.get("dayNtlVlm", 0))
            if vol_24h >= min_volume_24h:
                symbols.append(symbol)

        logger.info(f"Found {len(symbols)} assets with 24h volume >= ${min_volume_24h:,.0f}")

        results: dict[str, pd.DataFrame] = {}
        for i, symbol in enumerate(symbols):
            try:
                logger.info(f"[{i + 1}/{len(symbols)}] Fetching {symbol}...")
                df = await self.fetch_candles(symbol, interval, days)
                if not df.empty:
                    results[symbol] = df
            except Exception as e:
                logger.error(f"Failed to fetch {symbol}: {e}")
                continue

        logger.info(f"Fetched candle data for {len(results)}/{len(symbols)} assets")
        return results

    def load_candles(self, symbol: str, interval: str = "15m") -> pd.DataFrame:
        """Load cached candle data from Parquet file.

        Args:
            symbol: Trading pair symbol.
            interval: Candle interval.

        Returns:
            DataFrame with candle data.

        Raises:
            FileNotFoundError: If no cached data exists for this symbol/interval.
        """
        path = self._parquet_path(symbol, interval)
        if not path.exists():
            raise FileNotFoundError(f"No cached data for {symbol} ({interval}) at {path}")
        return pd.read_parquet(path)

    def list_available(self) -> list[str]:
        """List symbols with cached Parquet data."""
        symbols: list[str] = []
        for f in sorted(self.data_dir.glob("*.parquet")):
            # Filename format: {SYMBOL}_{interval}.parquet
            name = f.stem
            parts = name.rsplit("_", 1)
            if parts:
                symbols.append(parts[0])
        return symbols

    def _parquet_path(self, symbol: str, interval: str) -> Path:
        """Build the Parquet file path for a symbol/interval."""
        return self.data_dir / f"{symbol}_{interval}.parquet"
