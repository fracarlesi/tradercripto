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

                -- Create indexes
                CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy_id);
                CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
                CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time);
                CREATE INDEX IF NOT EXISTS idx_account_snapshots_timestamp ON account_snapshots(timestamp);
                CREATE INDEX IF NOT EXISTS idx_strategy_metrics_strategy ON strategy_metrics(strategy_id);
                CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alerts(timestamp);
            """)
        logger.info("Database schema initialized")

    # -------------------------------------------------------------------------
    # Trades
    # -------------------------------------------------------------------------
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
