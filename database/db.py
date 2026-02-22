"""
HLQuantBot Database Module
==========================
Classe Database per operazioni CRUD asincrone con PostgreSQL.
Usa asyncpg per performance ottimali.

Uso:
    db = Database()
    await db.connect()
    
    # Operazioni...
    account = await db.get_account()
    await db.upsert_positions(positions)
    
    await db.disconnect()
"""

import json
import os

from dotenv import load_dotenv
load_dotenv()
from datetime import datetime
from typing import Any, Optional

import asyncpg

# Configurazione da environment o default
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://trader:trader_password@localhost:5432/trading_db"
)


class Database:
    """
    Client database asincrono per HLQuantBot.
    Gestisce connessioni pooled e operazioni CRUD.
    """
    
    def __init__(self, database_url: str = DATABASE_URL):
        self.database_url = database_url
        self.pool: Optional[asyncpg.Pool] = None
    
    # =========================================================================
    # CONNECTION MANAGEMENT
    # =========================================================================
    
    async def connect(self, min_size: int = 2, max_size: int = 10) -> None:
        """Crea connection pool."""
        self.pool = await asyncpg.create_pool(
            self.database_url,
            min_size=min_size,
            max_size=max_size,
            command_timeout=60
        )
        print(f"[DB] Connected to PostgreSQL (pool: {min_size}-{max_size})")
    
    async def disconnect(self) -> None:
        """Chiude connection pool."""
        if self.pool:
            await self.pool.close()
            print("[DB] Disconnected from PostgreSQL")
    
    async def health_check(self) -> bool:
        """Verifica connessione al database."""
        try:
            async with self.pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception:
            return False

    async def fetch(self, query: str, *args) -> list:
        """Execute query and return all rows."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *args)
            return rows

    async def fetchrow(self, query: str, *args) -> Optional[dict]:
        """Execute query and return single row."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, *args)
            return row

    async def fetchval(self, query: str, *args) -> Any:
        """Execute query and return single value."""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(query, *args)

    async def execute(self, query: str, *args) -> str:
        """Execute a query without returning results (INSERT, UPDATE, DELETE)."""
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)
    
    # =========================================================================
    # Cooldowns
    # =========================================================================

    async def insert_cooldown(
        self,
        reason: str,
        triggered_at: "datetime",
        cooldown_until: "datetime",
        details: Optional[dict] = None
    ) -> int:
        """
        Insert a new cooldown record.

        Args:
            reason: Cooldown reason (StoplossStreak, DailyDrawdown, LowPerformance)
            triggered_at: When cooldown was triggered
            cooldown_until: When cooldown expires
            details: Extra context as dict

        Returns:
            ID of inserted record
        """
        import json
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO cooldowns (reason, triggered_at, cooldown_until, details)
                VALUES ($1, $2, $3, $4)
                RETURNING id
                """,
                reason,
                triggered_at,
                cooldown_until,
                json.dumps(details) if details else None
            )
            return row["id"] if row else 0

    async def get_active_cooldown(self) -> Optional[dict]:
        """
        Get the currently active cooldown (if any).

        Returns:
            Dict with cooldown data or None if no active cooldown
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, reason, triggered_at, cooldown_until, details, created_at
                FROM cooldowns
                WHERE cooldown_until > NOW()
                ORDER BY triggered_at DESC
                LIMIT 1
                """
            )
            return dict(row) if row else None

    async def get_cooldown_history(self, limit: int = 50) -> list[dict]:
        """
        Get cooldown history.

        Args:
            limit: Max number of records to return

        Returns:
            List of cooldown records
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, reason, triggered_at, cooldown_until, details, created_at
                FROM cooldowns
                ORDER BY triggered_at DESC
                LIMIT $1
                """,
                limit
            )
            return [dict(row) for row in rows]


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

async def get_database() -> Database:
    """Factory per creare istanza Database connessa."""
    db = Database()
    await db.connect()
    return db
