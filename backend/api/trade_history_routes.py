"""Trade History API - Complete trade log with P&L and duration"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import get_db
from database.models import Trade, Position, Account

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trade-history", tags=["trade-history"])


async def _calculate_complete_trades(
    account_id: int,
    db: AsyncSession,
    days: Optional[int] = None,
    symbol_filter: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Calculate complete trades by pairing entry/exit fills.

    Algorithm:
    1. Fetch all trades for account (filtered by days/symbol if provided)
    2. Group by symbol
    3. For each symbol, sort by time and pair buy→sell or sell→buy (for shorts)
    4. Calculate P&L and duration for each complete trade

    Args:
        account_id: Account ID to fetch trades for
        db: Database session
        days: Optional filter for last N days
        symbol_filter: Optional filter for specific symbol

    Returns:
        List of complete trade dicts with entry/exit/pnl/duration
    """
    try:
        # Build query
        query = select(Trade).where(Trade.account_id == account_id)

        if days:
            cutoff = datetime.utcnow() - timedelta(days=days)
            query = query.where(Trade.trade_time >= cutoff)

        if symbol_filter:
            query = query.where(Trade.symbol == symbol_filter)

        query = query.order_by(Trade.symbol, Trade.trade_time)

        result = await db.execute(query)
        trades = result.scalars().all()

        if not trades:
            return []

        # Group trades by symbol
        trades_by_symbol = {}
        for trade in trades:
            if trade.symbol not in trades_by_symbol:
                trades_by_symbol[trade.symbol] = []
            trades_by_symbol[trade.symbol].append(trade)

        # Process each symbol to find complete trades
        complete_trades = []

        for symbol, symbol_trades in trades_by_symbol.items():
            # Stack to track open positions
            position_stack = []

            for trade in symbol_trades:
                quantity = float(trade.quantity)
                price = float(trade.price)
                commission = float(trade.commission)

                if trade.side.lower() == "buy":
                    # Entry for LONG position
                    position_stack.append({
                        'entry_side': 'buy',
                        'entry_time': trade.trade_time,
                        'entry_price': price,
                        'quantity': quantity,
                        'entry_commission': commission,
                        'trade_id': trade.id
                    })

                elif trade.side.lower() == "sell":
                    # Exit for LONG or entry for SHORT
                    if position_stack and position_stack[-1]['entry_side'] == 'buy':
                        # Exit LONG position
                        entry = position_stack.pop()

                        # Calculate P&L
                        entry_cost = entry['entry_price'] * entry['quantity']
                        exit_value = price * quantity
                        pnl = exit_value - entry_cost - entry['entry_commission'] - commission
                        pnl_pct = (pnl / entry_cost) * 100 if entry_cost > 0 else 0

                        # Calculate duration
                        duration = trade.trade_time - entry['entry_time']
                        duration_minutes = duration.total_seconds() / 60

                        complete_trades.append({
                            'symbol': symbol,
                            'side': 'LONG',
                            'entry_time': entry['entry_time'].isoformat(),
                            'exit_time': trade.trade_time.isoformat(),
                            'entry_price': entry['entry_price'],
                            'exit_price': price,
                            'quantity': entry['quantity'],
                            'pnl': round(pnl, 2),
                            'pnl_pct': round(pnl_pct, 2),
                            'duration_minutes': int(duration_minutes),
                            'total_commission': round(entry['entry_commission'] + commission, 4),
                            'entry_trade_id': entry['trade_id'],
                            'exit_trade_id': trade.id,
                            'leverage': entry.get('leverage'),  # Will be None for historical trades
                            'strategy': entry.get('strategy')   # Will be None for historical trades
                        })
                    else:
                        # Entry for SHORT position
                        position_stack.append({
                            'entry_side': 'sell',
                            'entry_time': trade.trade_time,
                            'entry_price': price,
                            'quantity': quantity,
                            'entry_commission': commission,
                            'trade_id': trade.id
                        })

        logger.info(
            f"Calculated {len(complete_trades)} complete trades for account {account_id} "
            f"from {len(trades)} individual fills"
        )

        # Sort by exit time (most recent first)
        complete_trades.sort(key=lambda x: x['exit_time'], reverse=True)

        return complete_trades

    except Exception as e:
        logger.error(f"Failed to calculate complete trades: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to calculate trades: {str(e)}")


@router.get("/{account_id}")
async def get_trade_history(
    account_id: int,
    db: AsyncSession = Depends(get_db),
    days: Optional[int] = Query(None, description="Filter last N days"),
    symbol: Optional[str] = Query(None, description="Filter by symbol")
) -> Dict[str, Any]:
    """
    Get complete trade history with P&L and duration.

    Returns:
        {
            "account_id": 1,
            "total_trades": 15,
            "total_pnl": 12.50,
            "win_rate": 66.7,
            "trades": [
                {
                    "symbol": "BTC",
                    "side": "LONG",
                    "entry_time": "2025-11-15T10:00:00",
                    "exit_time": "2025-11-15T12:30:00",
                    "entry_price": 96000.0,
                    "exit_price": 97000.0,
                    "quantity": 0.001,
                    "pnl": 0.95,
                    "pnl_pct": 1.04,
                    "duration_minutes": 150,
                    "total_commission": 0.05
                },
                ...
            ]
        }
    """
    try:
        # Verify account exists
        result = await db.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()

        if not account:
            raise HTTPException(status_code=404, detail=f"Account {account_id} not found")

        # Calculate complete trades
        trades = await _calculate_complete_trades(account_id, db, days, symbol)

        # Calculate statistics
        total_trades = len(trades)
        total_pnl = sum(t['pnl'] for t in trades)
        winning_trades = sum(1 for t in trades if t['pnl'] > 0)
        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0

        avg_pnl = total_pnl / total_trades if total_trades > 0 else 0
        avg_duration = sum(t['duration_minutes'] for t in trades) / total_trades if total_trades > 0 else 0

        return {
            "account_id": account_id,
            "total_trades": total_trades,
            "total_pnl": round(total_pnl, 2),
            "win_rate": round(win_rate, 1),
            "winning_trades": winning_trades,
            "losing_trades": total_trades - winning_trades,
            "avg_pnl": round(avg_pnl, 2),
            "avg_duration_minutes": int(avg_duration),
            "trades": trades
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to get trade history for account {account_id}",
            extra={"context": {"account_id": account_id, "error": str(e)}},
            exc_info=True
        )
        raise HTTPException(status_code=500, detail=f"Failed to get trade history: {str(e)}")


@router.get("/{account_id}/summary")
async def get_trade_summary(
    account_id: int,
    db: AsyncSession = Depends(get_db),
    days: int = Query(30, description="Summary period in days")
) -> Dict[str, Any]:
    """
    Get trade summary statistics for specified period.

    Returns:
        {
            "period_days": 30,
            "total_trades": 45,
            "total_pnl": 125.50,
            "win_rate": 68.9,
            "avg_win": 5.20,
            "avg_loss": -3.10,
            "best_trade": 15.00,
            "worst_trade": -8.50,
            "total_commissions": 12.30
        }
    """
    try:
        trades = await _calculate_complete_trades(account_id, db, days=days)

        if not trades:
            return {
                "period_days": days,
                "total_trades": 0,
                "total_pnl": 0,
                "win_rate": 0,
                "avg_win": 0,
                "avg_loss": 0,
                "best_trade": 0,
                "worst_trade": 0,
                "total_commissions": 0
            }

        winning = [t for t in trades if t['pnl'] > 0]
        losing = [t for t in trades if t['pnl'] <= 0]

        return {
            "period_days": days,
            "total_trades": len(trades),
            "total_pnl": round(sum(t['pnl'] for t in trades), 2),
            "win_rate": round(len(winning) / len(trades) * 100, 1),
            "avg_win": round(sum(t['pnl'] for t in winning) / len(winning), 2) if winning else 0,
            "avg_loss": round(sum(t['pnl'] for t in losing) / len(losing), 2) if losing else 0,
            "best_trade": round(max(t['pnl'] for t in trades), 2),
            "worst_trade": round(min(t['pnl'] for t in trades), 2),
            "total_commissions": round(sum(t['total_commission'] for t in trades), 2)
        }

    except Exception as e:
        logger.error(
            f"Failed to get trade summary for account {account_id}",
            extra={"context": {"account_id": account_id, "error": str(e)}},
            exc_info=True
        )
        raise HTTPException(status_code=500, detail=f"Failed to get summary: {str(e)}")
