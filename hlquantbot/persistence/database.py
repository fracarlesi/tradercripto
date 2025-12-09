"""Database operations for HLQuantBot."""

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Any

import asyncpg

from ..core.models import (
    AccountState,
    Position,
    ClosedTrade,
    StrategyMetrics,
    RegimeAnalysis,
)
from ..core.enums import StrategyId, Side, ExitReason, MarketRegime
from ..config.settings import Settings


logger = logging.getLogger(__name__)


class Database:
    """
    Async database operations using asyncpg.

    Handles all persistence for trades, positions, metrics, etc.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self):
        """Create connection pool."""
        self._pool = await asyncpg.create_pool(
            host=self.settings.database.host,
            port=self.settings.database.port,
            database=self.settings.database.name,
            user=self.settings.database.user,
            password=self.settings.database.password,
            min_size=2,
            max_size=10,
        )
        logger.info("Database connected")

        # Initialize schema
        await self._init_schema()

    async def close(self):
        """Close connection pool."""
        if self._pool:
            await self._pool.close()
            logger.info("Database disconnected")

    async def _init_schema(self):
        """Create tables if they don't exist."""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                -- Trades table
                CREATE TABLE IF NOT EXISTS trades (
                    id SERIAL PRIMARY KEY,
                    trade_id VARCHAR(50) UNIQUE NOT NULL,
                    strategy_id VARCHAR(50) NOT NULL,
                    symbol VARCHAR(20) NOT NULL,
                    side VARCHAR(10) NOT NULL,
                    size DECIMAL(20, 8) NOT NULL,
                    entry_price DECIMAL(20, 8) NOT NULL,
                    exit_price DECIMAL(20, 8),
                    pnl DECIMAL(20, 8),
                    pnl_pct DECIMAL(10, 6),
                    fees DECIMAL(20, 8) DEFAULT 0,
                    funding_paid DECIMAL(20, 8) DEFAULT 0,
                    entry_time TIMESTAMP WITH TIME ZONE NOT NULL,
                    exit_time TIMESTAMP WITH TIME ZONE,
                    duration_seconds INTEGER,
                    exit_reason VARCHAR(50),
                    metadata JSONB,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );

                -- Positions snapshot table
                CREATE TABLE IF NOT EXISTS positions (
                    id SERIAL PRIMARY KEY,
                    snapshot_time TIMESTAMP WITH TIME ZONE NOT NULL,
                    symbol VARCHAR(20) NOT NULL,
                    side VARCHAR(10) NOT NULL,
                    size DECIMAL(20, 8) NOT NULL,
                    entry_price DECIMAL(20, 8) NOT NULL,
                    current_price DECIMAL(20, 8) NOT NULL,
                    unrealized_pnl DECIMAL(20, 8),
                    leverage DECIMAL(5, 2),
                    liquidation_price DECIMAL(20, 8),
                    strategy_id VARCHAR(50),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );

                -- Account snapshots table
                CREATE TABLE IF NOT EXISTS account_snapshots (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
                    equity DECIMAL(20, 8) NOT NULL,
                    available_balance DECIMAL(20, 8) NOT NULL,
                    total_margin_used DECIMAL(20, 8),
                    total_position_value DECIMAL(20, 8),
                    unrealized_pnl DECIMAL(20, 8),
                    daily_pnl DECIMAL(20, 8),
                    daily_pnl_pct DECIMAL(10, 6),
                    current_leverage DECIMAL(5, 2),
                    position_count INTEGER,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );

                -- Strategy metrics table
                CREATE TABLE IF NOT EXISTS strategy_metrics (
                    id SERIAL PRIMARY KEY,
                    strategy_id VARCHAR(50) NOT NULL,
                    period_type VARCHAR(20) NOT NULL,
                    period_start DATE NOT NULL,
                    period_end DATE NOT NULL,
                    total_trades INTEGER DEFAULT 0,
                    winning_trades INTEGER DEFAULT 0,
                    losing_trades INTEGER DEFAULT 0,
                    win_rate DECIMAL(5, 4),
                    total_pnl DECIMAL(20, 8),
                    gross_profit DECIMAL(20, 8),
                    gross_loss DECIMAL(20, 8),
                    profit_factor DECIMAL(10, 4),
                    avg_win DECIMAL(20, 8),
                    avg_loss DECIMAL(20, 8),
                    max_drawdown DECIMAL(10, 6),
                    sharpe_ratio DECIMAL(10, 4),
                    avg_trade_duration_minutes DECIMAL(10, 2),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    UNIQUE(strategy_id, period_type, period_start)
                );

                -- Regime analysis history
                CREATE TABLE IF NOT EXISTS regime_history (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
                    regime VARCHAR(30) NOT NULL,
                    confidence DECIMAL(5, 4),
                    risk_adjustment DECIMAL(5, 4),
                    asset_regimes JSONB,
                    analysis TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );

                -- Alerts/events log
                CREATE TABLE IF NOT EXISTS alerts (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
                    severity VARCHAR(20) NOT NULL,
                    category VARCHAR(50),
                    message TEXT NOT NULL,
                    data JSONB,
                    acknowledged BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );

                -- Config history for audit
                CREATE TABLE IF NOT EXISTS config_history (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
                    config_type VARCHAR(50) NOT NULL,
                    config_data JSONB NOT NULL,
                    reason VARCHAR(255),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );

                -- Strategy signals table (NUOVO - da specifica consigli.md)
                -- Traccia ogni segnale generato con contesto completo per analisi P&L
                CREATE TABLE IF NOT EXISTS strategy_signals (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
                    strategy_id VARCHAR(50) NOT NULL,
                    symbol VARCHAR(20) NOT NULL,
                    side VARCHAR(10) NOT NULL,

                    -- Contesto regime e aggressività (da specifica)
                    regime VARCHAR(30),
                    aggression_level VARCHAR(20),

                    -- Parametri effettivi applicati
                    leverage_effective DECIMAL(5, 2),
                    tp_pct DECIMAL(10, 6),
                    sl_pct DECIMAL(10, 6),
                    notional_usd DECIMAL(20, 8),
                    risk_per_trade_pct DECIMAL(10, 6),

                    -- Stato del segnale
                    accepted BOOLEAN NOT NULL,
                    reason_for_reject VARCHAR(255),  -- Da risk engine se rigettato

                    -- Metriche per analisi
                    confidence DECIMAL(5, 4),
                    signal_reason TEXT,

                    -- Tracciamento esecuzione
                    order_id VARCHAR(100),
                    fill_price DECIMAL(20, 8),
                    fill_time TIMESTAMP WITH TIME ZONE,

                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );

                -- Aggression history table (NUOVO - da specifica)
                -- Traccia i cambi di livello aggressività nel tempo
                CREATE TABLE IF NOT EXISTS aggression_history (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
                    level VARCHAR(20) NOT NULL,
                    previous_level VARCHAR(20),
                    trigger_reason VARCHAR(255),
                    win_rate_100 DECIMAL(5, 4),
                    daily_pnl_pct DECIMAL(10, 6),
                    regime VARCHAR(30),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );

                -- Create indexes
                CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy_id);
                CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
                CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time);
                CREATE INDEX IF NOT EXISTS idx_account_snapshots_timestamp ON account_snapshots(timestamp);
                CREATE INDEX IF NOT EXISTS idx_strategy_metrics_strategy ON strategy_metrics(strategy_id);
                CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alerts(timestamp);

                -- Nuovi indici per strategy_signals e aggression_history (da specifica)
                CREATE INDEX IF NOT EXISTS idx_strategy_signals_timestamp ON strategy_signals(timestamp);
                CREATE INDEX IF NOT EXISTS idx_strategy_signals_strategy ON strategy_signals(strategy_id);
                CREATE INDEX IF NOT EXISTS idx_strategy_signals_regime ON strategy_signals(regime);
                CREATE INDEX IF NOT EXISTS idx_strategy_signals_aggression ON strategy_signals(aggression_level);
                CREATE INDEX IF NOT EXISTS idx_aggression_history_timestamp ON aggression_history(timestamp);
            """)
        logger.info("Database schema initialized")

    # -------------------------------------------------------------------------
    # Trades
    # -------------------------------------------------------------------------
    async def save_open_trade(self, order) -> str:
        """
        Save an open trade (position entry) to database.

        This is called when a position is OPENED, before it's closed.
        The trade is saved with exit_time=NULL to indicate it's still open.
        When the position is closed, save_trade() will update it with exit info.

        Returns the trade_id for tracking.
        """
        trade_id = f"{order.symbol}_{order.executed_at.timestamp():.0f}"

        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO trades (
                    trade_id, strategy_id, symbol, side, size,
                    entry_price, exit_price, pnl, pnl_pct, fees,
                    funding_paid, entry_time, exit_time, duration_seconds,
                    exit_reason, metadata
                ) VALUES ($1, $2, $3, $4, $5, $6, NULL, NULL, NULL, $7, 0, $8, NULL, NULL, NULL, $9)
                ON CONFLICT (trade_id) DO NOTHING
            """,
                trade_id,
                order.strategy_id.value,
                order.symbol,
                order.side.value,
                float(order.filled_size or order.size),
                float(order.filled_price or order.price or 0),
                float(order.fees or 0),
                order.executed_at or datetime.now(timezone.utc),
                json.dumps({"leverage": float(order.leverage_used)}) if order.leverage_used else None,
            )

        logger.info(f"Open trade saved: {trade_id} ({order.symbol} {order.side.value})")
        return trade_id

    async def get_open_trades(self) -> List[Dict]:
        """Get all open trades (where exit_time is NULL)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM trades
                WHERE exit_time IS NULL
                ORDER BY entry_time DESC
            """)
        return [dict(row) for row in rows]

    async def close_orphan_trade(
        self,
        symbol: str,
        exit_price: Decimal,
        pnl: Decimal,
        pnl_pct: Decimal,
        exit_reason: str = "external_close",
    ):
        """
        Close an orphan trade (position closed externally).

        This is called when we detect a position was closed outside the bot
        (e.g., manually on Hyperliquid, or bot crashed and user closed).
        """
        async with self._pool.acquire() as conn:
            # Find the open trade for this symbol
            row = await conn.fetchrow("""
                SELECT trade_id, entry_time FROM trades
                WHERE symbol = $1 AND exit_time IS NULL
                ORDER BY entry_time DESC LIMIT 1
            """, symbol)

            if not row:
                logger.warning(f"No open trade found for {symbol} to close")
                return

            trade_id = row["trade_id"]
            entry_time = row["entry_time"]
            exit_time = datetime.now(timezone.utc)
            duration = int((exit_time - entry_time).total_seconds())

            await conn.execute("""
                UPDATE trades SET
                    exit_price = $2,
                    pnl = $3,
                    pnl_pct = $4,
                    exit_time = $5,
                    duration_seconds = $6,
                    exit_reason = $7
                WHERE trade_id = $1
            """,
                trade_id,
                float(exit_price),
                float(pnl),
                float(pnl_pct),
                exit_time,
                duration,
                exit_reason,
            )

            logger.info(
                f"Orphan trade closed: {trade_id} | "
                f"P&L: ${pnl:.2f} ({pnl_pct:.2%}) | Reason: {exit_reason}"
            )

    async def save_trade(self, trade: ClosedTrade, metadata: Optional[Dict] = None):
        """Save a closed trade."""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO trades (
                    trade_id, strategy_id, symbol, side, size,
                    entry_price, exit_price, pnl, pnl_pct, fees,
                    funding_paid, entry_time, exit_time, duration_seconds,
                    exit_reason, metadata
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
                ON CONFLICT (trade_id) DO UPDATE SET
                    exit_price = EXCLUDED.exit_price,
                    pnl = EXCLUDED.pnl,
                    pnl_pct = EXCLUDED.pnl_pct,
                    exit_time = EXCLUDED.exit_time,
                    duration_seconds = EXCLUDED.duration_seconds,
                    exit_reason = EXCLUDED.exit_reason
            """,
                trade.trade_id,
                trade.strategy_id.value,
                trade.symbol,
                trade.side.value,
                float(trade.size),
                float(trade.entry_price),
                float(trade.exit_price),
                float(trade.pnl),
                float(trade.pnl_pct),
                float(trade.fees),
                float(trade.funding_paid),
                trade.entry_time,
                trade.exit_time,
                trade.duration_seconds,
                trade.exit_reason.value,
                json.dumps(metadata) if metadata else None,
            )

    async def get_trades(
        self,
        strategy_id: Optional[StrategyId] = None,
        symbol: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
    ) -> List[ClosedTrade]:
        """Get trades with filters."""
        query = "SELECT * FROM trades WHERE 1=1"
        params = []
        param_num = 1

        if strategy_id:
            query += f" AND strategy_id = ${param_num}"
            params.append(strategy_id.value)
            param_num += 1

        if symbol:
            query += f" AND symbol = ${param_num}"
            params.append(symbol)
            param_num += 1

        if start_time:
            query += f" AND entry_time >= ${param_num}"
            params.append(start_time)
            param_num += 1

        if end_time:
            query += f" AND entry_time <= ${param_num}"
            params.append(end_time)
            param_num += 1

        query += f" ORDER BY entry_time DESC LIMIT ${param_num}"
        params.append(limit)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        trades = []
        for row in rows:
            trades.append(ClosedTrade(
                trade_id=row["trade_id"],
                symbol=row["symbol"],
                side=Side(row["side"]),
                size=Decimal(str(row["size"])),
                entry_price=Decimal(str(row["entry_price"])),
                exit_price=Decimal(str(row["exit_price"])) if row["exit_price"] else Decimal(0),
                pnl=Decimal(str(row["pnl"])) if row["pnl"] else Decimal(0),
                pnl_pct=Decimal(str(row["pnl_pct"])) if row["pnl_pct"] else Decimal(0),
                fees=Decimal(str(row["fees"])) if row["fees"] else Decimal(0),
                funding_paid=Decimal(str(row["funding_paid"])) if row["funding_paid"] else Decimal(0),
                entry_time=row["entry_time"],
                exit_time=row["exit_time"],
                duration_seconds=row["duration_seconds"] or 0,
                strategy_id=StrategyId(row["strategy_id"]),
                exit_reason=ExitReason(row["exit_reason"]) if row["exit_reason"] else ExitReason.SIGNAL_EXIT,
            ))

        return trades

    # -------------------------------------------------------------------------
    # Account Snapshots
    # -------------------------------------------------------------------------
    async def save_account_snapshot(self, account: AccountState):
        """Save account snapshot."""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO account_snapshots (
                    timestamp, equity, available_balance, total_margin_used,
                    total_position_value, unrealized_pnl, daily_pnl, daily_pnl_pct,
                    current_leverage, position_count
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
                account.timestamp,
                float(account.equity),
                float(account.available_balance),
                float(account.total_margin_used),
                float(account.total_position_value),
                float(account.total_unrealized_pnl),
                float(account.daily_pnl),
                float(account.daily_pnl_pct),
                float(account.current_leverage),
                account.position_count,
            )

        # Save positions snapshot
        if account.positions:
            await self.save_positions(account.positions, account.timestamp)

    async def save_positions(self, positions: List[Position], snapshot_time: datetime):
        """Save positions snapshot."""
        async with self._pool.acquire() as conn:
            for pos in positions:
                await conn.execute("""
                    INSERT INTO positions (
                        snapshot_time, symbol, side, size, entry_price,
                        current_price, unrealized_pnl, leverage, liquidation_price,
                        strategy_id
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                    snapshot_time,
                    pos.symbol,
                    pos.side.value,
                    float(pos.size),
                    float(pos.entry_price),
                    float(pos.current_price),
                    float(pos.unrealized_pnl),
                    float(pos.leverage),
                    float(pos.liquidation_price) if pos.liquidation_price else None,
                    pos.strategy_id.value if pos.strategy_id else None,
                )

    async def get_account_history(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[Dict]:
        """Get account history."""
        query = "SELECT * FROM account_snapshots WHERE 1=1"
        params = []
        param_num = 1

        if start_time:
            query += f" AND timestamp >= ${param_num}"
            params.append(start_time)
            param_num += 1

        if end_time:
            query += f" AND timestamp <= ${param_num}"
            params.append(end_time)
            param_num += 1

        query += f" ORDER BY timestamp DESC LIMIT ${param_num}"
        params.append(limit)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        return [dict(row) for row in rows]

    # -------------------------------------------------------------------------
    # Strategy Metrics
    # -------------------------------------------------------------------------
    async def save_strategy_metrics(self, metrics: StrategyMetrics, period_type: str = "daily"):
        """Save strategy metrics."""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO strategy_metrics (
                    strategy_id, period_type, period_start, period_end,
                    total_trades, winning_trades, losing_trades, win_rate,
                    total_pnl, gross_profit, gross_loss, profit_factor,
                    avg_win, avg_loss, max_drawdown, sharpe_ratio,
                    avg_trade_duration_minutes
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)
                ON CONFLICT (strategy_id, period_type, period_start) DO UPDATE SET
                    total_trades = EXCLUDED.total_trades,
                    winning_trades = EXCLUDED.winning_trades,
                    losing_trades = EXCLUDED.losing_trades,
                    win_rate = EXCLUDED.win_rate,
                    total_pnl = EXCLUDED.total_pnl,
                    gross_profit = EXCLUDED.gross_profit,
                    gross_loss = EXCLUDED.gross_loss,
                    profit_factor = EXCLUDED.profit_factor,
                    avg_win = EXCLUDED.avg_win,
                    avg_loss = EXCLUDED.avg_loss,
                    max_drawdown = EXCLUDED.max_drawdown,
                    sharpe_ratio = EXCLUDED.sharpe_ratio
            """,
                metrics.strategy_id.value,
                period_type,
                metrics.period_start.date(),
                metrics.period_end.date(),
                metrics.total_trades,
                metrics.winning_trades,
                metrics.losing_trades,
                float(metrics.win_rate),
                float(metrics.total_pnl),
                float(metrics.gross_profit),
                float(metrics.gross_loss),
                float(metrics.profit_factor),
                float(metrics.avg_win),
                float(metrics.avg_loss),
                float(metrics.max_drawdown),
                float(metrics.sharpe_ratio) if metrics.sharpe_ratio else None,
                float(metrics.avg_trade_duration_minutes),
            )

    async def get_strategy_metrics(
        self,
        strategy_id: StrategyId,
        period_type: str = "daily",
        limit: int = 30,
    ) -> List[Dict]:
        """Get strategy metrics history."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM strategy_metrics
                WHERE strategy_id = $1 AND period_type = $2
                ORDER BY period_start DESC
                LIMIT $3
            """, strategy_id.value, period_type, limit)

        return [dict(row) for row in rows]

    # -------------------------------------------------------------------------
    # Regime History
    # -------------------------------------------------------------------------
    async def save_regime_analysis(self, analysis: RegimeAnalysis):
        """Save regime analysis."""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO regime_history (
                    timestamp, regime, confidence, risk_adjustment,
                    asset_regimes, analysis
                ) VALUES ($1, $2, $3, $4, $5, $6)
            """,
                analysis.timestamp,
                analysis.regime.value,
                float(analysis.confidence),
                float(analysis.risk_adjustment),
                json.dumps({k: v.value for k, v in analysis.asset_regimes.items()}),
                analysis.analysis,
            )

    # -------------------------------------------------------------------------
    # Alerts
    # -------------------------------------------------------------------------
    async def save_alert(
        self,
        severity: str,
        message: str,
        category: Optional[str] = None,
        data: Optional[Dict] = None,
    ):
        """Save an alert."""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO alerts (timestamp, severity, category, message, data)
                VALUES ($1, $2, $3, $4, $5)
            """,
                datetime.now(timezone.utc),
                severity,
                category,
                message,
                json.dumps(data) if data else None,
            )

    async def get_alerts(
        self,
        severity: Optional[str] = None,
        unacknowledged_only: bool = False,
        limit: int = 100,
    ) -> List[Dict]:
        """Get alerts."""
        query = "SELECT * FROM alerts WHERE 1=1"
        params = []
        param_num = 1

        if severity:
            query += f" AND severity = ${param_num}"
            params.append(severity)
            param_num += 1

        if unacknowledged_only:
            query += " AND acknowledged = FALSE"

        query += f" ORDER BY timestamp DESC LIMIT ${param_num}"
        params.append(limit)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        return [dict(row) for row in rows]

    # -------------------------------------------------------------------------
    # Config History
    # -------------------------------------------------------------------------
    async def save_config_change(
        self,
        config_type: str,
        config_data: Dict,
        reason: Optional[str] = None,
    ):
        """Save config change for audit."""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO config_history (timestamp, config_type, config_data, reason)
                VALUES ($1, $2, $3, $4)
            """,
                datetime.now(timezone.utc),
                config_type,
                json.dumps(config_data, default=str),
                reason,
            )

    # -------------------------------------------------------------------------
    # Utility
    # -------------------------------------------------------------------------
    async def get_daily_pnl(self, date: Optional[datetime] = None) -> Decimal:
        """Get total P&L for a day."""
        if date is None:
            date = datetime.now(timezone.utc)

        start = date.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT COALESCE(SUM(pnl), 0) as total_pnl
                FROM trades
                WHERE exit_time >= $1 AND exit_time < $2
            """, start, end)

        return Decimal(str(row["total_pnl"]))

    async def calculate_strategy_metrics(
        self,
        strategy_id: StrategyId,
        start_time: datetime,
        end_time: datetime,
    ) -> StrategyMetrics:
        """Calculate metrics for a strategy over a period."""
        trades = await self.get_trades(
            strategy_id=strategy_id,
            start_time=start_time,
            end_time=end_time,
            limit=10000,
        )

        metrics = StrategyMetrics(
            strategy_id=strategy_id,
            period_start=start_time,
            period_end=end_time,
        )

        if not trades:
            return metrics

        metrics.total_trades = len(trades)
        metrics.winning_trades = sum(1 for t in trades if t.pnl > 0)
        metrics.losing_trades = sum(1 for t in trades if t.pnl <= 0)

        metrics.total_pnl = sum(t.pnl for t in trades)
        metrics.gross_profit = sum(t.pnl for t in trades if t.pnl > 0)
        metrics.gross_loss = sum(t.pnl for t in trades if t.pnl <= 0)

        total_duration = sum(t.duration_seconds for t in trades)
        metrics.avg_trade_duration_minutes = Decimal(total_duration) / Decimal(60) / Decimal(len(trades))

        metrics.calculate_ratios()

        return metrics

    # -------------------------------------------------------------------------
    # Strategy Signals (NUOVO - da specifica consigli.md)
    # Traccia ogni segnale per analisi P&L per regime/aggression
    # -------------------------------------------------------------------------
    async def save_strategy_signal(
        self,
        strategy_id: str,
        symbol: str,
        side: str,
        accepted: bool,
        regime: Optional[str] = None,
        aggression_level: Optional[str] = None,
        leverage_effective: Optional[float] = None,
        tp_pct: Optional[float] = None,
        sl_pct: Optional[float] = None,
        notional_usd: Optional[float] = None,
        risk_per_trade_pct: Optional[float] = None,
        reason_for_reject: Optional[str] = None,
        confidence: Optional[float] = None,
        signal_reason: Optional[str] = None,
        order_id: Optional[str] = None,
    ):
        """
        Save a strategy signal with full context for P&L analysis.

        Questo permette di analizzare:
        - P&L per regime
        - P&L per aggressiveness
        - P&L per strategia
        - Distribuzione SL/TP
        - Rejection rate dal risk engine
        """
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO strategy_signals (
                    timestamp, strategy_id, symbol, side,
                    regime, aggression_level, leverage_effective,
                    tp_pct, sl_pct, notional_usd, risk_per_trade_pct,
                    accepted, reason_for_reject, confidence, signal_reason, order_id
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
            """,
                datetime.now(timezone.utc),
                strategy_id,
                symbol,
                side,
                regime,
                aggression_level,
                leverage_effective,
                tp_pct,
                sl_pct,
                notional_usd,
                risk_per_trade_pct,
                accepted,
                reason_for_reject,
                confidence,
                signal_reason,
                order_id,
            )

    async def update_signal_fill(
        self,
        order_id: str,
        fill_price: float,
        fill_time: Optional[datetime] = None,
    ):
        """Update signal with fill information."""
        if fill_time is None:
            fill_time = datetime.now(timezone.utc)

        async with self._pool.acquire() as conn:
            await conn.execute("""
                UPDATE strategy_signals
                SET fill_price = $2, fill_time = $3
                WHERE order_id = $1
            """, order_id, fill_price, fill_time)

    async def get_signals_by_regime(
        self,
        regime: str,
        start_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[Dict]:
        """Get signals filtered by regime for P&L analysis."""
        query = "SELECT * FROM strategy_signals WHERE regime = $1"
        params = [regime]
        param_num = 2

        if start_time:
            query += f" AND timestamp >= ${param_num}"
            params.append(start_time)
            param_num += 1

        query += f" ORDER BY timestamp DESC LIMIT ${param_num}"
        params.append(limit)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        return [dict(row) for row in rows]

    async def get_signals_by_aggression(
        self,
        aggression_level: str,
        start_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[Dict]:
        """Get signals filtered by aggression level for P&L analysis."""
        query = "SELECT * FROM strategy_signals WHERE aggression_level = $1"
        params = [aggression_level]
        param_num = 2

        if start_time:
            query += f" AND timestamp >= ${param_num}"
            params.append(start_time)
            param_num += 1

        query += f" ORDER BY timestamp DESC LIMIT ${param_num}"
        params.append(limit)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        return [dict(row) for row in rows]

    # -------------------------------------------------------------------------
    # Aggression History (NUOVO - da specifica consigli.md)
    # Traccia i cambi di livello aggressività
    # -------------------------------------------------------------------------
    async def save_aggression_change(
        self,
        level: str,
        previous_level: Optional[str] = None,
        trigger_reason: Optional[str] = None,
        win_rate_100: Optional[float] = None,
        daily_pnl_pct: Optional[float] = None,
        regime: Optional[str] = None,
    ):
        """Save aggression level change for analysis."""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO aggression_history (
                    timestamp, level, previous_level, trigger_reason,
                    win_rate_100, daily_pnl_pct, regime
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
                datetime.now(timezone.utc),
                level,
                previous_level,
                trigger_reason,
                win_rate_100,
                daily_pnl_pct,
                regime,
            )

    async def get_aggression_history(
        self,
        start_time: Optional[datetime] = None,
        limit: int = 100,
    ) -> List[Dict]:
        """Get aggression level history."""
        query = "SELECT * FROM aggression_history WHERE 1=1"
        params = []
        param_num = 1

        if start_time:
            query += f" AND timestamp >= ${param_num}"
            params.append(start_time)
            param_num += 1

        query += f" ORDER BY timestamp DESC LIMIT ${param_num}"
        params.append(limit)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        return [dict(row) for row in rows]

    async def get_win_rate_last_n_trades(self, n: int = 100) -> Optional[float]:
        """
        Calculate win rate for last N trades.

        Usato dall'AggressionController per decidere se alzare il livello
        (win rate > 58% -> +1 livello).
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT
                    COUNT(*) FILTER (WHERE pnl > 0) as wins,
                    COUNT(*) as total
                FROM (
                    SELECT pnl FROM trades
                    WHERE exit_time IS NOT NULL
                    ORDER BY exit_time DESC
                    LIMIT $1
                ) t
            """, n)

        if row and row["total"] > 0:
            return float(row["wins"]) / float(row["total"])
        return None

    # -------------------------------------------------------------------------
    # Symbol P&L Tracking (per dynamic blacklist)
    # -------------------------------------------------------------------------
    async def get_symbol_performance(
        self,
        symbol: str,
        lookback_hours: int = 24,
    ) -> Dict[str, Any]:
        """
        Get P&L metrics for a specific symbol.

        Used by SymbolBlacklist to identify underperforming symbols.

        Returns:
            Dict with: symbol, total_trades, winning_trades, win_rate, total_pnl, avg_pnl
        """
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT
                    COUNT(*) as total_trades,
                    COUNT(*) FILTER (WHERE pnl > 0) as winning_trades,
                    COUNT(*) FILTER (WHERE pnl <= 0) as losing_trades,
                    COALESCE(SUM(pnl), 0) as total_pnl,
                    COALESCE(AVG(pnl), 0) as avg_pnl,
                    COALESCE(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0) as gross_profit,
                    COALESCE(SUM(CASE WHEN pnl <= 0 THEN pnl ELSE 0 END), 0) as gross_loss
                FROM trades
                WHERE symbol = $1 AND exit_time >= $2
            """, symbol, cutoff_time)

        total_trades = row["total_trades"] or 0
        winning_trades = row["winning_trades"] or 0

        return {
            "symbol": symbol,
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "losing_trades": row["losing_trades"] or 0,
            "win_rate": winning_trades / total_trades if total_trades > 0 else 0.5,
            "total_pnl": Decimal(str(row["total_pnl"])),
            "avg_pnl": Decimal(str(row["avg_pnl"])),
            "gross_profit": Decimal(str(row["gross_profit"])),
            "gross_loss": Decimal(str(row["gross_loss"])),
            "lookback_hours": lookback_hours,
        }

    async def get_all_symbols_performance(
        self,
        lookback_hours: int = 24,
    ) -> List[Dict[str, Any]]:
        """
        Get P&L metrics for all traded symbols.

        Useful for dashboard and ranking symbols by profitability.

        Returns:
            List of dicts with symbol performance metrics, sorted by total_pnl desc
        """
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT
                    symbol,
                    COUNT(*) as total_trades,
                    COUNT(*) FILTER (WHERE pnl > 0) as winning_trades,
                    COUNT(*) FILTER (WHERE pnl <= 0) as losing_trades,
                    COALESCE(SUM(pnl), 0) as total_pnl,
                    COALESCE(AVG(pnl), 0) as avg_pnl,
                    COALESCE(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0) as gross_profit,
                    COALESCE(SUM(CASE WHEN pnl <= 0 THEN pnl ELSE 0 END), 0) as gross_loss
                FROM trades
                WHERE exit_time >= $1
                GROUP BY symbol
                ORDER BY total_pnl DESC
            """, cutoff_time)

        results = []
        for row in rows:
            total_trades = row["total_trades"] or 0
            winning_trades = row["winning_trades"] or 0

            results.append({
                "symbol": row["symbol"],
                "total_trades": total_trades,
                "winning_trades": winning_trades,
                "losing_trades": row["losing_trades"] or 0,
                "win_rate": winning_trades / total_trades if total_trades > 0 else 0.5,
                "total_pnl": Decimal(str(row["total_pnl"])),
                "avg_pnl": Decimal(str(row["avg_pnl"])),
                "gross_profit": Decimal(str(row["gross_profit"])),
                "gross_loss": Decimal(str(row["gross_loss"])),
            })

        return results

    async def get_strategy_symbol_performance(
        self,
        strategy_id: StrategyId,
        lookback_hours: int = 24,
    ) -> List[Dict[str, Any]]:
        """
        Get P&L metrics per symbol for a specific strategy.

        Useful for identifying which symbols work best with which strategy.
        """
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT
                    symbol,
                    COUNT(*) as total_trades,
                    COUNT(*) FILTER (WHERE pnl > 0) as winning_trades,
                    COALESCE(SUM(pnl), 0) as total_pnl,
                    COALESCE(AVG(pnl), 0) as avg_pnl
                FROM trades
                WHERE strategy_id = $1 AND exit_time >= $2
                GROUP BY symbol
                ORDER BY total_pnl DESC
            """, strategy_id.value, cutoff_time)

        results = []
        for row in rows:
            total_trades = row["total_trades"] or 0
            winning_trades = row["winning_trades"] or 0

            results.append({
                "symbol": row["symbol"],
                "strategy_id": strategy_id.value,
                "total_trades": total_trades,
                "winning_trades": winning_trades,
                "win_rate": winning_trades / total_trades if total_trades > 0 else 0.5,
                "total_pnl": Decimal(str(row["total_pnl"])),
                "avg_pnl": Decimal(str(row["avg_pnl"])),
            })

        return results

    # -------------------------------------------------------------------------
    # Strategy Signals (Decision Tracking)
    # -------------------------------------------------------------------------
    async def save_signal(
        self,
        strategy_id: str,
        symbol: str,
        side: str,
        accepted: bool,
        signal_reason: str,
        regime: Optional[str] = None,
        aggression_level: Optional[str] = None,
        leverage_effective: Optional[float] = None,
        tp_pct: Optional[float] = None,
        sl_pct: Optional[float] = None,
        notional_usd: Optional[float] = None,
        risk_per_trade_pct: Optional[float] = None,
        confidence: Optional[float] = None,
        reason_for_reject: Optional[str] = None,
        order_id: Optional[str] = None,
    ):
        """Save a strategy signal with full context for decision tracking.

        This enables debugging and analysis of why trades were taken or rejected.
        """
        try:
            async with self._pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO strategy_signals (
                        timestamp, strategy_id, symbol, side,
                        regime, aggression_level,
                        leverage_effective, tp_pct, sl_pct,
                        notional_usd, risk_per_trade_pct,
                        accepted, reason_for_reject,
                        confidence, signal_reason, order_id
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
                """,
                    datetime.now(timezone.utc),
                    strategy_id,
                    symbol,
                    side,
                    regime,
                    aggression_level,
                    leverage_effective,
                    tp_pct,
                    sl_pct,
                    notional_usd,
                    risk_per_trade_pct,
                    accepted,
                    reason_for_reject,
                    confidence,
                    signal_reason,
                    order_id,
                )
        except Exception as e:
            logger.warning(f"Failed to save signal: {e}")

    async def get_signals(
        self,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        accepted: Optional[bool] = None,
        limit: int = 100,
    ) -> List[Dict]:
        """Get strategy signals for analysis."""
        query = "SELECT * FROM strategy_signals WHERE 1=1"
        params = []
        param_num = 1

        if strategy_id:
            query += f" AND strategy_id = ${param_num}"
            params.append(strategy_id)
            param_num += 1

        if symbol:
            query += f" AND symbol = ${param_num}"
            params.append(symbol)
            param_num += 1

        if accepted is not None:
            query += f" AND accepted = ${param_num}"
            params.append(accepted)
            param_num += 1

        query += f" ORDER BY timestamp DESC LIMIT ${param_num}"
        params.append(limit)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        return [dict(row) for row in rows]
