"""
DATABASE CLEANUP SCRIPT: Reset to Hyperliquid Ground Truth

This script:
1. Verifies current state on Hyperliquid (source of truth)
2. Cleans up all stale trades and positions from local database
3. Resets account balance to match Hyperliquid exactly
4. Removes AI decision logs (fresh start)
5. Keeps only account configuration (API keys, settings)

Why needed:
- Local database can have simulation data mixed with real data
- Trades from manual Hyperliquid operations not in DB
- Prevents AI from making decisions on stale/fake data

Usage:
    cd backend
    source .venv/bin/activate
    python -m scripts.clean_database
"""

import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from database.connection import SessionLocal
from database.models import Account, Position, Trade, Order, AIDecisionLog
from services.trading.hyperliquid_trading_service import hyperliquid_trading_service

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def verify_hyperliquid_state():
    """Get ground truth from Hyperliquid"""
    logger.info("=" * 80)
    logger.info("STEP 1: Fetching Ground Truth from Hyperliquid")
    logger.info("=" * 80)

    try:
        user_state = await hyperliquid_trading_service.get_user_state_async()

        if not user_state:
            logger.error("❌ Failed to fetch Hyperliquid state")
            return None

        margin = user_state.get('marginSummary', {})
        positions = user_state.get('assetPositions', [])

        account_value = float(margin.get('accountValue', '0'))
        withdrawable = float(margin.get('withdrawable', '0'))

        logger.info(f"✅ Hyperliquid Account State:")
        logger.info(f"   Account Value: ${account_value:,.2f}")
        logger.info(f"   Withdrawable: ${withdrawable:,.2f}")
        logger.info(f"   Open Positions: {len(positions)}")

        if len(positions) > 0:
            logger.warning(f"⚠️  WARNING: {len(positions)} positions still open on Hyperliquid!")
            for asset_pos in positions:
                pos = asset_pos.get('position', {})
                coin = pos.get('coin', 'UNKNOWN')
                size = float(pos.get('szi', '0'))
                logger.warning(f"      - {coin}: {size:,.4f}")
            logger.warning("   Run emergency_liquidate.py first to close all positions!")
            return None

        return {
            'account_value': account_value,
            'withdrawable': withdrawable,
            'positions': positions
        }

    except Exception as e:
        logger.error(f"❌ Failed to fetch Hyperliquid state: {e}", exc_info=True)
        return None


def clean_database(hyperliquid_state):
    """Clean up local database and reset to Hyperliquid truth"""
    logger.info("\n" + "=" * 80)
    logger.info("STEP 2: Cleaning Local Database")
    logger.info("=" * 80)

    db = SessionLocal()
    try:
        # Get AI trading account
        account = db.query(Account).filter(
            Account.account_type == "AI",
            Account.is_active == True
        ).first()

        if not account:
            logger.error("❌ No AI trading account found in database")
            return False

        logger.info(f"📋 Account: {account.name} (id={account.id})")

        # Count existing records
        trade_count = db.query(Trade).filter(Trade.account_id == account.id).count()
        position_count = db.query(Position).filter(Position.account_id == account.id).count()
        order_count = db.query(Order).filter(Order.account_id == account.id).count()
        decision_count = db.query(AIDecisionLog).filter(AIDecisionLog.account_id == account.id).count()

        logger.info(f"\n📊 Current Database State:")
        logger.info(f"   Trades: {trade_count}")
        logger.info(f"   Positions: {position_count}")
        logger.info(f"   Orders: {order_count}")
        logger.info(f"   AI Decisions: {decision_count}")

        # Confirm deletion
        print("\n⚠️  WARNING: This will DELETE ALL trading history from the database!")
        print("The account will be reset to current Hyperliquid balance.")
        print(f"Account will be set to: ${hyperliquid_state['account_value']:,.2f}")
        print("\nAre you sure you want to continue? (yes/no): ", end='')
        confirmation = input().strip().lower()

        if confirmation != 'yes':
            logger.info("❌ Database cleanup cancelled by user")
            return False

        # Delete all trading history
        logger.info("\n🗑️  Deleting stale data...")

        # Delete in correct order (respect foreign key constraints)
        trades_deleted = db.query(Trade).filter(Trade.account_id == account.id).delete()
        logger.info(f"   ✅ Deleted {trades_deleted} trades")

        positions_deleted = db.query(Position).filter(Position.account_id == account.id).delete()
        logger.info(f"   ✅ Deleted {positions_deleted} positions")

        orders_deleted = db.query(Order).filter(Order.account_id == account.id).delete()
        logger.info(f"   ✅ Deleted {orders_deleted} orders")

        decisions_deleted = db.query(AIDecisionLog).filter(AIDecisionLog.account_id == account.id).delete()
        logger.info(f"   ✅ Deleted {decisions_deleted} AI decision logs")

        # Reset account balance to Hyperliquid value
        account_value = hyperliquid_state['account_value']

        # Update account - set current_cash to match Hyperliquid
        # (following the "always fetch from Hyperliquid" principle from CLAUDE.md)
        logger.info(f"\n💰 Resetting account balance to Hyperliquid value: ${account_value:,.2f}")

        # Note: We're setting initial_capital for reference, but the system
        # should ALWAYS fetch real-time data from Hyperliquid API
        account.initial_capital = account_value
        account.current_cash = account_value
        account.frozen_cash = 0.0

        db.commit()

        logger.info("✅ Database cleanup completed successfully")
        logger.info("\n📊 New Database State:")
        logger.info(f"   Trades: 0")
        logger.info(f"   Positions: 0")
        logger.info(f"   Orders: 0")
        logger.info(f"   AI Decisions: 0")
        logger.info(f"   Account Balance: ${account_value:,.2f}")

        return True

    except Exception as e:
        logger.error(f"❌ Database cleanup failed: {e}", exc_info=True)
        db.rollback()
        return False
    finally:
        db.close()


def verify_cleanup():
    """Verify database is clean"""
    logger.info("\n" + "=" * 80)
    logger.info("STEP 3: Verifying Database Cleanup")
    logger.info("=" * 80)

    db = SessionLocal()
    try:
        # Get AI trading account
        account = db.query(Account).filter(
            Account.account_type == "AI",
            Account.is_active == True
        ).first()

        if not account:
            logger.error("❌ No AI trading account found")
            return False

        # Verify all tables are empty
        trade_count = db.query(Trade).filter(Trade.account_id == account.id).count()
        position_count = db.query(Position).filter(Position.account_id == account.id).count()
        order_count = db.query(Order).filter(Order.account_id == account.id).count()
        decision_count = db.query(AIDecisionLog).filter(AIDecisionLog.account_id == account.id).count()

        logger.info(f"📊 Verification Results:")
        logger.info(f"   Trades: {trade_count} (should be 0)")
        logger.info(f"   Positions: {position_count} (should be 0)")
        logger.info(f"   Orders: {order_count} (should be 0)")
        logger.info(f"   AI Decisions: {decision_count} (should be 0)")
        logger.info(f"   Account Balance: ${float(account.current_cash):,.2f}")

        if trade_count == 0 and position_count == 0 and order_count == 0 and decision_count == 0:
            logger.info("✅ Database is clean - ready for fresh start!")
            return True
        else:
            logger.error("❌ Database cleanup incomplete - some records remain")
            return False

    except Exception as e:
        logger.error(f"❌ Verification failed: {e}", exc_info=True)
        return False
    finally:
        db.close()


async def main():
    """Main cleanup procedure"""
    logger.info("\n")
    logger.info("🧹" * 40)
    logger.info("DATABASE CLEANUP - RESET TO HYPERLIQUID GROUND TRUTH")
    logger.info("This script will DELETE ALL trading history and reset to current balance")
    logger.info("🧹" * 40)
    logger.info("\n")

    # Step 1: Get Hyperliquid state
    hyperliquid_state = await verify_hyperliquid_state()
    if not hyperliquid_state:
        logger.error("❌ Cannot proceed without Hyperliquid state")
        return

    # Step 2: Clean database
    success = clean_database(hyperliquid_state)
    if not success:
        logger.error("❌ Database cleanup failed")
        return

    # Step 3: Verify cleanup
    verified = verify_cleanup()

    # Final summary
    logger.info("\n" + "=" * 80)
    if verified:
        logger.info("✅ DATABASE CLEANUP COMPLETED SUCCESSFULLY")
        logger.info("")
        logger.info("Next steps:")
        logger.info("1. Implement system fixes (technical analysis, stop-loss, etc.)")
        logger.info("2. Test in dry-run mode")
        logger.info("3. Re-enable auto-trading with clean slate")
        logger.info("")
        logger.info("⚠️  REMEMBER: System now fetches ALL data from Hyperliquid API")
        logger.info("   Database is only for logging, NOT source of truth!")
    else:
        logger.error("❌ DATABASE CLEANUP VERIFICATION FAILED")
        logger.error("Please review the logs and try again")
    logger.info("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
