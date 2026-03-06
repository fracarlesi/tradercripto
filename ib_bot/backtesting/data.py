"""
IB Backtesting - Historical Data Fetcher
==========================================

Fetches 1-min OHLCV bars from Interactive Brokers and caches them
as JSON files to avoid redundant API calls.

Key design decisions:
- Uses client_id=2 to avoid conflicts with live bot (client_id=1)
- Respects IB pacing rules (~60 requests per 10 minutes)
- All prices stored as Decimal strings in cache for precision
- Cache key: {symbol}_{date}.json
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from ib_insync import IB, Future

from ..core.contracts import CONTRACTS
from .session import ET, get_trading_days

logger = logging.getLogger(__name__)


def _front_month_expiry(day: date) -> str:
    """Calculate front-month futures expiry for a given date (YYYYMM).

    Quarterly months: Mar(H), Jun(M), Sep(U), Dec(Z).
    Switch to next quarter when within 2 weeks of expiry (day > 14).
    """
    year = day.year
    month = day.month
    quarters = [3, 6, 9, 12]
    for q in quarters:
        if month < q:
            return f"{year}{q:02d}"
        if month == q and day.day <= 14:
            return f"{year}{q:02d}"
    return f"{year + 1}03"


class IBDataFetcher:
    """Fetch and cache 1-min bars from Interactive Brokers.

    Uses ib_insync async API with a dedicated client_id (default 2)
    so it can run alongside the live bot.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4002,
        client_id: int = 2,
        cache_dir: str = "ib_bot/backtesting/cache",
    ) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._ib: Optional[IB] = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect to IB Gateway / TWS."""
        self._ib = IB()
        await self._ib.connectAsync(
            self._host, self._port, self._client_id, timeout=15
        )
        logger.info(
            "Connected to IB for data fetch (host=%s, port=%d, clientId=%d)",
            self._host,
            self._port,
            self._client_id,
        )

    async def disconnect(self) -> None:
        """Disconnect from IB Gateway / TWS."""
        if self._ib and self._ib.isConnected():
            self._ib.disconnect()
            logger.info("Disconnected from IB data feed")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_days(
        self, symbol: str, start: date, end: date
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Fetch 1-min bars for all trading days in [start, end], using cache.

        Args:
            symbol: Futures symbol (e.g., "MES", "ES").
            start: First date (inclusive).
            end: Last date (inclusive).

        Returns:
            Mapping of date ISO strings to lists of bar dicts:
            ``{"2026-03-01": [{"dt": datetime, "o": Decimal, ...}, ...]}``
        """
        trading_days = get_trading_days(start, end)
        result: Dict[str, List[Dict[str, Any]]] = {}

        for day in trading_days:
            cache_file = self._cache_path(symbol, day)

            if cache_file.exists():
                bars = self._load_cache(cache_file)
                logger.debug(
                    "Cache hit: %s %s (%d bars)", symbol, day, len(bars)
                )
            else:
                bars = await self._fetch_day(symbol, day)
                if bars:
                    self._save_cache(cache_file, bars)
                    logger.info(
                        "Fetched %s %s: %d bars", symbol, day, len(bars)
                    )
                else:
                    logger.warning("No bars for %s %s", symbol, day)
                    continue

                # IB pacing: ~60 historical data requests per 10 minutes
                # Sleep 1s between requests to stay well within limits
                await asyncio.sleep(1.0)

            result[day.isoformat()] = bars

        logger.info(
            "Data ready: %s %s->%s (%d days, %d total bars)",
            symbol,
            start,
            end,
            len(result),
            sum(len(v) for v in result.values()),
        )
        return result

    # ------------------------------------------------------------------
    # IB data request
    # ------------------------------------------------------------------

    async def _fetch_day(
        self, symbol: str, day: date
    ) -> List[Dict[str, Any]]:
        """Fetch one day of 1-min bars from IB.

        Uses Regular Trading Hours (RTH) only: 09:30-16:00 ET for US equities.
        IB returns bars in UTC with formatDate=2.

        Args:
            symbol: Futures symbol.
            day: Trading date to fetch.

        Returns:
            List of bar dicts with Decimal prices and ET datetimes.
        """
        if self._ib is None or not self._ib.isConnected():
            raise RuntimeError("Not connected to IB. Call connect() first.")

        spec = CONTRACTS.get(symbol)
        if spec is None:
            raise ValueError(
                f"Unknown symbol: {symbol}. "
                f"Available: {list(CONTRACTS.keys())}"
            )

        # Qualify the front-month contract for this specific date
        expiry = _front_month_expiry(day)
        contract = Future(
            symbol,
            lastTradeDateOrContractMonth=expiry,
            exchange=spec.exchange,
            currency=spec.currency,
        )
        qualified = await self._ib.qualifyContractsAsync(contract)
        if not qualified:
            logger.warning(
                "Could not qualify contract for %s on %s", symbol, day
            )
            return []

        # Request end-of-day to get the full day
        end_dt = datetime(day.year, day.month, day.day, 23, 59, 59)

        bars = await self._ib.reqHistoricalDataAsync(
            qualified[0],
            endDateTime=end_dt,
            durationStr="1 D",
            barSizeSetting="1 min",
            whatToShow="TRADES",
            useRTH=True,  # Regular Trading Hours only
            formatDate=2,  # UTC timestamps
        )

        if not bars:
            return []

        result: List[Dict[str, Any]] = []
        for bar in bars:
            dt = bar.date
            # Convert to Eastern Time for session phase classification
            if isinstance(dt, datetime):
                dt_et = dt.astimezone(ET)
            else:
                # Fallback: bar.date is a date object, not datetime
                dt_et = datetime.combine(day, datetime.min.time(), tzinfo=ET)

            result.append(
                {
                    "dt": dt_et,
                    "o": Decimal(str(bar.open)),
                    "h": Decimal(str(bar.high)),
                    "l": Decimal(str(bar.low)),
                    "c": Decimal(str(bar.close)),
                    "v": Decimal(str(bar.volume)),
                }
            )

        return result

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def _cache_path(self, symbol: str, day: date) -> Path:
        """Build cache file path: cache/{SYMBOL}_{YYYY-MM-DD}.json"""
        return self._cache_dir / f"{symbol}_{day.isoformat()}.json"

    def _load_cache(self, path: Path) -> List[Dict[str, Any]]:
        """Load bars from a JSON cache file.

        Deserialises ISO datetime strings and Decimal-encoded prices.
        """
        with open(path) as f:
            raw = json.load(f)
        return [
            {
                "dt": datetime.fromisoformat(b["dt"]),
                "o": Decimal(b["o"]),
                "h": Decimal(b["h"]),
                "l": Decimal(b["l"]),
                "c": Decimal(b["c"]),
                "v": Decimal(b["v"]),
            }
            for b in raw
        ]

    def _save_cache(self, path: Path, bars: List[Dict[str, Any]]) -> None:
        """Save bars to a JSON cache file.

        Serialises datetimes as ISO strings and Decimals as strings
        to preserve precision.
        """
        raw = [
            {
                "dt": b["dt"].isoformat(),
                "o": str(b["o"]),
                "h": str(b["h"]),
                "l": str(b["l"]),
                "c": str(b["c"]),
                "v": str(b["v"]),
            }
            for b in bars
        ]
        with open(path, "w") as f:
            json.dump(raw, f)

    def clear_cache(self, symbol: str | None = None) -> int:
        """Delete cached data files.

        Args:
            symbol: If provided, only delete cache for this symbol.
                    If None, delete all cached files.

        Returns:
            Number of files deleted.
        """
        pattern = f"{symbol}_*.json" if symbol else "*.json"
        files = list(self._cache_dir.glob(pattern))
        for f in files:
            f.unlink()
        if files:
            logger.info("Cleared %d cache files (pattern=%s)", len(files), pattern)
        return len(files)
