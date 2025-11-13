"""
Asset Curve Calculator - SNAPSHOT-BASED SYSTEM

Simple approach: Query portfolio snapshots from database (captured every 5 minutes from Hyperliquid).
No complex P&L reconstruction - just return historical snapshots.

REPLACES: Old reconstruction algorithm that had critical bugs in P&L calculation.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from services.portfolio_snapshot_service import get_snapshots_for_chart

logger = logging.getLogger(__name__)


async def get_all_asset_curves_data_new_async(db: Session, timeframe: str = "1h") -> list[dict]:
    """
    Get asset curve data from portfolio snapshots.

    Simple algorithm:
    1. Calculate time range based on timeframe
    2. Query snapshots from database
    3. Return formatted data

    Args:
        db: Database session
        timeframe: Time period for the curve, options: "5m", "1h", "1d"

    Returns:
        List of asset curve data points with timestamp, account info, and asset values
    """
    try:
        # Calculate time range based on timeframe
        end_time = datetime.now(UTC)

        if timeframe == "5m":
            # Last 8 hours (96 snapshots at 5-min intervals)
            start_time = end_time - timedelta(hours=8)
        elif timeframe == "1h":
            # Last 1 hour (12 snapshots at 5-min intervals)
            start_time = end_time - timedelta(hours=1)
        elif timeframe == "1d":
            # Last 30 days (30 snapshots at 1-day intervals)
            start_time = end_time - timedelta(days=30)
        else:
            # Default: last 7 days
            start_time = end_time - timedelta(days=7)

        logger.info(
            f"Fetching portfolio snapshots from {start_time} to {end_time} "
            f"(timeframe: {timeframe})"
        )

        # Get snapshots for all accounts
        # Note: get_snapshots_for_chart already handles account_id filtering
        # For now, we'll get snapshots for all accounts by querying without account_id filter
        from database.models import Account, PortfolioSnapshot

        # Get all active accounts
        accounts = db.query(Account).filter(Account.is_active == True).all()

        if not accounts:
            logger.warning("No active accounts found")
            return []

        # Collect snapshots for all accounts
        all_snapshots = []
        for account in accounts:
            snapshots = get_snapshots_for_chart(
                db=db,
                account_id=account.id,
                start_time=start_time,
                end_time=end_time,
            )
            all_snapshots.extend(snapshots)

        if not all_snapshots:
            logger.warning(
                f"No snapshots found between {start_time} and {end_time}. "
                "Snapshots will accumulate as the system captures them every 5 minutes."
            )

            # Return current Hyperliquid balance as single data point
            from services.trading.hyperliquid_trading_service import hyperliquid_trading_service

            user_state = await hyperliquid_trading_service.get_user_state_async()

            if user_state and 'marginSummary' in user_state:
                margin = user_state['marginSummary']
                current_value = float(margin.get('accountValue', '0'))
                withdrawable = float(margin.get('withdrawable', '0'))

                now = datetime.now(UTC)
                return [
                    {
                        "timestamp": int(now.timestamp()),
                        "datetime_str": now.isoformat(),
                        "account_id": account.id,
                        "user_id": account.user_id,
                        "username": account.name,
                        "total_assets": current_value,
                        "cash": withdrawable,
                        "positions_value": current_value - withdrawable,
                    }
                    for account in accounts
                ]

        # Sort by timestamp for consistent ordering
        all_snapshots.sort(key=lambda x: (x["timestamp"], x["account_id"]))

        logger.info(f"Returning {len(all_snapshots)} snapshot data points")
        return all_snapshots

    except Exception as e:
        logger.error(f"Failed to get asset curve data: {e}", exc_info=True)
        return []
