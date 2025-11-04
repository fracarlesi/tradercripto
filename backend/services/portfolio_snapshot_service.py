"""
Portfolio Snapshot Service - Periodic snapshots from Hyperliquid

SIMPLE APPROACH: Save Hyperliquid user_state() periodically to database.
No complex P&L reconstruction - just store real data from exchange.
"""

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from database.models import Account, PortfolioSnapshot
from services.trading.hyperliquid_trading_service import hyperliquid_trading_service

logger = logging.getLogger(__name__)


async def capture_portfolio_snapshot_async(db: Session, account_id: int) -> PortfolioSnapshot | None:
    """
    Capture current portfolio snapshot from Hyperliquid and save to database.

    Args:
        db: Database session
        account_id: Account ID to capture snapshot for

    Returns:
        Saved snapshot or None if failed
    """
    try:
        # Fetch current state from Hyperliquid
        user_state = await hyperliquid_trading_service.get_user_state_async()

        if not user_state or 'marginSummary' not in user_state:
            logger.error(f"Failed to fetch user state for account {account_id}")
            return None

        margin = user_state['marginSummary']

        # Extract snapshot data
        total_assets = Decimal(str(margin.get('accountValue', '0')))
        total_raw_usd = Decimal(str(margin.get('totalRawUsd', '0')))
        total_margin_used = Decimal(str(margin.get('totalMarginUsed', '0')))
        withdrawable = Decimal(str(margin.get('withdrawable', '0')))

        # Create snapshot
        snapshot = PortfolioSnapshot(
            account_id=account_id,
            total_assets=total_assets,
            total_raw_usd=total_raw_usd,
            total_margin_used=total_margin_used,
            withdrawable=withdrawable,
            snapshot_time=datetime.now(UTC),
        )

        db.add(snapshot)
        db.commit()
        db.refresh(snapshot)

        logger.info(
            f"Captured portfolio snapshot for account {account_id}: "
            f"${float(total_assets):.2f} total assets"
        )

        return snapshot

    except Exception as e:
        logger.error(f"Failed to capture snapshot for account {account_id}: {e}", exc_info=True)
        db.rollback()
        return None


async def capture_all_accounts_snapshots_async(db: Session) -> int:
    """
    Capture snapshots for all active accounts.

    Args:
        db: Database session

    Returns:
        Number of snapshots captured
    """
    try:
        # Get all active accounts
        accounts = db.query(Account).filter(Account.is_active == True).all()

        if not accounts:
            logger.warning("No active accounts found for snapshot capture")
            return 0

        captured_count = 0
        for account in accounts:
            snapshot = await capture_portfolio_snapshot_async(db, account.id)
            if snapshot:
                captured_count += 1

        logger.info(f"Captured {captured_count}/{len(accounts)} portfolio snapshots")
        return captured_count

    except Exception as e:
        logger.error(f"Failed to capture all account snapshots: {e}", exc_info=True)
        return 0


def cleanup_old_snapshots(db: Session, retention_days: int = 30) -> int:
    """
    Delete snapshots older than retention period.

    Args:
        db: Database session
        retention_days: Number of days to keep snapshots (default: 30)

    Returns:
        Number of snapshots deleted
    """
    try:
        cutoff_date = datetime.now(UTC) - timedelta(days=retention_days)

        deleted_count = (
            db.query(PortfolioSnapshot)
            .filter(PortfolioSnapshot.snapshot_time < cutoff_date)
            .delete()
        )

        db.commit()

        if deleted_count > 0:
            logger.info(f"Deleted {deleted_count} old portfolio snapshots (older than {retention_days} days)")

        return deleted_count

    except Exception as e:
        logger.error(f"Failed to cleanup old snapshots: {e}", exc_info=True)
        db.rollback()
        return 0


def get_snapshots_for_chart(
    db: Session, account_id: int, start_time: datetime, end_time: datetime
) -> list[dict]:
    """
    Get portfolio snapshots for chart display.

    Args:
        db: Database session
        account_id: Account ID
        start_time: Start time for snapshots
        end_time: End time for snapshots

    Returns:
        List of snapshot data dictionaries
    """
    try:
        snapshots = (
            db.query(PortfolioSnapshot)
            .filter(
                PortfolioSnapshot.account_id == account_id,
                PortfolioSnapshot.snapshot_time >= start_time,
                PortfolioSnapshot.snapshot_time <= end_time,
            )
            .order_by(PortfolioSnapshot.snapshot_time.asc())
            .all()
        )

        return [
            {
                "timestamp": int(snapshot.snapshot_time.timestamp()),
                "datetime_str": snapshot.snapshot_time.isoformat(),
                "account_id": snapshot.account_id,
                "user_id": snapshot.account.user_id,
                "username": snapshot.account.name,
                "total_assets": float(snapshot.total_assets),
                "cash": float(snapshot.withdrawable),  # Use withdrawable as "cash"
                "positions_value": float(
                    snapshot.total_assets - snapshot.withdrawable
                ),  # Approximate
            }
            for snapshot in snapshots
        ]

    except Exception as e:
        logger.error(f"Failed to get snapshots for chart: {e}", exc_info=True)
        return []
