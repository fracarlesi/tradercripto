"""
Tiered Summarization for LLM Context
Handles growing data by providing detail for recent periods
and aggregates for historical periods.
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from decimal import Decimal
import json
import logging

logger = logging.getLogger(__name__)


class TieredSummarizer:
    """
    Creates tiered summaries for LLM context:
    - Last 24 hours: Hourly detail
    - Last 7 days: Daily aggregates
    - Last 30 days: Weekly aggregates
    - Lifetime: Monthly aggregates

    This keeps context window manageable (~1000 tokens)
    while preserving key information.
    """

    def __init__(self, db):
        """
        Initialize summarizer.

        Args:
            db: Database connection pool
        """
        self.db = db

    async def get_context_for_llm(self, current_params: Dict) -> Dict:
        """
        Build complete context for DeepSeek optimization.

        Args:
            current_params: Current parameter configuration

        Returns:
            Structured data ready for prompt formatting
        """
        now = datetime.now(timezone.utc)

        context = {
            "timestamp": now.isoformat(),
            "current_parameters": current_params,
            "recent_hours": await self._get_hourly_detail(now, hours=24),
            "daily_summaries": await self._get_daily_aggregates(now, days=7),
            "weekly_summaries": await self._get_weekly_aggregates(now, weeks=4),
            "parameter_history": await self._get_parameter_performance(),
            "market_regime": await self._detect_market_regime(now),
            "strategy_breakdown": await self._get_strategy_breakdown(now),
            "performance_metrics": await self._get_performance_metrics(now, days=7)
        }

        return context

    async def _get_hourly_detail(self, now: datetime, hours: int) -> List[Dict]:
        """Get detailed hourly metrics for last N hours."""
        start = now - timedelta(hours=hours)

        async with self.db.acquire() as conn:
            rows = await conn.fetch("""
                SELECT
                    hour_start,
                    trades_count,
                    win_count,
                    loss_count,
                    net_pnl,
                    pnl_by_strategy,
                    max_drawdown_pct,
                    volatility_index
                FROM hourly_metrics
                WHERE hour_start >= $1
                ORDER BY hour_start DESC
            """, start)

            return [dict(r) for r in rows]

    async def _get_daily_aggregates(self, now: datetime, days: int) -> List[Dict]:
        """Get daily aggregated metrics."""
        start = now - timedelta(days=days)

        async with self.db.acquire() as conn:
            rows = await conn.fetch("""
                SELECT
                    DATE(hour_start) as date,
                    SUM(trades_count) as trades,
                    SUM(win_count) as wins,
                    SUM(loss_count) as losses,
                    SUM(net_pnl) as net_pnl,
                    AVG(max_drawdown_pct) as avg_drawdown,
                    AVG(volatility_index) as avg_volatility
                FROM hourly_metrics
                WHERE hour_start >= $1
                GROUP BY DATE(hour_start)
                ORDER BY date DESC
            """, start)

            return [dict(r) for r in rows]

    async def _get_weekly_aggregates(self, now: datetime, weeks: int) -> List[Dict]:
        """Get weekly aggregated metrics."""
        start = now - timedelta(weeks=weeks)

        async with self.db.acquire() as conn:
            rows = await conn.fetch("""
                SELECT
                    DATE_TRUNC('week', hour_start) as week_start,
                    SUM(trades_count) as trades,
                    SUM(win_count) as wins,
                    SUM(net_pnl) as net_pnl,
                    AVG(volatility_index) as avg_volatility
                FROM hourly_metrics
                WHERE hour_start >= $1
                GROUP BY DATE_TRUNC('week', hour_start)
                ORDER BY week_start DESC
            """, start)

            return [dict(r) for r in rows]

    async def _get_parameter_performance(self) -> List[Dict]:
        """Get performance correlation for each parameter version."""
        async with self.db.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM parameter_performance
                ORDER BY created_at DESC
                LIMIT 10
            """)

            return [dict(r) for r in rows]

    async def _get_strategy_breakdown(self, now: datetime) -> Dict:
        """Get per-strategy performance breakdown for last 7 days."""
        start = now - timedelta(days=7)

        async with self.db.acquire() as conn:
            rows = await conn.fetch("""
                SELECT
                    strategy,
                    COUNT(*) as trades,
                    SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(net_pnl) as total_pnl,
                    AVG(net_pnl) as avg_pnl,
                    AVG(duration_seconds) as avg_duration
                FROM trades
                WHERE exit_time >= $1 AND is_closed = TRUE
                GROUP BY strategy
            """, start)

            return {r['strategy']: dict(r) for r in rows if r['strategy']}

    async def _detect_market_regime(self, now: datetime) -> Dict:
        """
        Detect current market regime based on recent metrics.
        Returns regime classification for LLM context.
        """
        start = now - timedelta(hours=24)

        async with self.db.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT
                    MIN(btc_price_start) as low,
                    MAX(btc_price_end) as high,
                    AVG(volatility_index) as avg_vol,
                    (SELECT btc_price_start FROM hourly_metrics
                     WHERE hour_start >= $1 AND btc_price_start IS NOT NULL
                     ORDER BY hour_start LIMIT 1) as start_price,
                    (SELECT btc_price_end FROM hourly_metrics
                     WHERE hour_start >= $1 AND btc_price_end IS NOT NULL
                     ORDER BY hour_start DESC LIMIT 1) as end_price
                FROM hourly_metrics
                WHERE hour_start >= $1
            """, start)

            if not row or not row['start_price'] or not row['end_price']:
                return {"regime": "unknown", "confidence": 0}

            change_pct = (row['end_price'] - row['start_price']) / row['start_price'] * 100
            range_pct = (row['high'] - row['low']) / row['low'] * 100 if row['low'] else 0
            avg_vol = float(row['avg_vol']) if row['avg_vol'] else 0

            # Classify regime
            if avg_vol > 0.02:  # High volatility
                if abs(change_pct) > 3:
                    regime = "trending_volatile"
                else:
                    regime = "ranging_volatile"
            else:  # Low volatility
                if abs(change_pct) > 2:
                    regime = "trending_calm"
                else:
                    regime = "ranging_calm"

            return {
                "regime": regime,
                "direction": "bullish" if change_pct > 0 else "bearish",
                "change_24h_pct": round(change_pct, 2),
                "range_24h_pct": round(range_pct, 2),
                "avg_volatility": round(avg_vol, 6),
                "confidence": 0.8  # Based on data availability
            }

    async def _get_performance_metrics(self, now: datetime, days: int = 7) -> Dict:
        """
        Calculate advanced performance metrics for walk-forward validation.
        
        Returns:
            Dict with Sharpe Ratio, Profit Factor, Max Drawdown, and other metrics
        """
        start = now - timedelta(days=days)
        
        async with self.db.acquire() as conn:
            # Get aggregated hourly metrics for the period
            hourly_row = await conn.fetchrow("""
                SELECT
                    COALESCE(AVG(sharpe_ratio), 0) as avg_sharpe,
                    COALESCE(AVG(profit_factor), 0) as avg_profit_factor,
                    COALESCE(MAX(max_drawdown_pct), 0) as max_drawdown,
                    COALESCE(SUM(net_pnl), 0) as total_pnl,
                    COALESCE(SUM(trades_count), 0) as total_trades,
                    COALESCE(SUM(win_count), 0) as total_wins
                FROM hourly_metrics
                WHERE hour_start >= $1
            """, start)
            
            # Calculate overall Sharpe from individual trades
            trades = await conn.fetch("""
                SELECT net_pnl
                FROM trades
                WHERE exit_time >= $1 AND is_closed = TRUE
                ORDER BY exit_time
            """, start)
            
            # Calculate rolling max drawdown from cumulative PnL
            cumulative_pnl = []
            running_total = 0
            for t in trades:
                pnl = float(t['net_pnl'] or 0)
                running_total += pnl
                cumulative_pnl.append(running_total)
            
            # Max drawdown calculation
            max_dd_pct = 0.0
            if cumulative_pnl:
                peak = cumulative_pnl[0]
                for val in cumulative_pnl:
                    if val > peak:
                        peak = val
                    if peak > 0:
                        dd = (peak - val) / peak * 100
                        max_dd_pct = max(max_dd_pct, dd)
            
            # Calculate overall Sharpe ratio from trade returns
            overall_sharpe = 0.0
            if len(trades) >= 2:
                returns = [float(t['net_pnl'] or 0) for t in trades]
                mean_ret = sum(returns) / len(returns)
                variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
                std_dev = variance ** 0.5
                if std_dev > 0:
                    # Annualized (assuming ~24 trades/day, 252 trading days)
                    overall_sharpe = (mean_ret / std_dev) * (252 * 24) ** 0.5
            
            # Calculate overall Profit Factor
            gross_profits = sum(float(t['net_pnl']) for t in trades if float(t['net_pnl'] or 0) > 0)
            gross_losses = abs(sum(float(t['net_pnl']) for t in trades if float(t['net_pnl'] or 0) < 0))
            overall_pf = gross_profits / gross_losses if gross_losses > 0 else (float('inf') if gross_profits > 0 else 0)
            
            total_trades = hourly_row['total_trades'] or 0
            total_wins = hourly_row['total_wins'] or 0
            win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0
            
            return {
                "period_days": days,
                "sharpe_ratio": round(overall_sharpe, 2),
                "profit_factor": round(overall_pf, 2) if overall_pf != float('inf') else 99.99,
                "max_drawdown_pct": round(max_dd_pct, 2),
                "total_pnl": float(hourly_row['total_pnl'] or 0),
                "total_trades": total_trades,
                "win_rate": round(win_rate, 1),
                "avg_hourly_sharpe": round(float(hourly_row['avg_sharpe'] or 0), 2),
                "avg_hourly_pf": round(float(hourly_row['avg_profit_factor'] or 0), 2),
                # Targets for optimization
                "sharpe_target": 1.5,
                "profit_factor_target": 1.5,
                "max_drawdown_target": 10.0  # Max acceptable drawdown %
            }

    def format_for_prompt(self, context: Dict) -> str:
        """
        Format context into a concise string for LLM prompt.
        Designed to fit within context window limits (~1000 tokens).

        Args:
            context: Context dict from get_context_for_llm()

        Returns:
            Formatted string for LLM
        """
        lines = []

        # Current parameters
        lines.append("## CURRENT PARAMETERS")
        params = context["current_parameters"]
        lines.append(
            f"Global: TP={params['tp_pct']*100:.2f}%, SL={params['sl_pct']*100:.2f}%, "
            f"Size=${params['position_size_usd']}, Leverage={params['leverage']}x"
        )
        m = params['momentum']
        lines.append(
            f"Momentum ({'ON' if m['enabled'] else 'OFF'}): "
            f"EMA {m['ema_fast']}/{m['ema_slow']}, "
            f"RSI {m['rsi_period']} ({m['rsi_long_threshold']}/{m['rsi_short_threshold']})"
        )
        mr = params['mean_reversion']
        lines.append(
            f"MeanRev ({'ON' if mr['enabled'] else 'OFF'}): "
            f"RSI {mr['rsi_oversold']}/{mr['rsi_overbought']}, "
            f"BB {mr['bb_period']} ({mr['bb_std']})"
        )
        b = params['breakout']
        lines.append(
            f"Breakout ({'ON' if b['enabled'] else 'OFF'}): "
            f"Lookback {b['lookback_bars']}, Min {b['min_breakout_pct']*100:.2f}%"
        )
        lines.append("")

        # Performance Metrics (Walk-Forward Validation)
        perf = context.get("performance_metrics", {})
        if perf:
            lines.append("## PERFORMANCE METRICS (7-day out-of-sample)")
            sharpe = perf.get('sharpe_ratio', 0)
            pf = perf.get('profit_factor', 0)
            max_dd = perf.get('max_drawdown_pct', 0)
            
            # Show current vs target with status indicators
            sharpe_status = "OK" if sharpe >= 1.5 else "BELOW TARGET"
            pf_status = "OK" if pf >= 1.5 else "BELOW TARGET"
            dd_status = "OK" if max_dd <= 10.0 else "ABOVE TARGET"
            
            lines.append(f"  Sharpe Ratio: {sharpe:.2f} (target >= 1.5) [{sharpe_status}]")
            lines.append(f"  Profit Factor: {pf:.2f} (target >= 1.5) [{pf_status}]")
            lines.append(f"  Max Drawdown: {max_dd:.2f}% (target <= 10%) [{dd_status}]")
            lines.append(f"  Total P&L: ${perf.get('total_pnl', 0):+.2f}")
            lines.append(f"  Total Trades: {perf.get('total_trades', 0)}, Win Rate: {perf.get('win_rate', 0):.1f}%")
            lines.append("")

        # Market regime
        regime = context.get("market_regime", {})
        if regime.get("regime") != "unknown":
            lines.append(f"## MARKET REGIME: {regime.get('regime', 'unknown').upper()} ({regime.get('direction', 'neutral')})")
            lines.append(
                f"24h Change: {regime.get('change_24h_pct', 0):+.2f}%, "
                f"Volatility: {regime.get('avg_volatility', 0):.6f}"
            )
            lines.append("")

        # Strategy breakdown (last 7 days)
        breakdown = context.get("strategy_breakdown", {})
        if breakdown:
            lines.append("## STRATEGY PERFORMANCE (7 days)")
            for strat, data in breakdown.items():
                trades = data.get('trades', 0)
                wins = data.get('wins', 0)
                pnl = float(data.get('total_pnl', 0))
                wr = (wins / trades * 100) if trades > 0 else 0
                lines.append(f"  {strat}: {trades} trades, {wr:.0f}% WR, ${pnl:+.2f}")
            lines.append("")

        # Last 12 hours detail (condensed)
        lines.append("## LAST 12 HOURS (hourly)")
        for h in context.get("recent_hours", [])[:12]:
            pnl = float(h.get('net_pnl', 0) or 0)
            hour_str = h['hour_start'].strftime('%H:00') if hasattr(h['hour_start'], 'strftime') else str(h['hour_start'])[:13]
            lines.append(
                f"  {hour_str}: "
                f"{h.get('trades_count', 0)} trades, "
                f"{h.get('win_count', 0)}W/{h.get('loss_count', 0)}L, "
                f"PnL ${pnl:+.2f}"
            )
        lines.append("")

        # Daily summary (last 7 days)
        lines.append("## LAST 7 DAYS (daily)")
        for d in context.get("daily_summaries", []):
            trades = d.get('trades', 0) or 0
            wins = d.get('wins', 0) or 0
            pnl = float(d.get('net_pnl', 0) or 0)
            wr = (wins / trades * 100) if trades > 0 else 0
            date_str = str(d.get('date', ''))[:10]
            lines.append(f"  {date_str}: {trades} trades, {wr:.0f}% WR, PnL ${pnl:+.2f}")
        lines.append("")

        # Parameter history performance (top 5)
        lines.append("## PARAMETER VERSION PERFORMANCE")
        for p in context.get("parameter_history", [])[:5]:
            pnl = float(p.get('total_pnl', 0) or 0)
            hourly = float(p.get('hourly_pnl_avg', 0) or 0)
            lines.append(
                f"  v{p.get('version_id', '?')} ({p.get('source', '?')}): "
                f"{p.get('total_trades', 0)} trades, {p.get('win_rate', 0)}% WR, "
                f"PnL ${pnl:+.2f} (${hourly:+.4f}/hr)"
            )

        return "\n".join(lines)

    async def get_token_estimate(self, context: Dict) -> int:
        """
        Estimate token count for context.
        Rough estimate: ~4 chars per token.
        """
        formatted = self.format_for_prompt(context)
        return len(formatted) // 4
