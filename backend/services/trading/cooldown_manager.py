"""
Cooldown Manager - Prevents flip-flopping by enforcing minimum time between trades on same symbol.

Based on Perplexity research best practices:
1. Minimum 30-minute cooldown between same-symbol trades
2. Track recent trade history for AI context
3. Prevent rapid position reversals (LONG->SHORT->LONG in minutes)
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from database.models import AIDecisionLog, Trade

logger = logging.getLogger(__name__)

# Cooldown period in minutes
SAME_SYMBOL_COOLDOWN_MINUTES = 30
REVERSAL_COOLDOWN_MINUTES = 60  # Longer cooldown for position reversals


def check_cooldown(db: Session, account_id: int, symbol: str, operation: str) -> dict:
    """
    Check if a trade is allowed based on cooldown rules.

    Prevents flip-flopping by enforcing:
    1. 30-minute cooldown between any trades on same symbol
    2. 60-minute cooldown for position reversals (LONG->SHORT or SHORT->LONG)

    Args:
        db: Database session
        account_id: Account ID
        symbol: Symbol to trade
        operation: Proposed operation ("buy", "short", "sell")

    Returns:
        dict with:
        - allowed: bool - True if trade is allowed
        - reason: str - Reason if blocked
        - last_trade_time: datetime - When last trade occurred (if any)
        - minutes_remaining: int - Minutes until cooldown expires (if blocked)
    """
    now = datetime.now(timezone.utc)

    # Get last trade on this symbol from AI decision log
    stmt = (
        select(AIDecisionLog)
        .where(
            AIDecisionLog.account_id == account_id,
            AIDecisionLog.symbol == symbol,
            AIDecisionLog.executed == True,  # Only count executed trades
            AIDecisionLog.operation.in_(["buy", "short", "sell"])  # Not HOLD
        )
        .order_by(desc(AIDecisionLog.decision_time))
        .limit(1)
    )

    result = db.execute(stmt)
    last_decision = result.scalar_one_or_none()

    if not last_decision:
        # No previous trades on this symbol - allowed
        return {
            "allowed": True,
            "reason": "No previous trades on this symbol",
            "last_trade_time": None,
            "minutes_remaining": 0
        }

    last_trade_time = last_decision.decision_time
    if last_trade_time.tzinfo is None:
        last_trade_time = last_trade_time.replace(tzinfo=timezone.utc)

    time_since_last = now - last_trade_time
    minutes_since_last = time_since_last.total_seconds() / 60

    # Determine cooldown period based on operation type
    last_operation = last_decision.operation.lower()
    is_reversal = (
        (last_operation == "buy" and operation == "short") or
        (last_operation == "short" and operation == "buy")
    )

    if is_reversal:
        cooldown_minutes = REVERSAL_COOLDOWN_MINUTES
        cooldown_type = "reversal"
    else:
        cooldown_minutes = SAME_SYMBOL_COOLDOWN_MINUTES
        cooldown_type = "same-symbol"

    if minutes_since_last < cooldown_minutes:
        minutes_remaining = int(cooldown_minutes - minutes_since_last)
        return {
            "allowed": False,
            "reason": f"Cooldown active ({cooldown_type}): {minutes_remaining} min remaining. "
                     f"Last trade: {last_operation.upper()} at {last_trade_time.strftime('%H:%M')}",
            "last_trade_time": last_trade_time,
            "minutes_remaining": minutes_remaining,
            "last_operation": last_operation,
            "is_reversal": is_reversal
        }

    return {
        "allowed": True,
        "reason": f"Cooldown expired ({minutes_since_last:.0f} min since last trade)",
        "last_trade_time": last_trade_time,
        "minutes_remaining": 0
    }


def get_recent_trades_for_context(
    db: Session,
    account_id: int,
    limit: int = 5,
    hours: int = 24
) -> list[dict]:
    """
    Get recent trade history for AI context.

    Returns last N trades within the past X hours to provide
    the AI with memory of its recent decisions.

    Args:
        db: Database session
        account_id: Account ID
        limit: Maximum trades to return
        hours: Only include trades from past N hours

    Returns:
        List of trade summaries with symbol, operation, time, reason
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    stmt = (
        select(AIDecisionLog)
        .where(
            AIDecisionLog.account_id == account_id,
            AIDecisionLog.executed == True,
            AIDecisionLog.decision_time >= cutoff
        )
        .order_by(desc(AIDecisionLog.decision_time))
        .limit(limit)
    )

    result = db.execute(stmt)
    decisions = result.scalars().all()

    recent_trades = []
    for decision in decisions:
        trade_time = decision.decision_time
        if trade_time.tzinfo is None:
            trade_time = trade_time.replace(tzinfo=timezone.utc)

        minutes_ago = int((datetime.now(timezone.utc) - trade_time).total_seconds() / 60)

        recent_trades.append({
            "symbol": decision.symbol,
            "operation": decision.operation.upper(),
            "time": trade_time.strftime("%H:%M"),
            "minutes_ago": minutes_ago,
            "reason": decision.reason[:100] + "..." if len(decision.reason) > 100 else decision.reason
        })

    return recent_trades


def format_recent_trades_for_prompt(recent_trades: list[dict]) -> str:
    """
    Format recent trades as context string for AI prompt.

    Example output:
    YOUR RECENT TRADES (Last 24h):
    1. NIL SHORT @ 14:32 (28 min ago) - "Score 0.25 indicates weakness..."
    2. BTC BUY @ 13:45 (75 min ago) - "Strong momentum 0.92..."

    IMPORTANT: Consider if new data justifies changing these positions!
    """
    if not recent_trades:
        return """
YOUR RECENT TRADES: None in last 24h

You are starting fresh - make decisions based solely on current market data.
"""

    lines = [
        "",
        "=" * 60,
        "YOUR RECENT TRADES (Last 24h) - REVIEW BEFORE ACTING!",
        "=" * 60,
        ""
    ]

    for i, trade in enumerate(recent_trades, 1):
        lines.append(
            f"{i}. {trade['symbol']} {trade['operation']} @ {trade['time']} "
            f"({trade['minutes_ago']} min ago)"
        )
        lines.append(f"   Reason: {trade['reason']}")
        lines.append("")

    lines.extend([
        "ANTI-FLIP-FLOP RULES:",
        "- Do NOT reverse a position within 60 minutes unless MAJOR news event",
        "- Do NOT re-enter same symbol within 30 minutes of exit",
        "- If you just traded a symbol, HOLD unless data has SIGNIFICANTLY changed",
        "- Ask yourself: 'What NEW information justifies changing this position?'",
        "",
        "=" * 60,
    ])

    return "\n".join(lines)


def get_symbol_trade_count_last_hours(
    db: Session,
    account_id: int,
    symbol: str,
    hours: int = 2
) -> int:
    """
    Count how many times a symbol was traded in last N hours.

    Used to detect over-trading on single symbol.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    stmt = (
        select(AIDecisionLog)
        .where(
            AIDecisionLog.account_id == account_id,
            AIDecisionLog.symbol == symbol,
            AIDecisionLog.executed == True,
            AIDecisionLog.decision_time >= cutoff
        )
    )

    result = db.execute(stmt)
    return len(result.scalars().all())
