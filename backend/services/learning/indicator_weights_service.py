"""
Service for managing indicator weights and their application to accounts.

This service handles:
- Applying suggested weights from DeepSeek self-analysis
- Tracking weight changes in history table
- Auto-applying weights once per day (configurable)
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, Optional

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import settings
from database.connection import async_session_factory
from database.models import Account, IndicatorWeightsHistory

logger = logging.getLogger(__name__)


async def apply_indicator_weights(
    account_id: int,
    suggested_weights: Dict[str, float],
    source: str = "manual",
    session: Optional[AsyncSession] = None,
) -> Dict[str, float]:
    """
    Apply suggested indicator weights to an account.

    Args:
        account_id: The account ID to update
        suggested_weights: Dictionary of indicator names to weights (0.0-1.0)
        source: Source of the weights ("manual", "auto_daily", "self_analysis")
        session: Optional database session (creates new if not provided)

    Returns:
        Dictionary of applied weights

    Raises:
        ValueError: If weights are invalid or account not found
    """
    # Validate weights
    if not suggested_weights:
        raise ValueError("No weights provided")

    for indicator, weight in suggested_weights.items():
        if not isinstance(weight, (int, float)):
            raise ValueError(f"Invalid weight type for {indicator}: {type(weight)}")
        if not 0.0 <= weight <= 1.0:
            raise ValueError(f"Weight for {indicator} must be between 0.0 and 1.0, got {weight}")

    # Use provided session or create new one
    close_session = False
    if session is None:
        session = async_session_factory()
        close_session = True

    try:
        # Get account
        result = await session.execute(
            select(Account).where(Account.id == account_id)
        )
        account = result.scalar_one_or_none()

        if not account:
            raise ValueError(f"Account {account_id} not found")

        # Get current weights for comparison
        old_weights = account.indicator_weights or {}

        # Update account weights
        account.indicator_weights = suggested_weights

        # Create history entry
        history_entry = IndicatorWeightsHistory(
            account_id=account_id,
            old_weights=old_weights if old_weights else None,
            new_weights=suggested_weights,
            source=source,
            applied_at=datetime.utcnow(),
        )
        session.add(history_entry)

        await session.commit()

        logger.info(
            f"✅ Applied indicator weights to account {account_id}",
            extra={
                "context": {
                    "account_id": account_id,
                    "weights": suggested_weights,
                    "source": source,
                    "old_weights": old_weights,
                }
            },
        )

        return suggested_weights

    except Exception as e:
        await session.rollback()
        logger.error(
            f"Failed to apply indicator weights to account {account_id}",
            extra={
                "context": {
                    "account_id": account_id,
                    "error": str(e),
                }
            },
            exc_info=True,
        )
        raise

    finally:
        if close_session:
            await session.close()


async def should_auto_apply_today(account_id: int) -> bool:
    """
    Check if weights should be auto-applied today.

    Auto-apply happens once per day maximum, controlled by AUTO_APPLY_WEIGHTS env var.

    Args:
        account_id: The account ID to check

    Returns:
        True if weights should be auto-applied, False otherwise
    """
    # Check if auto-apply is enabled
    if not settings.AUTO_APPLY_WEIGHTS:
        logger.debug(
            f"Auto-apply disabled for account {account_id} (AUTO_APPLY_WEIGHTS=false)"
        )
        return False

    async with async_session_factory() as session:
        # Get last auto-apply from history
        result = await session.execute(
            select(IndicatorWeightsHistory)
            .where(
                and_(
                    IndicatorWeightsHistory.account_id == account_id,
                    IndicatorWeightsHistory.source == "auto_daily",
                )
            )
            .order_by(IndicatorWeightsHistory.applied_at.desc())
            .limit(1)
        )
        last_auto_apply = result.scalar_one_or_none()

        # If never auto-applied, allow it
        if not last_auto_apply:
            logger.info(
                f"Auto-apply allowed for account {account_id} (never applied before)"
            )
            return True

        # Check if last auto-apply was more than 24 hours ago
        hours_since_last = (datetime.utcnow() - last_auto_apply.applied_at).total_seconds() / 3600

        if hours_since_last >= 24:
            logger.info(
                f"Auto-apply allowed for account {account_id} ({hours_since_last:.1f}h since last apply)"
            )
            return True
        else:
            logger.debug(
                f"Auto-apply skipped for account {account_id} ({hours_since_last:.1f}h since last apply, need 24h)"
            )
            return False


async def get_weight_history(
    account_id: int,
    limit: int = 50,
) -> list[IndicatorWeightsHistory]:
    """
    Get weight change history for an account.

    Args:
        account_id: The account ID
        limit: Maximum number of history entries to return

    Returns:
        List of IndicatorWeightsHistory entries, newest first
    """
    async with async_session_factory() as session:
        result = await session.execute(
            select(IndicatorWeightsHistory)
            .where(IndicatorWeightsHistory.account_id == account_id)
            .order_by(IndicatorWeightsHistory.applied_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


async def get_current_weights(account_id: int) -> Optional[Dict[str, float]]:
    """
    Get current indicator weights for an account.

    Args:
        account_id: The account ID

    Returns:
        Dictionary of indicator weights, or None if not set
    """
    async with async_session_factory() as session:
        result = await session.execute(
            select(Account.indicator_weights).where(Account.id == account_id)
        )
        weights = result.scalar_one_or_none()
        return weights if weights else None
