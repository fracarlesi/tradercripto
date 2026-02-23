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
from decimal import Decimal
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


    # =========================================================================
    # LLM Decisions
    # =========================================================================

    async def insert_llm_decision(
        self,
        *,
        symbol: str,
        direction: str,
        regime: str,
        entry_price: Decimal,
        stop_price: Decimal,
        tp_price: Decimal,
        decision: str,
        confidence: Decimal,
        reason: str,
        latency_ms: int,
        adx: Optional[Decimal] = None,
        rsi: Optional[Decimal] = None,
        atr: Optional[Decimal] = None,
        ema9: Optional[Decimal] = None,
        ema21: Optional[Decimal] = None,
        volume_ratio: Optional[Decimal] = None,
    ) -> int:
        """Insert a new LLM decision record.

        Returns:
            ID of inserted record.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO llm_decisions (
                    symbol, direction, regime, entry_price, stop_price, tp_price,
                    adx, rsi, atr, ema9, ema21, volume_ratio,
                    decision, confidence, reason, latency_ms
                ) VALUES (
                    $1, $2, $3, $4, $5, $6,
                    $7, $8, $9, $10, $11, $12,
                    $13, $14, $15, $16
                )
                RETURNING id
                """,
                symbol, direction, regime, entry_price, stop_price, tp_price,
                adx, rsi, atr, ema9, ema21, volume_ratio,
                decision, confidence, reason, latency_ms,
            )
            return row["id"] if row else 0

    async def get_pending_llm_decisions(
        self, max_age_hours: int = 4
    ) -> list[dict]:
        """Get unresolved LLM decisions within max_age_hours.

        Returns:
            List of pending decision dicts.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, decided_at, symbol, direction, regime,
                       entry_price, stop_price, tp_price,
                       decision, max_favorable_pct, max_adverse_pct,
                       price_5m, price_15m, price_30m, price_1h, price_2h, price_4h
                FROM llm_decisions
                WHERE resolved_at IS NULL
                  AND decided_at > NOW() - make_interval(hours => $1)
                ORDER BY decided_at ASC
                """,
                max_age_hours,
            )
            return [dict(row) for row in rows]

    async def update_decision_checkpoint(
        self,
        decision_id: int,
        decided_at: datetime,
        column: str,
        price: Decimal,
        favorable_pct: Decimal,
        adverse_pct: Decimal,
    ) -> None:
        """Update a price checkpoint and running MFE/MAE for a decision.

        Args:
            decision_id: Row ID.
            decided_at: Timestamp (needed for hypertable partition key).
            column: Checkpoint column name (price_5m, price_15m, etc.).
            price: Current price.
            favorable_pct: New candidate favorable %.
            adverse_pct: New candidate adverse %.
        """
        # Validate column name to prevent SQL injection
        valid_columns = {"price_5m", "price_15m", "price_30m",
                         "price_1h", "price_2h", "price_4h"}
        if column not in valid_columns:
            raise ValueError(f"Invalid checkpoint column: {column}")

        async with self.pool.acquire() as conn:
            await conn.execute(
                f"""
                UPDATE llm_decisions
                SET {column} = $1,
                    max_favorable_pct = GREATEST(max_favorable_pct, $2),
                    max_adverse_pct = GREATEST(max_adverse_pct, $3)
                WHERE id = $4 AND decided_at = $5
                """,
                price, favorable_pct, adverse_pct,
                decision_id, decided_at,
            )

    async def resolve_llm_decision(
        self,
        decision_id: int,
        decided_at: datetime,
        first_hit: str,
        time_to_hit_min: Optional[int],
        was_correct: bool,
    ) -> None:
        """Mark a decision as resolved with outcome.

        Args:
            decision_id: Row ID.
            decided_at: Timestamp (partition key).
            first_hit: "tp", "sl", or "neither".
            time_to_hit_min: Minutes from decision to first hit.
            was_correct: Whether the LLM decision was correct.
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE llm_decisions
                SET first_hit = $1,
                    time_to_hit_min = $2,
                    was_correct = $3,
                    resolved_at = NOW()
                WHERE id = $4 AND decided_at = $5
                """,
                first_hit, time_to_hit_min, was_correct,
                decision_id, decided_at,
            )

    async def get_llm_performance(self, days: int = 7) -> dict:
        """Get aggregate LLM decision performance stats.

        Returns dict with confusion matrix, MFE/MAE averages, hit rates.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE resolved_at IS NOT NULL) AS resolved,
                    COUNT(*) FILTER (WHERE decision = 'ALLOW') AS total_allow,
                    COUNT(*) FILTER (WHERE decision = 'DENY') AS total_deny,
                    COUNT(*) FILTER (WHERE was_correct = TRUE) AS correct,
                    COUNT(*) FILTER (WHERE was_correct = FALSE) AS incorrect,
                    COUNT(*) FILTER (WHERE first_hit = 'tp') AS hit_tp,
                    COUNT(*) FILTER (WHERE first_hit = 'sl') AS hit_sl,
                    COUNT(*) FILTER (WHERE first_hit = 'neither') AS hit_neither,
                    AVG(max_favorable_pct) FILTER (WHERE resolved_at IS NOT NULL) AS avg_mfe,
                    AVG(max_adverse_pct) FILTER (WHERE resolved_at IS NOT NULL) AS avg_mae,
                    AVG(time_to_hit_min) FILTER (WHERE time_to_hit_min IS NOT NULL) AS avg_time_to_hit,
                    -- Confusion matrix
                    COUNT(*) FILTER (WHERE decision='ALLOW' AND first_hit='tp') AS allow_tp,
                    COUNT(*) FILTER (WHERE decision='ALLOW' AND first_hit='sl') AS allow_sl,
                    COUNT(*) FILTER (WHERE decision='DENY'  AND first_hit='tp') AS deny_tp,
                    COUNT(*) FILTER (WHERE decision='DENY'  AND first_hit='sl') AS deny_sl
                FROM llm_decisions
                WHERE decided_at > NOW() - make_interval(days => $1)
                """,
                days,
            )
            if not row:
                return {"total": 0}

            total = row["total"]
            resolved = row["resolved"]
            correct = row["correct"]

            return {
                "total": total,
                "resolved": resolved,
                "total_allow": row["total_allow"],
                "total_deny": row["total_deny"],
                "correct": correct,
                "incorrect": row["incorrect"],
                "accuracy": round(correct / resolved, 3) if resolved > 0 else None,
                "hit_tp": row["hit_tp"],
                "hit_sl": row["hit_sl"],
                "hit_neither": row["hit_neither"],
                "avg_mfe_pct": float(row["avg_mfe"]) if row["avg_mfe"] else None,
                "avg_mae_pct": float(row["avg_mae"]) if row["avg_mae"] else None,
                "avg_time_to_hit_min": float(row["avg_time_to_hit"]) if row["avg_time_to_hit"] else None,
                "confusion_matrix": {
                    "allow_tp": row["allow_tp"],
                    "allow_sl": row["allow_sl"],
                    "deny_tp": row["deny_tp"],
                    "deny_sl": row["deny_sl"],
                },
            }


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

async def get_database() -> Database:
    """Factory per creare istanza Database connessa."""
    db = Database()
    await db.connect()
    return db
