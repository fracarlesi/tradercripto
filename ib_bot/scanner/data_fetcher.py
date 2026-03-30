"""Data fetcher for the IB scanner.

Downloads daily bars via yfinance (bulk, no IB rate limits),
caches results in Parquet files for fast reload.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# Default cache directory
_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "scanner_cache"


class ScannerDataFetcher:
    """Fetches and caches daily OHLCV bars for the scanner universe."""

    def __init__(self, cache_dir: Path | None = None) -> None:
        self.cache_dir = cache_dir or _CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _cache_path(self, symbol: str) -> Path:
        """Return the Parquet cache path for a symbol."""
        safe = symbol.replace("/", "_").replace(".", "_")
        return self.cache_dir / f"{safe}_daily.parquet"

    def is_cache_fresh(self, symbol: str, max_age_hours: int = 16) -> bool:
        """Check whether the cached file exists and is fresh enough.

        Args:
            symbol: Ticker symbol.
            max_age_hours: Maximum age in hours before the cache is stale.

        Returns:
            True if cache is usable.
        """
        path = self._cache_path(symbol)
        if not path.exists():
            return False
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        age = datetime.now(tz=timezone.utc) - mtime
        return age < timedelta(hours=max_age_hours)

    def _read_cache(self, symbol: str) -> pd.DataFrame | None:
        """Read cached Parquet file if it exists."""
        path = self._cache_path(symbol)
        if path.exists():
            try:
                return pd.read_parquet(path)
            except Exception:
                logger.warning("Corrupt cache for %s, will re-download", symbol)
                path.unlink(missing_ok=True)
        return None

    def _write_cache(self, symbol: str, df: pd.DataFrame) -> None:
        """Write DataFrame to Parquet cache."""
        path = self._cache_path(symbol)
        df.to_parquet(path, index=True)

    # ------------------------------------------------------------------
    # Single-symbol download
    # ------------------------------------------------------------------

    async def fetch_daily_bars(
        self, symbol: str, days: int = 60
    ) -> pd.DataFrame:
        """Download daily OHLCV bars for a single symbol.

        Uses the cache if fresh, otherwise downloads via yfinance.

        Args:
            symbol: Ticker symbol (e.g. "AAPL").
            days: Number of calendar days of history to fetch.

        Returns:
            DataFrame with columns [Open, High, Low, Close, Volume].
            Empty DataFrame if the download fails.
        """
        if self.is_cache_fresh(symbol):
            cached = self._read_cache(symbol)
            if cached is not None and len(cached) > 0:
                return cached

        # yfinance is synchronous — run in executor to not block the loop
        loop = asyncio.get_running_loop()
        df = await loop.run_in_executor(None, self._download_symbol, symbol, days)

        if df is not None and len(df) > 0:
            self._write_cache(symbol, df)
            return df

        # Fall back to (possibly stale) cache
        cached = self._read_cache(symbol)
        if cached is not None and len(cached) > 0:
            logger.warning("Using stale cache for %s", symbol)
            return cached

        logger.error("No data available for %s", symbol)
        return pd.DataFrame()

    def _download_symbol(self, symbol: str, days: int) -> pd.DataFrame | None:
        """Synchronous yfinance download for a single symbol."""
        try:
            period = f"{days}d"
            ticker = yf.Ticker(symbol)
            df: pd.DataFrame = ticker.history(period=period, interval="1d")
            if df is None or df.empty:
                logger.warning("Empty data from yfinance for %s", symbol)
                return None
            # Keep only the columns we need
            cols = ["Open", "High", "Low", "Close", "Volume"]
            available = [c for c in cols if c in df.columns]
            return df[available]
        except Exception as e:
            logger.error("Failed to download %s: %s", symbol, e)
            return None

    # ------------------------------------------------------------------
    # Bulk download
    # ------------------------------------------------------------------

    async def fetch_universe(
        self,
        symbols: list[str],
        days: int = 60,
        max_concurrent: int = 8,
    ) -> dict[str, pd.DataFrame]:
        """Download daily bars for a list of symbols in parallel.

        Args:
            symbols: List of ticker symbols.
            days: Calendar days of history per symbol.
            max_concurrent: Maximum concurrent downloads.

        Returns:
            Dict mapping symbol -> DataFrame (empty DataFrames excluded).
        """
        semaphore = asyncio.Semaphore(max_concurrent)
        results: dict[str, pd.DataFrame] = {}

        async def _fetch_one(sym: str) -> None:
            async with semaphore:
                df = await self.fetch_daily_bars(sym, days=days)
                if not df.empty:
                    results[sym] = df

        tasks = [asyncio.create_task(_fetch_one(s)) for s in symbols]
        await asyncio.gather(*tasks, return_exceptions=True)

        logger.info(
            "Fetched %d / %d symbols successfully", len(results), len(symbols)
        )
        return results
