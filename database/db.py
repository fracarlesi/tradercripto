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

import asyncio
import json
import os
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

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
    
    # =========================================================================
    # LIVE ACCOUNT
    # =========================================================================
    
    async def get_account(self) -> Optional[dict]:
        """Recupera stato account."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM live_account WHERE id = 1")
            return dict(row) if row else None
    
    async def update_account(
        self,
        equity: Decimal,
        available_balance: Decimal,
        margin_used: Decimal,
        unrealized_pnl: Decimal
    ) -> None:
        """Aggiorna stato account."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE live_account SET
                    equity = $1,
                    available_balance = $2,
                    margin_used = $3,
                    unrealized_pnl = $4,
                    updated_at = NOW()
                WHERE id = 1
            """, equity, available_balance, margin_used, unrealized_pnl)
    
    # =========================================================================
    # LIVE POSITIONS
    # =========================================================================
    
    async def get_positions(self) -> list[dict]:
        """Recupera tutte le posizioni aperte."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM live_positions ORDER BY symbol")
            return [dict(row) for row in rows]
    
    async def get_position(self, symbol: str) -> Optional[dict]:
        """Recupera posizione per simbolo."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM live_positions WHERE symbol = $1", 
                symbol
            )
            return dict(row) if row else None
    
    async def upsert_positions(self, positions: list[dict]) -> None:
        """
        Aggiorna posizioni usando ON CONFLICT per evitare race conditions.
        Scrive su entrambe le tabelle: live_positions e realtime_positions.
        Rimuove posizioni non più presenti (closed).
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Get current symbols to track which to delete
                current_symbols = {p["symbol"] for p in positions}

                # Delete positions that are no longer open
                if current_symbols:
                    await conn.execute(
                        "DELETE FROM live_positions WHERE symbol != ALL($1::text[])",
                        list(current_symbols)
                    )
                    await conn.execute(
                        "DELETE FROM realtime_positions WHERE symbol != ALL($1::text[])",
                        list(current_symbols)
                    )
                else:
                    # No positions - delete all
                    await conn.execute("DELETE FROM live_positions")
                    await conn.execute("DELETE FROM realtime_positions")

                # Upsert each position
                for p in positions:
                    # live_positions
                    await conn.execute("""
                        INSERT INTO live_positions
                        (symbol, side, size, entry_price, mark_price, unrealized_pnl,
                         leverage, liquidation_price, margin_used)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        ON CONFLICT (symbol) DO UPDATE SET
                            side = EXCLUDED.side,
                            size = EXCLUDED.size,
                            entry_price = EXCLUDED.entry_price,
                            mark_price = EXCLUDED.mark_price,
                            unrealized_pnl = EXCLUDED.unrealized_pnl,
                            leverage = EXCLUDED.leverage,
                            liquidation_price = EXCLUDED.liquidation_price,
                            margin_used = EXCLUDED.margin_used
                    """,
                        p["symbol"],
                        p["side"],
                        p["size"],
                        p["entry_price"],
                        p["mark_price"],
                        p["unrealized_pnl"],
                        p["leverage"],
                        p.get("liquidation_price"),
                        p["margin_used"]
                    )

                    # realtime_positions (frontend)
                    await conn.execute("""
                        INSERT INTO realtime_positions
                        (symbol, side, size, entry_price, mark_price, unrealized_pnl,
                         unrealized_pnl_pct, leverage, stop_loss_price, take_profit_price,
                         liquidation_price, margin_used, strategy_id, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, NOW())
                        ON CONFLICT (symbol) DO UPDATE SET
                            side = EXCLUDED.side,
                            size = EXCLUDED.size,
                            entry_price = EXCLUDED.entry_price,
                            mark_price = EXCLUDED.mark_price,
                            unrealized_pnl = EXCLUDED.unrealized_pnl,
                            unrealized_pnl_pct = EXCLUDED.unrealized_pnl_pct,
                            leverage = EXCLUDED.leverage,
                            stop_loss_price = EXCLUDED.stop_loss_price,
                            take_profit_price = EXCLUDED.take_profit_price,
                            liquidation_price = EXCLUDED.liquidation_price,
                            margin_used = EXCLUDED.margin_used,
                            strategy_id = EXCLUDED.strategy_id,
                            updated_at = NOW()
                    """,
                        p["symbol"],
                        p["side"].lower(),  # Frontend expects lowercase (long/short)
                        p["size"],
                        p["entry_price"],
                        p["mark_price"],
                        p["unrealized_pnl"],
                        self._calc_pnl_pct(p),
                        p["leverage"],
                        p.get("stop_loss_price"),
                        p.get("take_profit_price"),
                        p.get("liquidation_price"),
                        p["margin_used"],
                        p.get("strategy_id")
                    )

    def _calc_pnl_pct(self, p: dict) -> float:
        """Calcola PnL percentuale."""
        entry = p.get("entry_price", 0)
        size = p.get("size", 0)
        pnl = p.get("unrealized_pnl", 0)
        if entry and size:
            notional = entry * size
            return (pnl / notional) * 100 if notional else 0
        return 0
    
    # =========================================================================
    # LIVE ORDERS
    # =========================================================================
    
    async def get_orders(self) -> list[dict]:
        """Recupera tutti gli ordini aperti."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM live_orders ORDER BY created_at DESC")
            return [dict(row) for row in rows]
    
    async def upsert_orders(self, orders: list[dict]) -> None:
        """
        Aggiorna ordini: cancella tutti e reinserisce.
        Approccio semplice per sync completo da Hyperliquid.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM live_orders")
                
                if orders:
                    await conn.executemany("""
                        INSERT INTO live_orders 
                        (order_id, symbol, side, size, price, order_type, reduce_only, created_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """, [
                        (
                            o["order_id"],
                            o["symbol"],
                            o["side"],
                            o["size"],
                            o["price"],
                            o["order_type"],
                            o.get("reduce_only", False),
                            o["created_at"]
                        )
                        for o in orders
                    ])
    
    # =========================================================================
    # FILLS (STORICO)
    # =========================================================================
    
    async def insert_fill(self, fill: dict) -> bool:
        """
        Inserisce un fill. Ritorna True se inserito, False se duplicato.
        """
        async with self.pool.acquire() as conn:
            try:
                await conn.execute("""
                    INSERT INTO fills 
                    (fill_id, order_id, symbol, side, size, price, fee, fee_token, 
                     fill_time, closed_pnl, is_maker)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                    fill["fill_id"],
                    fill["order_id"],
                    fill["symbol"],
                    fill["side"],
                    fill["size"],
                    fill["price"],
                    fill["fee"],
                    fill.get("fee_token", "USDC"),
                    fill["fill_time"],
                    fill.get("closed_pnl"),
                    fill.get("is_maker")
                )
                return True
            except asyncpg.UniqueViolationError:
                return False  # Fill gia presente
    
    async def insert_fills_batch(self, fills: list[dict]) -> int:
        """
        Inserisce fills in batch. Ritorna numero di fills effettivamente inseriti (ignora duplicati).
        """
        if not fills:
            return 0

        inserted = 0
        async with self.pool.acquire() as conn:
            for fill in fills:
                try:
                    # execute() returns status string like "INSERT 0 1" where last number is rows inserted
                    # ON CONFLICT DO NOTHING returns "INSERT 0 0" when skipped
                    result = await conn.execute("""
                        INSERT INTO fills
                        (fill_id, order_id, symbol, side, size, price, fee, fee_token,
                         fill_time, closed_pnl, is_maker)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                        ON CONFLICT (fill_id) DO NOTHING
                    """,
                        fill["fill_id"],
                        fill["order_id"],
                        fill["symbol"],
                        fill["side"],
                        fill["size"],
                        fill["price"],
                        fill["fee"],
                        fill.get("fee_token", "USDC"),
                        fill["fill_time"],
                        fill.get("closed_pnl"),
                        fill.get("is_maker")
                    )
                    # Check if row was actually inserted (result ends with "1" not "0")
                    if result and result.endswith("1"):
                        inserted += 1
                except Exception:
                    pass
        return inserted
    
    async def get_fills(
        self, 
        symbol: Optional[str] = None, 
        limit: int = 100,
        since: Optional[datetime] = None
    ) -> list[dict]:
        """Recupera fills con filtri opzionali."""
        query = "SELECT * FROM fills WHERE 1=1"
        params = []
        param_idx = 1
        
        if symbol:
            query += f" AND symbol = ${param_idx}"
            params.append(symbol)
            param_idx += 1
        
        if since:
            query += f" AND fill_time >= ${param_idx}"
            params.append(since)
            param_idx += 1
        
        query += f" ORDER BY fill_time DESC LIMIT ${param_idx}"
        params.append(limit)
        
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [dict(row) for row in rows]
    
    async def get_last_fill_id(self) -> Optional[int]:
        """Recupera ultimo fill_id per sync incrementale."""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT MAX(fill_id) FROM fills"
            )
    
    # =========================================================================
    # TRADES
    # =========================================================================
    
    async def create_trade(self, trade: dict) -> UUID:
        """Crea un nuovo trade (aperto)."""
        async with self.pool.acquire() as conn:
            trade_id = await conn.fetchval("""
                INSERT INTO trades 
                (symbol, side, size, entry_price, entry_time, entry_fill_ids, strategy)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING trade_id
            """,
                trade["symbol"],
                trade["side"],
                trade["size"],
                trade["entry_price"],
                trade["entry_time"],
                trade["entry_fill_ids"],
                trade.get("strategy")
            )
            return trade_id
    
    async def close_trade(
        self,
        trade_id: UUID,
        exit_price: Decimal,
        exit_time: datetime,
        exit_fill_ids: list[int],
        gross_pnl: Decimal,
        fees: Decimal,
        net_pnl: Decimal,
        duration_seconds: int
    ) -> None:
        """Chiude un trade esistente."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE trades SET
                    exit_price = $2,
                    exit_time = $3,
                    exit_fill_ids = $4,
                    gross_pnl = $5,
                    fees = $6,
                    net_pnl = $7,
                    duration_seconds = $8,
                    is_closed = TRUE
                WHERE trade_id = $1
            """, trade_id, exit_price, exit_time, exit_fill_ids, 
                gross_pnl, fees, net_pnl, duration_seconds)
    
    async def get_open_trades(self, symbol: Optional[str] = None) -> list[dict]:
        """Recupera trade aperti."""
        query = "SELECT * FROM trades WHERE is_closed = FALSE"
        params = []
        
        if symbol:
            query += " AND symbol = $1"
            params.append(symbol)
        
        query += " ORDER BY entry_time DESC"
        
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [dict(row) for row in rows]
    
    async def get_trades(
        self,
        symbol: Optional[str] = None,
        strategy: Optional[str] = None,
        is_closed: Optional[bool] = None,
        limit: int = 100
    ) -> list[dict]:
        """Recupera trades con filtri."""
        query = "SELECT * FROM trades WHERE 1=1"
        params = []
        param_idx = 1
        
        if symbol:
            query += f" AND symbol = ${param_idx}"
            params.append(symbol)
            param_idx += 1
        
        if strategy:
            query += f" AND strategy = ${param_idx}"
            params.append(strategy)
            param_idx += 1
        
        if is_closed is not None:
            query += f" AND is_closed = ${param_idx}"
            params.append(is_closed)
            param_idx += 1
        
        query += f" ORDER BY entry_time DESC LIMIT ${param_idx}"
        params.append(limit)
        
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [dict(row) for row in rows]
    
    # =========================================================================
    # SIGNALS
    # =========================================================================
    
    async def insert_signal(self, signal: dict) -> UUID:
        """Inserisce un segnale."""
        async with self.pool.acquire() as conn:
            signal_id = await conn.fetchval("""
                INSERT INTO signals 
                (symbol, strategy, side, signal_type, confidence, reason)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING signal_id
            """,
                signal["symbol"],
                signal["strategy"],
                signal["side"],
                signal["signal_type"],
                signal.get("confidence"),
                signal.get("reason")
            )
            return signal_id
    
    async def mark_signal_executed(
        self,
        signal_id: UUID,
        order_id: int,
        execution_price: Decimal
    ) -> None:
        """Marca un segnale come eseguito."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE signals SET
                    executed = TRUE,
                    order_id = $2,
                    execution_price = $3
                WHERE signal_id = $1
            """, signal_id, order_id, execution_price)
    
    async def mark_signal_rejected(self, signal_id: UUID, reason: str) -> None:
        """Marca un segnale come rifiutato."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE signals SET
                    executed = FALSE,
                    rejected_reason = $2
                WHERE signal_id = $1
            """, signal_id, reason)
    
    async def get_signals(
        self,
        symbol: Optional[str] = None,
        strategy: Optional[str] = None,
        executed: Optional[bool] = None,
        limit: int = 100
    ) -> list[dict]:
        """Recupera segnali con filtri."""
        query = "SELECT * FROM signals WHERE 1=1"
        params = []
        param_idx = 1
        
        if symbol:
            query += f" AND symbol = ${param_idx}"
            params.append(symbol)
            param_idx += 1
        
        if strategy:
            query += f" AND strategy = ${param_idx}"
            params.append(strategy)
            param_idx += 1
        
        if executed is not None:
            query += f" AND executed = ${param_idx}"
            params.append(executed)
            param_idx += 1
        
        query += f" ORDER BY timestamp DESC LIMIT ${param_idx}"
        params.append(limit)
        
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [dict(row) for row in rows]
    
    # =========================================================================
    # DAILY SUMMARY
    # =========================================================================
    
    async def upsert_daily_summary(self, summary: dict) -> None:
        """Inserisce o aggiorna summary giornaliero."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO daily_summary 
                (date, starting_equity, ending_equity, trades_count, win_count, loss_count,
                 gross_pnl, fees, net_pnl, max_drawdown, max_equity, min_equity,
                 pnl_by_symbol, pnl_by_strategy)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                ON CONFLICT (date) DO UPDATE SET
                    ending_equity = EXCLUDED.ending_equity,
                    trades_count = EXCLUDED.trades_count,
                    win_count = EXCLUDED.win_count,
                    loss_count = EXCLUDED.loss_count,
                    gross_pnl = EXCLUDED.gross_pnl,
                    fees = EXCLUDED.fees,
                    net_pnl = EXCLUDED.net_pnl,
                    max_drawdown = EXCLUDED.max_drawdown,
                    max_equity = EXCLUDED.max_equity,
                    min_equity = EXCLUDED.min_equity,
                    pnl_by_symbol = EXCLUDED.pnl_by_symbol,
                    pnl_by_strategy = EXCLUDED.pnl_by_strategy,
                    updated_at = NOW()
            """,
                summary["date"],
                summary["starting_equity"],
                summary["ending_equity"],
                summary.get("trades_count", 0),
                summary.get("win_count", 0),
                summary.get("loss_count", 0),
                summary.get("gross_pnl", Decimal("0")),
                summary.get("fees", Decimal("0")),
                summary.get("net_pnl", Decimal("0")),
                summary.get("max_drawdown"),
                summary.get("max_equity"),
                summary.get("min_equity"),
                summary.get("pnl_by_symbol"),
                summary.get("pnl_by_strategy")
            )
    
    async def get_daily_summaries(self, days: int = 30) -> list[dict]:
        """Recupera ultimi N giorni di summary."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM daily_summary 
                ORDER BY date DESC 
                LIMIT $1
            """, days)
            return [dict(row) for row in rows]
    
    async def get_daily_summary(self, target_date: date) -> Optional[dict]:
        """Recupera summary per data specifica."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM daily_summary WHERE date = $1",
                target_date
            )
            return dict(row) if row else None
    
    # =========================================================================
    # STATISTICHE
    # =========================================================================
    
    async def get_stats(self) -> dict:
        """Statistiche aggregate."""
        async with self.pool.acquire() as conn:
            # Totale trades
            total_trades = await conn.fetchval(
                "SELECT COUNT(*) FROM trades WHERE is_closed = TRUE"
            )
            
            # Win rate
            wins = await conn.fetchval(
                "SELECT COUNT(*) FROM trades WHERE is_closed = TRUE AND net_pnl > 0"
            )
            
            # PnL totale
            total_pnl = await conn.fetchval(
                "SELECT COALESCE(SUM(net_pnl), 0) FROM trades WHERE is_closed = TRUE"
            )
            
            # Total fees
            total_fees = await conn.fetchval(
                "SELECT COALESCE(SUM(fees), 0) FROM trades WHERE is_closed = TRUE"
            )
            
            return {
                "total_trades": total_trades or 0,
                "wins": wins or 0,
                "losses": (total_trades or 0) - (wins or 0),
                "win_rate": round((wins or 0) / total_trades * 100, 2) if total_trades else 0,
                "total_pnl": total_pnl or Decimal("0"),
                "total_fees": total_fees or Decimal("0")
            }

    # =========================================================================
    # MARKET SNAPSHOTS
    # =========================================================================
    
    async def insert_market_snapshot(self, data: dict) -> int:
        """
        Inserisce uno snapshot di mercato.
        
        Args:
            data: Dict con dati per ogni coin {symbol: {price, volume_24h, ...}}
        
        Returns:
            snapshot_id del record inserito
        """
        async with self.pool.acquire() as conn:
            snapshot_id = await conn.fetchval("""
                INSERT INTO market_snapshots (timestamp, data, coins_count)
                VALUES (NOW(), $1, $2)
                RETURNING snapshot_id
            """, data, len(data))
            return snapshot_id
    
    async def get_latest_market_snapshot(self) -> Optional[dict]:
        """
        Recupera l'ultimo snapshot di mercato.
        
        Returns:
            Dict con snapshot_id, timestamp, data, coins_count o None
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT snapshot_id, timestamp, data, coins_count, created_at
                FROM market_snapshots
                ORDER BY timestamp DESC
                LIMIT 1
            """)
            return dict(row) if row else None
    
    # =========================================================================
    # OPPORTUNITY RANKINGS
    # =========================================================================
    
    async def insert_opportunity_ranking(
        self,
        rankings: list,
        regime: str,
        btc_price: Optional[Decimal] = None,
        total_volume_24h: Optional[Decimal] = None
    ) -> int:
        """
        Inserisce un ranking di opportunita.
        
        Args:
            rankings: Lista di dict [{symbol, score, factors, ...}]
            regime: Market regime (bullish, bearish, neutral, volatile)
            btc_price: Prezzo BTC corrente
            total_volume_24h: Volume totale mercato 24h
        
        Returns:
            ranking_id del record inserito
        """
        async with self.pool.acquire() as conn:
            ranking_id = await conn.fetchval("""
                INSERT INTO opportunity_rankings 
                (timestamp, rankings, market_regime, btc_price, total_volume_24h)
                VALUES (NOW(), $1, $2, $3, $4)
                RETURNING ranking_id
            """, rankings, regime, btc_price, total_volume_24h)
            return ranking_id
    
    async def get_latest_rankings(self) -> Optional[list]:
        """
        Recupera l'ultimo ranking di opportunita.
        
        Returns:
            Lista di rankings o None
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT rankings, market_regime, btc_price, total_volume_24h, timestamp
                FROM opportunity_rankings
                ORDER BY timestamp DESC
                LIMIT 1
            """)
            if row:
                return {
                    "rankings": row["rankings"],
                    "market_regime": row["market_regime"],
                    "btc_price": row["btc_price"],
                    "total_volume_24h": row["total_volume_24h"],
                    "timestamp": row["timestamp"]
                }
            return None
    
    # =========================================================================
    # STRATEGY DECISIONS
    # =========================================================================
    
    async def insert_strategy_decision(self, decision: dict) -> int:
        """
        Inserisce una decisione strategica.
        
        Args:
            decision: Dict con:
                - symbol: str (required)
                - selected_strategy: str (required)
                - confidence: float (optional)
                - llm_reasoning: str (optional)
                - input_context: dict (optional)
                - trade_id: UUID (optional)
        
        Returns:
            decision_id del record inserito
        """
        async with self.pool.acquire() as conn:
            decision_id = await conn.fetchval("""
                INSERT INTO strategy_decisions 
                (timestamp, symbol, selected_strategy, confidence, llm_reasoning, 
                 input_context, trade_id, outcome)
                VALUES (NOW(), $1, $2, $3, $4, $5, $6, 'pending')
                RETURNING decision_id
            """,
                decision["symbol"],
                decision["selected_strategy"],
                decision.get("confidence"),
                decision.get("llm_reasoning"),
                decision.get("input_context"),
                decision.get("trade_id")
            )
            return decision_id
    
    async def update_strategy_decision_outcome(
        self,
        decision_id: int,
        outcome: str,
        pnl: Optional[Decimal] = None
    ) -> None:
        """
        Aggiorna l'outcome di una decisione strategica.
        
        Args:
            decision_id: ID della decisione
            outcome: win, loss, cancelled
            pnl: PnL risultante (opzionale)
        """
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE strategy_decisions
                SET outcome = $2, pnl = $3
                WHERE decision_id = $1
            """, decision_id, outcome, pnl)
    
    async def get_strategy_decisions(
        self,
        symbol: Optional[str] = None,
        strategy: Optional[str] = None,
        limit: int = 100
    ) -> list[dict]:
        """
        Recupera decisioni strategiche con filtri.
        
        Args:
            symbol: Filtra per simbolo
            strategy: Filtra per strategia
            limit: Numero massimo di risultati
        
        Returns:
            Lista di dict con le decisioni
        """
        query = "SELECT * FROM strategy_decisions WHERE 1=1"
        params = []
        param_idx = 1
        
        if symbol:
            query += f" AND symbol = ${param_idx}"
            params.append(symbol)
            param_idx += 1
        
        if strategy:
            query += f" AND selected_strategy = ${param_idx}"
            params.append(strategy)
            param_idx += 1
        
        query += f" ORDER BY timestamp DESC LIMIT ${param_idx}"
        params.append(limit)
        
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [dict(row) for row in rows]
    
    # =========================================================================
    # CORRELATION MATRIX
    # =========================================================================
    
    async def upsert_correlation_matrix(
        self,
        target_date: date,
        matrix: dict,
        symbols_count: Optional[int] = None,
        avg_correlation: Optional[Decimal] = None
    ) -> None:
        """
        Inserisce o aggiorna la matrice di correlazione per una data.
        
        Args:
            target_date: Data della matrice
            matrix: Dict nested {symbol1: {symbol2: correlation, ...}, ...}
            symbols_count: Numero di simboli nella matrice
            avg_correlation: Correlazione media
        """
        if symbols_count is None:
            symbols_count = len(matrix)
        
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO correlation_matrix (date, matrix, symbols_count, avg_correlation)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (date) DO UPDATE SET
                    matrix = EXCLUDED.matrix,
                    symbols_count = EXCLUDED.symbols_count,
                    avg_correlation = EXCLUDED.avg_correlation,
                    created_at = NOW()
            """, target_date, matrix, symbols_count, avg_correlation)
    
    async def get_correlation(
        self,
        symbol1: str,
        symbol2: str,
        target_date: Optional[date] = None
    ) -> Optional[float]:
        """
        Recupera la correlazione tra due simboli.
        
        Args:
            symbol1: Primo simbolo
            symbol2: Secondo simbolo
            target_date: Data specifica (default: ultima disponibile)
        
        Returns:
            Valore di correlazione o None
        """
        async with self.pool.acquire() as conn:
            if target_date:
                row = await conn.fetchrow(
                    "SELECT matrix FROM correlation_matrix WHERE date = $1",
                    target_date
                )
            else:
                row = await conn.fetchrow("""
                    SELECT matrix FROM correlation_matrix
                    ORDER BY date DESC
                    LIMIT 1
                """)
            
            if row and row["matrix"]:
                matrix = row["matrix"]
                # Prova entrambe le direzioni
                if symbol1 in matrix and symbol2 in matrix[symbol1]:
                    return float(matrix[symbol1][symbol2])
                elif symbol2 in matrix and symbol1 in matrix[symbol2]:
                    return float(matrix[symbol2][symbol1])
            
            return None
    
    # =========================================================================
    # SERVICE HEALTH
    # =========================================================================
    
    async def update_service_health(
        self,
        service_name: str,
        status: str,
        metadata: Optional[dict] = None,
        increment_messages: int = 0,
        increment_errors: int = 0
    ) -> None:
        """
        Aggiorna lo stato di salute di un servizio.
        
        Args:
            service_name: Nome del servizio
            status: healthy, degraded, unhealthy, starting
            metadata: Dict con metriche aggiuntive
            increment_messages: Incremento messaggi processati
            increment_errors: Incremento errori
        """
        import json
        async with self.pool.acquire() as conn:
            # Serialize metadata dict to JSON string for asyncpg
            metadata_json = json.dumps(metadata) if metadata else None
            await conn.execute("""
                INSERT INTO service_health
                (service_name, status, last_heartbeat, messages_processed, errors_count, metadata)
                VALUES ($1, $2, NOW(), $3, $4, $5::jsonb)
                ON CONFLICT (service_name) DO UPDATE SET
                    status = EXCLUDED.status,
                    last_heartbeat = NOW(),
                    messages_processed = service_health.messages_processed + EXCLUDED.messages_processed,
                    errors_count = service_health.errors_count + EXCLUDED.errors_count,
                    metadata = COALESCE(EXCLUDED.metadata, service_health.metadata)
            """, service_name, status, increment_messages, increment_errors, metadata_json)
    
    async def get_service_health(self, service_name: Optional[str] = None) -> list[dict]:
        """
        Recupera lo stato di salute dei servizi.
        
        Args:
            service_name: Nome servizio specifico (opzionale, altrimenti tutti)
        
        Returns:
            Lista di dict con stato servizi
        """
        async with self.pool.acquire() as conn:
            if service_name:
                row = await conn.fetchrow(
                    "SELECT * FROM service_health WHERE service_name = $1",
                    service_name
                )
                return [dict(row)] if row else []
            else:
                rows = await conn.fetch(
                    "SELECT * FROM service_health ORDER BY service_name"
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


# =============================================================================
# ESEMPIO USO
# =============================================================================

async def _example():
    """Esempio di utilizzo."""
    db = await get_database()
    
    try:
        # Health check
        healthy = await db.health_check()
        print(f"Database healthy: {healthy}")
        
        # Aggiorna account
        await db.update_account(
            equity=Decimal("10000.00"),
            available_balance=Decimal("8000.00"),
            margin_used=Decimal("2000.00"),
            unrealized_pnl=Decimal("150.50")
        )
        
        # Leggi account
        account = await db.get_account()
        print(f"Account: {account}")
        
        # Statistiche
        stats = await db.get_stats()
        print(f"Stats: {stats}")
        
    finally:
        await db.disconnect()


if __name__ == "__main__":
    asyncio.run(_example())
