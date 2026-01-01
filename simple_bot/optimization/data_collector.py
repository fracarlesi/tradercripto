"""
Hourly Metrics Collection
Collects performance data every hour for LLM analysis.
"""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Optional, List
import json
import logging

logger = logging.getLogger(__name__)


class HourlyMetricsCollector:
    """
    Collects trading metrics at hourly intervals.
    Stores in hourly_metrics table for LLM context.
    """

    def __init__(self, db, info_client=None):
        """
        Initialize collector.

        Args:
            db: Database connection pool
            info_client: Hyperliquid info client (optional, for market data)
        """
        self.db = db
        self.info = info_client
        self.current_hour_start: Optional[datetime] = None

    async def collect_hourly_metrics(self, parameter_version_id: int) -> Dict:
        """
        Collect metrics for the completed hour.

        Args:
            parameter_version_id: Current active parameter version

        Returns:
            Dict with collected metrics
        """
        now = datetime.utcnow()
        hour_start = now.replace(minute=0, second=0, microsecond=0)
        hour_end = hour_start + timedelta(hours=1)

        logger.info(f"Collecting metrics for hour: {hour_start}")

        async with self.db.acquire() as conn:
            # Get trades closed this hour
            trades = await conn.fetch("""
                SELECT
                    strategy,
                    net_pnl,
                    gross_pnl,
                    fees,
                    duration_seconds,
                    CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END as is_win
                FROM trades
                WHERE exit_time >= $1 AND exit_time < $2 AND is_closed = TRUE
            """, hour_start, hour_end)

            # Aggregate by strategy
            pnl_by_strategy: Dict[str, Decimal] = {}
            trades_by_strategy: Dict[str, int] = {}

            total_trades = len(trades)
            win_count = 0
            gross_pnl = Decimal("0")
            net_pnl = Decimal("0")
            fees = Decimal("0")
            durations: List[int] = []
            largest_win = Decimal("0")
            largest_loss = Decimal("0")

            pnl_values: List[Decimal] = []  # For Sharpe ratio calculation
            
            for t in trades:
                strategy = t["strategy"] or "unknown"
                pnl = t["net_pnl"] or Decimal("0")

                pnl_by_strategy[strategy] = pnl_by_strategy.get(strategy, Decimal("0")) + pnl
                trades_by_strategy[strategy] = trades_by_strategy.get(strategy, 0) + 1
                pnl_values.append(pnl)  # Collect for Sharpe calculation

                if t["is_win"]:
                    win_count += 1
                    largest_win = max(largest_win, pnl)
                else:
                    largest_loss = min(largest_loss, pnl)

                gross_pnl += t["gross_pnl"] or Decimal("0")
                net_pnl += pnl
                fees += t["fees"] or Decimal("0")

                if t["duration_seconds"]:
                    durations.append(t["duration_seconds"])

            avg_duration = sum(durations) // len(durations) if durations else 0
            
            # Calculate advanced metrics
            sharpe_ratio = self._calculate_sharpe_ratio(pnl_values)
            profit_factor = self._calculate_profit_factor(trades)

            # Get BTC price for market context
            btc_start, btc_end, volatility = await self._get_market_context(hour_start, hour_end)

            # Calculate max drawdown this hour
            max_dd = await self._calculate_hourly_drawdown(conn, hour_start, hour_end)

            metrics = {
                "hour_start": hour_start,
                "parameter_version": parameter_version_id,
                "trades_count": total_trades,
                "win_count": win_count,
                "loss_count": total_trades - win_count,
                "gross_pnl": gross_pnl,
                "net_pnl": net_pnl,
                "fees": fees,
                "pnl_by_strategy": json.dumps({k: str(v) for k, v in pnl_by_strategy.items()}),
                "trades_by_strategy": json.dumps(trades_by_strategy),
                "max_drawdown_pct": max_dd,
                "avg_trade_duration": avg_duration,
                "largest_win": largest_win,
                "largest_loss": largest_loss,
                "btc_price_start": btc_start,
                "btc_price_end": btc_end,
                "volatility_index": volatility,
                "sharpe_ratio": sharpe_ratio,
                "profit_factor": profit_factor
            }

            # Insert into database
            await conn.execute("""
                INSERT INTO hourly_metrics (
                    hour_start, parameter_version, trades_count, win_count, loss_count,
                    gross_pnl, net_pnl, fees, pnl_by_strategy, trades_by_strategy,
                    max_drawdown_pct, avg_trade_duration, largest_win, largest_loss,
                    btc_price_start, btc_price_end, volatility_index,
                    sharpe_ratio, profit_factor
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19)
                ON CONFLICT (hour_start) DO UPDATE SET
                    parameter_version = EXCLUDED.parameter_version,
                    trades_count = EXCLUDED.trades_count,
                    win_count = EXCLUDED.win_count,
                    loss_count = EXCLUDED.loss_count,
                    gross_pnl = EXCLUDED.gross_pnl,
                    net_pnl = EXCLUDED.net_pnl,
                    fees = EXCLUDED.fees,
                    pnl_by_strategy = EXCLUDED.pnl_by_strategy,
                    trades_by_strategy = EXCLUDED.trades_by_strategy,
                    max_drawdown_pct = EXCLUDED.max_drawdown_pct,
                    avg_trade_duration = EXCLUDED.avg_trade_duration,
                    largest_win = EXCLUDED.largest_win,
                    largest_loss = EXCLUDED.largest_loss,
                    btc_price_start = EXCLUDED.btc_price_start,
                    btc_price_end = EXCLUDED.btc_price_end,
                    volatility_index = EXCLUDED.volatility_index,
                    sharpe_ratio = EXCLUDED.sharpe_ratio,
                    profit_factor = EXCLUDED.profit_factor
            """,
                metrics["hour_start"],
                metrics["parameter_version"],
                metrics["trades_count"],
                metrics["win_count"],
                metrics["loss_count"],
                metrics["gross_pnl"],
                metrics["net_pnl"],
                metrics["fees"],
                metrics["pnl_by_strategy"],
                metrics["trades_by_strategy"],
                metrics["max_drawdown_pct"],
                metrics["avg_trade_duration"],
                metrics["largest_win"],
                metrics["largest_loss"],
                metrics["btc_price_start"],
                metrics["btc_price_end"],
                metrics["volatility_index"],
                metrics["sharpe_ratio"],
                metrics["profit_factor"]
            )

            logger.info(
                f"Collected hourly metrics: {total_trades} trades, "
                f"${float(net_pnl):.2f} P&L, {win_count} wins"
            )

            return metrics

    async def _get_market_context(
        self,
        hour_start: datetime,
        hour_end: datetime
    ) -> tuple[Optional[float], Optional[float], Optional[float]]:
        """Get BTC price and volatility for market context."""
        if not self.info:
            return None, None, None

        try:
            # Get BTC candles for the hour
            btc_candles = self.info.candles_snapshot(
                "BTC", "1h",
                int(hour_start.timestamp() * 1000),
                int(hour_end.timestamp() * 1000)
            )

            if not btc_candles:
                return None, None, None

            btc_start = float(btc_candles[0]["o"])
            btc_end = float(btc_candles[-1]["c"])

            # Calculate simple volatility (std dev of returns)
            if len(btc_candles) > 1:
                returns = [
                    (float(c["c"]) - float(c["o"])) / float(c["o"])
                    for c in btc_candles if float(c["o"]) > 0
                ]
                volatility = (sum(r**2 for r in returns) / len(returns)) ** 0.5
            else:
                volatility = None

            return btc_start, btc_end, volatility

        except Exception as e:
            logger.warning(f"Failed to get market context: {e}")
            return None, None, None

    async def _calculate_hourly_drawdown(
        self,
        conn,
        hour_start: datetime,
        hour_end: datetime
    ) -> Optional[Decimal]:
        """
        Calculate max drawdown within the hour.
        Uses closed trades to estimate equity curve.
        """
        try:
            # Get starting equity
            row = await conn.fetchrow("""
                SELECT equity FROM live_account LIMIT 1
            """)

            if not row:
                return None

            # Simple approximation: max single trade loss as drawdown
            max_loss = await conn.fetchval("""
                SELECT MIN(net_pnl)
                FROM trades
                WHERE exit_time >= $1 AND exit_time < $2 AND is_closed = TRUE
            """, hour_start, hour_end)

            if max_loss and row['equity'] > 0:
                return abs(float(max_loss)) / float(row['equity']) * 100

            return None

        except Exception as e:
            logger.warning(f"Failed to calculate drawdown: {e}")
            return None

    def _calculate_sharpe_ratio(self, pnl_values: List[Decimal], risk_free_rate: float = 0.0) -> float:
        """
        Calculate Sharpe Ratio from PnL values.
        
        Sharpe = (mean_return - risk_free_rate) / std_dev_return
        
        Args:
            pnl_values: List of individual trade PnL values
            risk_free_rate: Risk-free rate (default 0 for crypto)
            
        Returns:
            Sharpe ratio (annualized assuming 24 trades/day)
        """
        if len(pnl_values) < 2:
            return 0.0
        
        returns = [float(pnl) for pnl in pnl_values]
        mean_return = sum(returns) / len(returns)
        
        # Calculate standard deviation
        variance = sum((r - mean_return) ** 2 for r in returns) / (len(returns) - 1)
        std_dev = variance ** 0.5
        
        if std_dev == 0:
            return 0.0
        
        # Sharpe ratio (annualized: sqrt(252 trading days * ~24 trades/day))
        # Using sqrt(252) for daily, we'll use hourly data so sqrt(8760) for yearly
        annualization_factor = (8760 ** 0.5)  # Hours in a year
        sharpe = ((mean_return - risk_free_rate) / std_dev) * annualization_factor
        
        return round(sharpe, 4)
    
    def _calculate_profit_factor(self, trades: List[Dict]) -> float:
        """
        Calculate Profit Factor from trades.
        
        Profit Factor = Gross Profits / Gross Losses
        
        Args:
            trades: List of trade dicts with 'net_pnl' key
            
        Returns:
            Profit factor (>1 is profitable, >1.5 is good)
        """
        gross_profits = Decimal("0")
        gross_losses = Decimal("0")
        
        for t in trades:
            pnl = t.get("net_pnl") or Decimal("0")
            if pnl > 0:
                gross_profits += pnl
            else:
                gross_losses += abs(pnl)
        
        if gross_losses == 0:
            return float("inf") if gross_profits > 0 else 0.0
        
        return round(float(gross_profits / gross_losses), 4)

    async def backfill_missing_hours(
        self,
        parameter_version_id: int,
        hours_back: int = 24
    ) -> int:
        """
        Backfill any missing hourly metrics.

        Args:
            parameter_version_id: Current version ID
            hours_back: How many hours to check

        Returns:
            Number of hours backfilled
        """
        now = datetime.utcnow()
        filled = 0

        async with self.db.acquire() as conn:
            for i in range(hours_back):
                hour = (now - timedelta(hours=i)).replace(minute=0, second=0, microsecond=0)

                # Check if exists
                exists = await conn.fetchval("""
                    SELECT 1 FROM hourly_metrics WHERE hour_start = $1
                """, hour)

                if not exists:
                    # Collect metrics for this hour
                    await self.collect_hourly_metrics(parameter_version_id)
                    filled += 1

        if filled > 0:
            logger.info(f"Backfilled {filled} missing hourly metrics")

        return filled
