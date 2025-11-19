"""Trade History API - Complete trade log with P&L and duration"""

import logging
from datetime import datetime, timedelta, timezone
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
    minutes: Optional[int] = None,
    symbol_filter: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Calculate complete trades by pairing entry/exit fills.

    Algorithm:
    1. Fetch ALL trades for account (no time filter on fills)
    2. Group by symbol
    3. For each symbol, sort by time and pair buy→sell or sell→buy (for shorts)
    4. Calculate P&L and duration for each complete trade
    5. Filter complete trades by exit_time if minutes is provided

    Args:
        account_id: Account ID to fetch trades for
        db: Database session
        minutes: Optional filter - filters by EXIT TIME of complete trades
        symbol_filter: Optional filter for specific symbol

    Returns:
        List of complete trade dicts with entry/exit/pnl/duration
    """
    try:
        # Build query - fetch ALL trades (no time filter on individual fills)
        query = select(Trade).where(Trade.account_id == account_id)

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
            # Stack to track open positions (FIFO for correct P&L matching)
            position_stack = []

            for trade in symbol_trades:
                quantity = float(trade.quantity)
                price = float(trade.price)
                commission = float(trade.commission)

                if trade.side.lower() == "buy":
                    # Exit SHORT or entry LONG
                    if position_stack and position_stack[-1]['entry_side'] == 'sell':
                        # Exit SHORT position - match with accumulated short entries
                        remaining_exit_qty = quantity
                        exit_commission = commission

                        # Accumulate matched short entries
                        matched_entries = []

                        while remaining_exit_qty > 0 and position_stack and position_stack[-1]['entry_side'] == 'sell':
                            entry = position_stack[-1]

                            # Match quantity (min of entry and exit remaining)
                            matched_qty = min(entry['quantity'], remaining_exit_qty)

                            # Record this match
                            matched_entries.append({
                                'entry_time': entry['entry_time'],
                                'entry_price': entry['entry_price'],
                                'quantity': matched_qty,
                                'entry_commission': entry['entry_commission'] * (matched_qty / entry['quantity']),
                                'trade_id': entry['trade_id']
                            })

                            # Update remaining quantities
                            entry['quantity'] -= matched_qty
                            entry['entry_commission'] *= (entry['quantity'] / (entry['quantity'] + matched_qty))
                            remaining_exit_qty -= matched_qty

                            # Remove entry from stack if fully matched
                            if entry['quantity'] <= 0.000001:  # Float precision tolerance
                                position_stack.pop()

                        # Calculate P&L for each matched SHORT pair
                        for matched in matched_entries:
                            matched_qty = matched['quantity']

                            # SHORT P&L: profit when exit price < entry price
                            entry_value = matched['entry_price'] * matched_qty
                            exit_cost = price * matched_qty
                            pnl = entry_value - exit_cost - matched['entry_commission'] - (exit_commission * matched_qty / quantity)
                            pnl_pct = (pnl / entry_value) * 100 if entry_value > 0 else 0

                            # Calculate duration
                            duration = trade.trade_time - matched['entry_time']
                            duration_minutes = duration.total_seconds() / 60

                            complete_trades.append({
                                'symbol': symbol,
                                'side': 'SHORT',
                                'entry_time': matched['entry_time'].isoformat(),
                                'exit_time': trade.trade_time.isoformat(),
                                'entry_price': matched['entry_price'],
                                'exit_price': price,
                                'quantity': matched_qty,
                                'pnl': round(pnl, 2),
                                'pnl_pct': round(pnl_pct, 2),
                                'duration_minutes': int(duration_minutes),
                                'total_commission': round(matched['entry_commission'] + (exit_commission * matched_qty / quantity), 4),
                                'entry_trade_id': matched['trade_id'],
                                'exit_trade_id': trade.id,
                                'leverage': float(trade.leverage) if trade.leverage else None,
                                'strategy': trade.strategy
                            })
                    else:
                        # Entry for LONG position - add to stack
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
                        # Exit LONG position - match with accumulated entries
                        remaining_exit_qty = quantity
                        exit_commission = commission

                        # Accumulate matched entries
                        matched_entries = []

                        while remaining_exit_qty > 0 and position_stack and position_stack[-1]['entry_side'] == 'buy':
                            entry = position_stack[-1]

                            # Match quantity (min of entry and exit remaining)
                            matched_qty = min(entry['quantity'], remaining_exit_qty)

                            # Record this match
                            matched_entries.append({
                                'entry_time': entry['entry_time'],
                                'entry_price': entry['entry_price'],
                                'quantity': matched_qty,
                                'entry_commission': entry['entry_commission'] * (matched_qty / entry['quantity']),
                                'trade_id': entry['trade_id']
                            })

                            # Update remaining quantities
                            entry['quantity'] -= matched_qty
                            entry['entry_commission'] *= (entry['quantity'] / (entry['quantity'] + matched_qty))
                            remaining_exit_qty -= matched_qty

                            # Remove entry from stack if fully matched
                            if entry['quantity'] <= 0.000001:  # Float precision tolerance
                                position_stack.pop()

                        # Calculate P&L for each matched pair
                        for matched in matched_entries:
                            matched_qty = matched['quantity']

                            # P&L calculation with MATCHED quantities
                            entry_cost = matched['entry_price'] * matched_qty
                            exit_value = price * matched_qty
                            pnl = exit_value - entry_cost - matched['entry_commission'] - (exit_commission * matched_qty / quantity)
                            pnl_pct = (pnl / entry_cost) * 100 if entry_cost > 0 else 0

                            # Calculate duration
                            duration = trade.trade_time - matched['entry_time']
                            duration_minutes = duration.total_seconds() / 60

                            complete_trades.append({
                                'symbol': symbol,
                                'side': 'LONG',
                                'entry_time': matched['entry_time'].isoformat(),
                                'exit_time': trade.trade_time.isoformat(),
                                'entry_price': matched['entry_price'],
                                'exit_price': price,
                                'quantity': matched_qty,
                                'pnl': round(pnl, 2),
                                'pnl_pct': round(pnl_pct, 2),
                                'duration_minutes': int(duration_minutes),
                                'total_commission': round(matched['entry_commission'] + (exit_commission * matched_qty / quantity), 4),
                                'entry_trade_id': matched['trade_id'],
                                'exit_trade_id': trade.id,
                                'leverage': float(trade.leverage) if trade.leverage else None,
                                'strategy': trade.strategy
                            })
                    else:
                        # Entry for SHORT position - add to stack
                        position_stack.append({
                            'entry_side': 'sell',
                            'entry_time': trade.trade_time,
                            'entry_price': price,
                            'quantity': quantity,
                            'entry_commission': commission,
                            'trade_id': trade.id
                        })

        # Filter by exit_time if minutes is provided (using UTC)
        if minutes:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
            # Convert cutoff to naive datetime string for comparison with DB timestamps
            # (DB stores naive UTC timestamps, so we strip timezone info for comparison)
            cutoff_naive = cutoff.replace(tzinfo=None).isoformat()
            complete_trades = [
                t for t in complete_trades
                if t['exit_time'] >= cutoff_naive
            ]
            logger.info(
                f"Filtered to {len(complete_trades)} trades with exit_time >= {cutoff_naive} UTC"
            )

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
    timeframe: Optional[str] = Query(None, description="Filter timeframe: 5m, 1h, 1d, or None for all"),
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

        # Convert timeframe to minutes
        minutes = None
        if timeframe:
            timeframe_map = {
                '5m': 5,
                '1h': 60,
                '1d': 1440,  # 24 * 60
            }
            minutes = timeframe_map.get(timeframe)
            # If timeframe is 'all' or unknown, minutes stays None (no filter)

        # Calculate complete trades
        trades = await _calculate_complete_trades(account_id, db, minutes, symbol)

        # Calculate statistics
        total_trades = len(trades)
        total_pnl = sum(t['pnl'] for t in trades)
        winning_trades_list = [t for t in trades if t['pnl'] > 0]
        losing_trades_list = [t for t in trades if t['pnl'] <= 0]
        winning_trades = len(winning_trades_list)
        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0

        avg_pnl = total_pnl / total_trades if total_trades > 0 else 0
        avg_duration = sum(t['duration_minutes'] for t in trades) / total_trades if total_trades > 0 else 0

        # Advanced metrics
        gross_profit = sum(t['pnl'] for t in winning_trades_list)
        gross_loss = abs(sum(t['pnl'] for t in losing_trades_list))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf') if gross_profit > 0 else 0

        avg_win = gross_profit / len(winning_trades_list) if winning_trades_list else 0
        avg_loss = abs(sum(t['pnl'] for t in losing_trades_list) / len(losing_trades_list)) if losing_trades_list else 0
        risk_reward = avg_win / avg_loss if avg_loss > 0 else float('inf') if avg_win > 0 else 0

        best_trade = max(t['pnl'] for t in trades) if trades else 0
        worst_trade = min(t['pnl'] for t in trades) if trades else 0

        # Calculate max drawdown (peak to trough)
        cumulative_pnl = 0
        peak = 0
        max_drawdown = 0
        for t in sorted(trades, key=lambda x: x['exit_time']):
            cumulative_pnl += t['pnl']
            if cumulative_pnl > peak:
                peak = cumulative_pnl
            drawdown = peak - cumulative_pnl
            if drawdown > max_drawdown:
                max_drawdown = drawdown

        # Calculate consecutive wins/losses
        max_consecutive_wins = 0
        max_consecutive_losses = 0
        current_wins = 0
        current_losses = 0
        for t in sorted(trades, key=lambda x: x['exit_time']):
            if t['pnl'] > 0:
                current_wins += 1
                current_losses = 0
                max_consecutive_wins = max(max_consecutive_wins, current_wins)
            else:
                current_losses += 1
                current_wins = 0
                max_consecutive_losses = max(max_consecutive_losses, current_losses)

        # Performance by symbol
        symbol_performance = {}
        for t in trades:
            sym = t['symbol']
            if sym not in symbol_performance:
                symbol_performance[sym] = {'trades': 0, 'wins': 0, 'pnl': 0}
            symbol_performance[sym]['trades'] += 1
            symbol_performance[sym]['pnl'] += t['pnl']
            if t['pnl'] > 0:
                symbol_performance[sym]['wins'] += 1

        # Calculate win rate per symbol
        for sym in symbol_performance:
            sp = symbol_performance[sym]
            sp['win_rate'] = round(sp['wins'] / sp['trades'] * 100, 1) if sp['trades'] > 0 else 0
            sp['pnl'] = round(sp['pnl'], 2)

        return {
            "account_id": account_id,
            "total_trades": total_trades,
            "total_pnl": round(total_pnl, 2),
            "win_rate": round(win_rate, 1),
            "winning_trades": winning_trades,
            "losing_trades": total_trades - winning_trades,
            "avg_pnl": round(avg_pnl, 2),
            "avg_duration_minutes": int(avg_duration),
            # Advanced metrics
            "profit_factor": round(profit_factor, 2) if profit_factor != float('inf') else 999.99,
            "risk_reward": round(risk_reward, 2) if risk_reward != float('inf') else 999.99,
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "best_trade": round(best_trade, 2),
            "worst_trade": round(worst_trade, 2),
            "max_drawdown": round(max_drawdown, 2),
            "max_consecutive_wins": max_consecutive_wins,
            "max_consecutive_losses": max_consecutive_losses,
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "symbol_performance": symbol_performance,
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
        # Convert days to minutes
        minutes = days * 1440 if days else None
        trades = await _calculate_complete_trades(account_id, db, minutes=minutes)

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
