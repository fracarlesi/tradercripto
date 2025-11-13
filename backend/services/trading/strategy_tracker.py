"""
Strategy Tracker - Assign and track trading strategy metadata for positions.

This module integrates with auto_trader.py to classify trading opportunities
and save strategy metadata (stop-loss, take-profit, time-based exit rules)
to Position records.
"""

import logging
from datetime import datetime, UTC
from typing import Dict, Optional

from sqlalchemy.orm import Session

from database.models import Position
from services.trading.trading_strategies import (
    classify_opportunity,
    get_strategy_rules,
    StrategyType,
)

logger = logging.getLogger(__name__)


def assign_strategy_to_position(
    db: Session,
    account_id: int,
    symbol: str,
    technical_data: Dict,
    sentiment: Optional[int] = None,
    prophet_trend: Optional[str] = None,
) -> Optional[StrategyType]:
    """
    Classify trading opportunity and assign strategy metadata to position.

    This function:
    1. Extracts technical metrics from technical_data
    2. Classifies the opportunity into a strategy type
    3. Updates the Position record with strategy metadata (stop-loss, take-profit, etc.)

    Args:
        db: Database session
        account_id: Account ID
        symbol: Trading symbol
        technical_data: Technical analysis data (from technical_factors)
        sentiment: Market sentiment (0-100, Fear & Greed Index)
        prophet_trend: Prophet forecast trend ("bullish", "bearish", "neutral")

    Returns:
        Strategy type assigned, or None if position not found
    """

    try:
        # Extract technical metrics
        technical_score = technical_data.get("technical_score", 0.0)
        momentum = technical_data.get("momentum", 0.0)
        support = technical_data.get("support", 0.0)

        # Classify opportunity
        strategy_type = classify_opportunity(
            technical_score=technical_score,
            momentum=momentum,
            support=support,
            sentiment=sentiment,
            prophet_trend=prophet_trend,
        )

        # Get strategy rules
        rules = get_strategy_rules(strategy_type)

        logger.info(
            f"📋 Strategy assigned for {symbol}: {strategy_type} "
            f"(TP={rules.take_profit_pct*100:+.1f}%, SL={rules.stop_loss_pct*100:.1f}%, "
            f"Time={rules.max_hold_minutes}min)"
        )

        # Find the position (should have just been created by order execution)
        position = (
            db.query(Position)
            .filter(
                Position.account_id == account_id,
                Position.symbol == symbol,
            )
            .first()
        )

        if not position:
            logger.warning(
                f"Position not found for {symbol} (account {account_id}), "
                "cannot assign strategy metadata"
            )
            return None

        # Update position with strategy metadata
        position.strategy_type = strategy_type
        position.take_profit_pct = rules.take_profit_pct
        position.stop_loss_pct = rules.stop_loss_pct
        position.max_hold_minutes = rules.max_hold_minutes

        db.commit()

        logger.info(f"✅ Strategy metadata saved to position {symbol}")

        return strategy_type

    except Exception as e:
        logger.error(
            f"Failed to assign strategy to position {symbol}: {e}",
            extra={"context": {"account_id": account_id, "symbol": symbol}},
            exc_info=True,
        )
        db.rollback()
        return None


def get_position_with_strategy(
    db: Session,
    account_id: int,
    symbol: str,
) -> Optional[Dict]:
    """
    Get position with strategy metadata.

    Returns dict with:
    - position: Position object
    - strategy_type: StrategyType
    - rules: StrategyRules
    """

    position = (
        db.query(Position)
        .filter(
            Position.account_id == account_id,
            Position.symbol == symbol,
        )
        .first()
    )

    if not position:
        return None

    # Return position with strategy data
    return {
        "position": position,
        "strategy_type": position.strategy_type,
        "take_profit_pct": float(position.take_profit_pct) if position.take_profit_pct else None,
        "stop_loss_pct": float(position.stop_loss_pct) if position.stop_loss_pct else None,
        "max_hold_minutes": position.max_hold_minutes,
        "created_at": position.created_at,
    }
