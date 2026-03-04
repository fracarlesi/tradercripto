"""Async SQLite service for storing market data snapshots."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

# DB lives in <project_root>/data/market_snapshots.db
_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_DB_PATH = _DATA_DIR / "market_snapshots.db"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    funding_rate REAL,
    open_interest REAL,
    close_price REAL
);
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_snapshots_symbol_ts
ON snapshots(symbol, timestamp);
"""


class MarketSnapshotService:
    """Lightweight async wrapper around an SQLite database of market snapshots."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or _DB_PATH
        self._db: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def init(self) -> None:
        """Create the data directory, open the database, and ensure the table exists."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        await self._db.execute(_CREATE_TABLE)
        await self._db.execute(_CREATE_INDEX)
        await self._db.commit()
        logger.info("MarketSnapshotService initialised — db=%s", self._db_path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None
            logger.info("MarketSnapshotService closed")

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def save_snapshot(
        self,
        symbol: str,
        timestamp: str,
        funding_rate: float | None,
        open_interest: float | None,
        close_price: float | None,
    ) -> None:
        """Insert a single market snapshot row."""
        assert self._db is not None, "call init() first"
        await self._db.execute(
            """
            INSERT INTO snapshots (symbol, timestamp, funding_rate, open_interest, close_price)
            VALUES (?, ?, ?, ?, ?)
            """,
            (symbol, timestamp, funding_rate, open_interest, close_price),
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get_snapshots(
        self,
        symbol: str,
        start_ts: str,
        end_ts: str,
    ) -> list[dict[str, Any]]:
        """Return snapshots for *symbol* whose timestamp falls in [start_ts, end_ts]."""
        assert self._db is not None, "call init() first"
        cursor = await self._db.execute(
            """
            SELECT id, symbol, timestamp, funding_rate, open_interest, close_price
            FROM snapshots
            WHERE symbol = ? AND timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp ASC
            """,
            (symbol, start_ts, end_ts),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "symbol": row[1],
                "timestamp": row[2],
                "funding_rate": row[3],
                "open_interest": row[4],
                "close_price": row[5],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    async def cleanup(self, days: int = 90) -> int:
        """Delete snapshots older than *days* days. Returns the number of deleted rows."""
        assert self._db is not None, "call init() first"
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cursor = await self._db.execute(
            "DELETE FROM snapshots WHERE timestamp < ?",
            (cutoff,),
        )
        await self._db.commit()
        deleted = cursor.rowcount
        logger.info("Cleaned up %d snapshots older than %d days", deleted, days)
        return deleted
