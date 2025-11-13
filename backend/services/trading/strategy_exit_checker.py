"""
Strategy Exit Checker - Automatic position exits based on strategy rules.

This service runs periodically (every 3 minutes) and checks all open positions
for exit criteria (stop-loss, take-profit, time-based) based on their assigned
trading strategy.
"""

import asyncio
import logging
from datetime import datetime, UTC
from typing import Dict, List

from database.connection import SessionLocal
from database.models import Account, Position
from services.trading.hyperliquid_trading_service import hyperliquid_trading_service
from services.trading.trading_strategies import should_exit_position, StrategyType

logger = logging.getLogger(__name__)


def check_strategy_exits() -> None:
    """
    Check all open positions for strategy-based exit criteria.

    This function:
    1. Fetches all open positions with strategy metadata
    2. Gets current market prices
    3. Checks each position against its strategy exit rules
    4. Executes SELL orders for positions meeting exit criteria

    Called by scheduler every 3 minutes (or configurable interval).
    """

    logger.info("="* 60)
    logger.info("🔍 Strategy Exit Checker - Scanning positions")
    logger.info("="* 60)

    db = SessionLocal()
    try:
        # Get active AI trading account
        account = (
            db.query(Account)
            .filter(Account.account_type == "AI", Account.is_active == True)
            .first()
        )

        if not account:
            logger.warning("No active AI trading account found")
            return

        # Get all open positions with strategy metadata
        positions = (
            db.query(Position)
            .filter(
                Position.account_id == account.id,
                Position.strategy_type.isnot(None),  # Only positions with strategy assigned
            )
            .all()
        )

        if not positions:
            logger.info("No open positions with strategy metadata")
            return

        logger.info(f"Found {len(positions)} positions to check")

        # Get current market prices
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            all_prices = loop.run_until_complete(
                hyperliquid_trading_service.get_all_mids_async()
            )
        finally:
            loop.close()

        # Check each position
        positions_to_exit = []

        for position in positions:
            symbol = position.symbol
            entry_price = float(position.average_cost)
            current_price = all_prices.get(symbol)

            if current_price is None:
                logger.warning(f"No current price for {symbol}, skipping")
                continue

            current_price = float(current_price)
            strategy_type = position.strategy_type

            # Check if position should exit
            should_exit, exit_reason = should_exit_position(
                entry_price=entry_price,
                current_price=current_price,
                entry_time=position.created_at,
                strategy_type=strategy_type,
                current_time=datetime.now(UTC),
            )

            if should_exit:
                pnl_pct = (current_price - entry_price) / entry_price
                positions_to_exit.append(
                    {
                        "position": position,
                        "exit_reason": exit_reason,
                        "pnl_pct": pnl_pct,
                        "current_price": current_price,
                    }
                )

        # Execute exit orders
        if not positions_to_exit:
            logger.info("✅ No positions meet exit criteria")
            return

        logger.info(f"🚨 {len(positions_to_exit)} positions meet exit criteria, executing sells...")

        for exit_data in positions_to_exit:
            _execute_strategy_exit(
                account=account,
                position=exit_data["position"],
                exit_reason=exit_data["exit_reason"],
                pnl_pct=exit_data["pnl_pct"],
                current_price=exit_data["current_price"],
            )

    except Exception as e:
        logger.error(
            "Strategy exit checker failed",
            extra={"context": {"error": str(e)}},
            exc_info=True,
        )
    finally:
        db.close()


def _execute_strategy_exit(
    account: Account,
    position: Position,
    exit_reason: str,
    pnl_pct: float,
    current_price: float,
) -> None:
    """
    Execute a strategy-based exit (SELL order).

    Args:
        account: Trading account
        position: Position to exit
        exit_reason: Reason for exit (e.g., "take_profit_MOMENTUM_BREAKOUT")
        pnl_pct: Current P&L percentage
        current_price: Current market price
    """

    symbol = position.symbol
    quantity = float(position.quantity)

    logger.info(
        f"💰 Executing strategy exit: {symbol} "
        f"(reason: {exit_reason}, P&L: {pnl_pct*100:+.2f}%, price: ${current_price:.4f})"
    )

    try:
        # Import here to avoid circular dependency
        from services.trading_commands import execute_crypto_order

        # Execute SELL order
        result = execute_crypto_order(
            account_id=account.id,
            symbol=symbol,
            operation="sell",
            target_quantity=quantity,
            leverage=1,  # Not used for sell
        )

        if result.get("status") == "success":
            logger.info(
                f"✅ Strategy exit executed successfully: {symbol} "
                f"(realized P&L: {pnl_pct*100:+.2f}%)"
            )
        else:
            logger.error(
                f"❌ Strategy exit failed for {symbol}: {result.get('message')}"
            )

    except Exception as e:
        logger.error(
            f"Failed to execute strategy exit for {symbol}: {e}",
            extra={
                "context": {
                    "account_id": account.id,
                    "symbol": symbol,
                    "exit_reason": exit_reason,
                }
            },
            exc_info=True,
        )


# Sync wrapper for scheduler
def check_strategy_exits_sync():
    """Synchronous wrapper for scheduler (APScheduler compatibility)."""
    return check_strategy_exits()
