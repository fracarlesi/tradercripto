"""
Skill-based metrics calculator for daily learning system.

Calculates trading performance metrics that are NOT influenced by market direction,
only by the trader's (AI's) ability to make good decisions.

These metrics measure:
- Decision accuracy (win rate, profit factor)
- Risk management quality (risk/reward ratio, max drawdown %)
- Execution quality (entry/exit timing within candles)
- Signal quality (false signal rate)
"""

import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Any
import numpy as np
from sqlalchemy import select, and_, func as sql_func
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import get_async_session_factory
from database.models import Trade, DecisionSnapshot

logger = logging.getLogger(__name__)


async def calculate_daily_skill_metrics(
    account_id: int,
    target_date: date
) -> Dict[str, Any]:
    """
    Calculate skill-based metrics for a specific trading day.

    Args:
        account_id: Account ID to analyze
        target_date: Date to analyze (e.g., date(2025, 11, 20))

    Returns:
        Dict with skill-based metrics:
        {
            "win_rate_pct": 65.0,
            "profit_factor": 2.3,
            "risk_reward_ratio": 1.8,
            "max_drawdown_pct": 3.5,
            "sharpe_ratio": 1.2,
            "sortino_ratio": 1.5,
            "entry_timing_quality_pct": 72.0,
            "exit_timing_quality_pct": 68.0,
            "false_signal_rate_pct": 12.0,
            "avg_hold_time_hours": 2.4,

            # Context metrics
            "total_trades": 8,
            "winning_trades": 5,
            "losing_trades": 3,
            "total_decisions": 480,  # Every 3 min = 480 decisions/day
        }
    """
    async with get_async_session_factory()() as session:
        # 1. Fetch today's COMPLETED trades (entry + exit both on target_date)
        trades = await _fetch_daily_trades(session, account_id, target_date)

        # 2. Fetch today's decision snapshots
        snapshots = await _fetch_daily_snapshots(session, account_id, target_date)

        if not trades:
            logger.warning(f"No trades found for {target_date}")
            return _empty_metrics()

        # 3. Calculate metrics
        metrics = {}

        # Basic counts
        total_trades = len(trades)
        winning_trades = [t for t in trades if t['pnl'] > 0]
        losing_trades = [t for t in trades if t['pnl'] <= 0]

        metrics['total_trades'] = total_trades
        metrics['winning_trades'] = len(winning_trades)
        metrics['losing_trades'] = len(losing_trades)
        metrics['total_decisions'] = len(snapshots)

        # Win rate
        metrics['win_rate_pct'] = (len(winning_trades) / total_trades * 100) if total_trades > 0 else 0

        # Profit factor (gross profit / gross loss)
        gross_profit = sum(t['pnl'] for t in winning_trades)
        gross_loss = abs(sum(t['pnl'] for t in losing_trades))
        metrics['profit_factor'] = (gross_profit / gross_loss) if gross_loss > 0 else float('inf') if gross_profit > 0 else 0

        # Risk/Reward ratio (avg win / avg loss)
        avg_win = gross_profit / len(winning_trades) if winning_trades else 0
        avg_loss = gross_loss / len(losing_trades) if losing_trades else 0
        metrics['risk_reward_ratio'] = (avg_win / avg_loss) if avg_loss > 0 else float('inf') if avg_win > 0 else 0

        # Max drawdown (%) - peak to trough as % of peak
        metrics['max_drawdown_pct'] = _calculate_max_drawdown_pct(trades)

        # Sharpe ratio (risk-adjusted return)
        metrics['sharpe_ratio'] = _calculate_sharpe_ratio(trades)

        # Sortino ratio (downside risk-adjusted return)
        metrics['sortino_ratio'] = _calculate_sortino_ratio(trades)

        # Entry/Exit timing quality (how close to optimal within candle)
        entry_quality, exit_quality = _calculate_timing_quality(trades)
        metrics['entry_timing_quality_pct'] = entry_quality
        metrics['exit_timing_quality_pct'] = exit_quality

        # False signal rate (trades closed < 1h with loss)
        metrics['false_signal_rate_pct'] = _calculate_false_signal_rate(trades)

        # Average hold time
        avg_duration_minutes = sum(t['duration_minutes'] for t in trades) / total_trades if total_trades > 0 else 0
        metrics['avg_hold_time_hours'] = avg_duration_minutes / 60

        logger.info(f"✅ Calculated skill metrics for {target_date}: {metrics['win_rate_pct']:.1f}% win rate")

        return metrics


async def _fetch_daily_trades(
    session: AsyncSession,
    account_id: int,
    target_date: date
) -> List[Dict[str, Any]]:
    """
    Fetch all COMPLETED trades for a specific day.

    Uses the trade pairing logic from trade_history_routes to reconstruct
    complete trades from individual fills.
    """
    from api.trade_history_routes import _calculate_complete_trades

    # Get ALL complete trades (no time filter)
    all_complete_trades = await _calculate_complete_trades(
        account_id=account_id,
        db=session,
        minutes=None,  # No time filter - we'll filter after
        symbol_filter=None
    )

    # Filter for trades that EXITED on target_date
    start_of_day = datetime.combine(target_date, datetime.min.time()).replace(tzinfo=None)
    end_of_day = datetime.combine(target_date, datetime.max.time()).replace(tzinfo=None)

    daily_trades = []
    for trade in all_complete_trades:
        # Parse exit_time string to datetime (format: "2025-11-20T15:30:00Z")
        exit_time_str = trade['exit_time'].rstrip('Z')  # Remove Z suffix
        exit_time = datetime.fromisoformat(exit_time_str).replace(tzinfo=None)

        # Check if exit happened on target_date
        if start_of_day <= exit_time <= end_of_day:
            # Parse entry_time as well for complete info
            entry_time_str = trade['entry_time'].rstrip('Z')
            entry_time = datetime.fromisoformat(entry_time_str).replace(tzinfo=None)

            daily_trades.append({
                'symbol': trade['symbol'],
                'entry_time': entry_time,
                'exit_time': exit_time,
                'entry_price': trade['entry_price'],
                'exit_price': trade['exit_price'],
                'quantity': trade['quantity'],
                'pnl': trade['pnl'],
                'pnl_pct': trade['pnl_pct'],
                'duration_minutes': trade['duration_minutes'],
                # Candle data not available from fills - set to None
                'candle_high_entry': None,
                'candle_low_entry': None,
                'candle_high_exit': None,
                'candle_low_exit': None,
            })

    return daily_trades


async def _fetch_daily_snapshots(
    session: AsyncSession,
    account_id: int,
    target_date: date
) -> List[Dict[str, Any]]:
    """Fetch all decision snapshots for a specific day."""
    start_of_day = datetime.combine(target_date, datetime.min.time())
    end_of_day = datetime.combine(target_date, datetime.max.time())

    stmt = select(DecisionSnapshot).where(
        and_(
            DecisionSnapshot.account_id == account_id,
            DecisionSnapshot.timestamp >= start_of_day,
            DecisionSnapshot.timestamp <= end_of_day
        )
    )

    result = await session.execute(stmt)
    snapshots_orm = result.scalars().all()

    return [
        {
            'timestamp': s.timestamp,
            'symbol': s.symbol,
            'actual_decision': s.actual_decision,
            'reasoning': s.deepseek_reasoning
        }
        for s in snapshots_orm
    ]


def _calculate_max_drawdown_pct(trades: List[Dict]) -> float:
    """Calculate max drawdown as percentage of peak equity."""
    if not trades:
        return 0.0

    cumulative_pnl = 0
    peak = 100  # Start with 100 as baseline
    max_drawdown_pct = 0

    for t in sorted(trades, key=lambda x: x['exit_time']):
        cumulative_pnl += t['pnl']
        current_equity = 100 + cumulative_pnl  # As if starting with $100

        if current_equity > peak:
            peak = current_equity

        drawdown_pct = ((peak - current_equity) / peak) * 100 if peak > 0 else 0

        if drawdown_pct > max_drawdown_pct:
            max_drawdown_pct = drawdown_pct

    return round(max_drawdown_pct, 2)


def _calculate_sharpe_ratio(trades: List[Dict], risk_free_rate: float = 0.0) -> float:
    """Calculate Sharpe ratio (return / volatility)."""
    if not trades or len(trades) < 2:
        return 0.0

    returns = [t['pnl_pct'] for t in trades]

    mean_return = np.mean(returns)
    std_return = np.std(returns, ddof=1)  # Sample std dev

    if std_return == 0:
        return 0.0

    sharpe = (mean_return - risk_free_rate) / std_return

    return round(sharpe, 2)


def _calculate_sortino_ratio(trades: List[Dict], risk_free_rate: float = 0.0) -> float:
    """Calculate Sortino ratio (return / downside deviation)."""
    if not trades or len(trades) < 2:
        return 0.0

    returns = [t['pnl_pct'] for t in trades]

    mean_return = np.mean(returns)

    # Downside deviation (only negative returns)
    downside_returns = [r for r in returns if r < 0]

    if not downside_returns:
        return float('inf')  # No downside = infinite Sortino

    downside_deviation = np.std(downside_returns, ddof=1)

    if downside_deviation == 0:
        return 0.0

    sortino = (mean_return - risk_free_rate) / downside_deviation

    return round(sortino, 2)


def _calculate_timing_quality(trades: List[Dict]) -> tuple[float, float]:
    """
    Calculate entry and exit timing quality.

    Quality = How close to optimal price within the candle.
    - For LONG entry: Quality = 1 - (entry_price - candle_low) / (candle_high - candle_low)
    - For LONG exit: Quality = (exit_price - candle_low) / (candle_high - candle_low)

    Returns:
        (entry_quality_pct, exit_quality_pct)
    """
    entry_qualities = []
    exit_qualities = []

    for t in trades:
        # Entry timing (buy at low, sell at high is perfect)
        if t['candle_high_entry'] and t['candle_low_entry']:
            high = t['candle_high_entry']
            low = t['candle_low_entry']
            entry = t['entry_price']

            if high != low:
                # For buy: lower is better (quality = 1 when entry == low)
                entry_quality = 1 - ((entry - low) / (high - low))
                entry_qualities.append(max(0, min(1, entry_quality)))  # Clamp 0-1

        # Exit timing (sell at high is perfect)
        if t['candle_high_exit'] and t['candle_low_exit']:
            high = t['candle_high_exit']
            low = t['candle_low_exit']
            exit_price = t['exit_price']

            if high != low:
                # For sell: higher is better (quality = 1 when exit == high)
                exit_quality = (exit_price - low) / (high - low)
                exit_qualities.append(max(0, min(1, exit_quality)))  # Clamp 0-1

    entry_avg = (sum(entry_qualities) / len(entry_qualities) * 100) if entry_qualities else 50.0
    exit_avg = (sum(exit_qualities) / len(exit_qualities) * 100) if exit_qualities else 50.0

    return round(entry_avg, 1), round(exit_avg, 1)


def _calculate_false_signal_rate(trades: List[Dict]) -> float:
    """
    Calculate false signal rate.

    False signal = Trade closed < 1 hour with loss.
    This indicates the signal was wrong or too weak.
    """
    if not trades:
        return 0.0

    false_signals = [
        t for t in trades
        if t['duration_minutes'] < 60 and t['pnl'] < 0
    ]

    return round((len(false_signals) / len(trades)) * 100, 1)


def _empty_metrics() -> Dict[str, Any]:
    """Return empty metrics when no trades available."""
    return {
        "win_rate_pct": 0,
        "profit_factor": 0,
        "risk_reward_ratio": 0,
        "max_drawdown_pct": 0,
        "sharpe_ratio": 0,
        "sortino_ratio": 0,
        "entry_timing_quality_pct": 0,
        "exit_timing_quality_pct": 0,
        "false_signal_rate_pct": 0,
        "avg_hold_time_hours": 0,
        "total_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "total_decisions": 0,
    }
