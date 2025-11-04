"""
EMERGENCY SCRIPT: Stop Auto-Trading and Liquidate All Positions

This script:
1. Stops the auto-trading scheduler
2. Fetches all open positions from Hyperliquid
3. Closes all positions at market price
4. Calculates total P&L
5. Logs all actions for audit

Run this BEFORE making any system changes to prevent further losses.

Usage:
    cd backend
    python -m scripts.emergency_liquidate
"""

import asyncio
import logging
import sys
from decimal import Decimal
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from database.connection import SessionLocal
from database.models import Account
from services.scheduler import stop_scheduler, task_scheduler
from services.trading.hyperliquid_trading_service import hyperliquid_trading_service

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def stop_auto_trading():
    """Stop the auto-trading scheduler"""
    logger.info("=" * 80)
    logger.info("STEP 1: Stopping Auto-Trading Scheduler")
    logger.info("=" * 80)

    try:
        # Stop the global scheduler
        stop_scheduler()
        logger.info("✅ Auto-trading scheduler stopped successfully")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to stop scheduler: {e}")
        return False


async def get_account_state():
    """Get current account state from Hyperliquid"""
    logger.info("\n" + "=" * 80)
    logger.info("STEP 2: Fetching Current Account State from Hyperliquid")
    logger.info("=" * 80)

    try:
        user_state = await hyperliquid_trading_service.get_user_state_async()

        if not user_state:
            logger.error("❌ Failed to fetch user state from Hyperliquid")
            return None

        margin = user_state.get('marginSummary', {})
        positions = user_state.get('assetPositions', [])

        account_value = float(margin.get('accountValue', '0'))
        total_margin_used = float(margin.get('totalMarginUsed', '0'))

        logger.info(f"📊 Account Value: ${account_value:,.2f}")
        logger.info(f"💰 Margin Used: ${total_margin_used:,.2f}")
        logger.info(f"📈 Open Positions: {len(positions)}")

        # Log each position
        for i, asset_pos in enumerate(positions, 1):
            pos = asset_pos.get('position', {})
            coin = pos.get('coin', 'UNKNOWN')
            size = float(pos.get('szi', '0'))
            entry_px = float(pos.get('entryPx', '0'))
            position_value = abs(size * entry_px)
            unrealized_pnl = float(pos.get('unrealizedPnl', '0'))

            logger.info(f"\n  Position {i}: {coin}")
            logger.info(f"    Size: {size:,.4f}")
            logger.info(f"    Entry Price: ${entry_px:,.2f}")
            logger.info(f"    Position Value: ${position_value:,.2f}")
            logger.info(f"    Unrealized P&L: ${unrealized_pnl:,.2f}")

        return user_state

    except Exception as e:
        logger.error(f"❌ Failed to get account state: {e}")
        return None


async def liquidate_all_positions(user_state):
    """Close all open positions at market price"""
    logger.info("\n" + "=" * 80)
    logger.info("STEP 3: Liquidating All Open Positions")
    logger.info("=" * 80)

    if not user_state:
        logger.error("❌ No user state provided, cannot liquidate")
        return []

    positions = user_state.get('assetPositions', [])

    if not positions:
        logger.info("✅ No open positions to liquidate")
        return []

    results = []

    for i, asset_pos in enumerate(positions, 1):
        pos = asset_pos.get('position', {})
        coin = pos.get('coin', 'UNKNOWN')
        size = float(pos.get('szi', '0'))

        if size == 0:
            logger.info(f"⏭️  Skipping {coin} (zero size)")
            continue

        logger.info(f"\n🔄 Liquidating position {i}/{len(positions)}: {coin}")
        logger.info(f"   Size: {size:,.4f}")

        try:
            # Determine if this is a long or short position
            is_long = size > 0
            close_size = abs(size)

            # For long positions, we SELL to close
            # For short positions, we BUY to close
            is_buy = not is_long

            logger.info(f"   Action: {'BUY' if is_buy else 'SELL'} {close_size:,.4f} {coin} (close position)")

            # Execute market order to close position
            result = await hyperliquid_trading_service.place_market_order_async(
                symbol=coin,
                is_buy=is_buy,
                size=close_size,
                reduce_only=True  # CRITICAL: Only close existing position, don't open opposite
            )

            if result.get('status') == 'ok':
                logger.info(f"   ✅ Position closed successfully")
                results.append({
                    'symbol': coin,
                    'size': size,
                    'status': 'success',
                    'result': result
                })
            else:
                logger.error(f"   ❌ Failed to close position: {result.get('message', 'Unknown error')}")
                results.append({
                    'symbol': coin,
                    'size': size,
                    'status': 'failed',
                    'result': result
                })

            # Wait a bit between orders to avoid rate limiting
            await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"   ❌ Exception while closing {coin}: {e}")
            results.append({
                'symbol': coin,
                'size': size,
                'status': 'error',
                'error': str(e)
            })

    return results


async def verify_liquidation():
    """Verify all positions are closed"""
    logger.info("\n" + "=" * 80)
    logger.info("STEP 4: Verifying Liquidation Complete")
    logger.info("=" * 80)

    await asyncio.sleep(2)  # Wait for orders to settle

    try:
        user_state = await hyperliquid_trading_service.get_user_state_async()

        if not user_state:
            logger.error("❌ Failed to verify liquidation")
            return False

        positions = user_state.get('assetPositions', [])
        margin = user_state.get('marginSummary', {})

        final_account_value = float(margin.get('accountValue', '0'))

        if len(positions) == 0:
            logger.info("✅ LIQUIDATION COMPLETE - All positions closed")
            logger.info(f"💵 Final Account Value: ${final_account_value:,.2f}")
            return True
        else:
            logger.warning(f"⚠️  WARNING: {len(positions)} positions still open")
            for asset_pos in positions:
                pos = asset_pos.get('position', {})
                coin = pos.get('coin', 'UNKNOWN')
                size = float(pos.get('szi', '0'))
                logger.warning(f"   - {coin}: {size:,.4f}")
            return False

    except Exception as e:
        logger.error(f"❌ Failed to verify liquidation: {e}")
        return False


async def calculate_total_pnl():
    """Calculate total realized P&L from database"""
    logger.info("\n" + "=" * 80)
    logger.info("STEP 5: Calculating Total P&L")
    logger.info("=" * 80)

    db = SessionLocal()
    try:
        from database.models import Trade
        from datetime import datetime, timezone

        # Get AI trading account
        account = db.query(Account).filter(
            Account.account_type == "AI",
            Account.is_active == True
        ).first()

        if not account:
            logger.warning("⚠️  No AI trading account found")
            return

        # Get all trades
        trades = db.query(Trade).filter(
            Trade.account_id == account.id
        ).order_by(Trade.trade_time.asc()).all()

        if not trades:
            logger.info("ℹ️  No trades found in database")
            return

        # Calculate P&L
        total_buy_value = Decimal('0')
        total_sell_value = Decimal('0')
        total_commission = Decimal('0')

        for trade in trades:
            trade_value = Decimal(str(trade.price)) * Decimal(str(trade.quantity))
            commission = Decimal(str(trade.commission))

            if trade.side == 'BUY':
                total_buy_value += trade_value + commission
            else:  # SELL
                total_sell_value += trade_value - commission

            total_commission += commission

        realized_pnl = total_sell_value - total_buy_value

        logger.info(f"📊 Trading Statistics:")
        logger.info(f"   Total Trades: {len(trades)}")
        logger.info(f"   Total Buy Value: ${float(total_buy_value):,.2f}")
        logger.info(f"   Total Sell Value: ${float(total_sell_value):,.2f}")
        logger.info(f"   Total Commission: ${float(total_commission):,.2f}")
        logger.info(f"   Realized P&L: ${float(realized_pnl):,.2f}")

        if realized_pnl > 0:
            logger.info(f"   💚 PROFIT: +{float(realized_pnl):,.2f}")
        else:
            logger.info(f"   💔 LOSS: {float(realized_pnl):,.2f}")

    except Exception as e:
        logger.error(f"❌ Failed to calculate P&L: {e}")
    finally:
        db.close()


async def main():
    """Main emergency liquidation procedure"""
    logger.info("\n")
    logger.info("🚨" * 40)
    logger.info("EMERGENCY LIQUIDATION PROCEDURE")
    logger.info("This script will STOP auto-trading and CLOSE ALL POSITIONS")
    logger.info("🚨" * 40)
    logger.info("\n")

    # Confirm with user
    print("\n⚠️  WARNING: This will close ALL open positions immediately!")
    print("Are you sure you want to continue? (yes/no): ", end='')
    confirmation = input().strip().lower()

    if confirmation != 'yes':
        logger.info("❌ Liquidation cancelled by user")
        return

    # Execute emergency procedure
    success = True

    # Step 1: Stop auto-trading
    if not await stop_auto_trading():
        success = False

    # Step 2: Get account state
    user_state = await get_account_state()
    if not user_state:
        success = False

    # Step 3: Liquidate all positions
    if success and user_state:
        results = await liquidate_all_positions(user_state)
        logger.info(f"\n📊 Liquidation Summary:")
        logger.info(f"   Successful: {sum(1 for r in results if r['status'] == 'success')}")
        logger.info(f"   Failed: {sum(1 for r in results if r['status'] in ['failed', 'error'])}")

    # Step 4: Verify liquidation
    if success:
        success = await verify_liquidation()

    # Step 5: Calculate P&L
    await calculate_total_pnl()

    # Final summary
    logger.info("\n" + "=" * 80)
    if success:
        logger.info("✅ EMERGENCY LIQUIDATION COMPLETED SUCCESSFULLY")
        logger.info("You can now proceed with system fixes safely")
    else:
        logger.error("❌ EMERGENCY LIQUIDATION ENCOUNTERED ERRORS")
        logger.error("Please review the logs and manually verify positions")
    logger.info("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
