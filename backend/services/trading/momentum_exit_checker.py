"""
Momentum Reversal Exit Checker

Monitors open positions and exits when momentum reverses.
Critical for momentum surfing strategy - exit quickly when trend changes.
"""

import logging
from typing import Dict, List
import asyncio

from services.trading.hyperliquid_trading_service import hyperliquid_trading_service
from services.market_data.hourly_momentum import get_hourly_momentum_scores
from database.connection import SessionLocal
from database.models import Position

logger = logging.getLogger(__name__)


async def check_momentum_exit_async(momentum_drop_threshold: float = 0.20) -> None:
    """Check if positions should exit due to momentum reversal.

    Momentum surfing strategy: exit when momentum drops significantly from entry.

    Args:
        momentum_drop_threshold: Exit if momentum drops by this % (default: 0.20 = -20%)

    Exit triggers:
    1. Momentum score drops >20% from entry (e.g., 0.85 → 0.68)
    2. Coin drops out of top 20 momentum (no longer trending)
    3. For LONG: momentum becomes negative
    4. For SHORT: momentum becomes positive

    Example:
        Entry: BTC momentum score 0.80 (top 5)
        Current: BTC momentum score 0.60 (dropped 25%) → EXIT
    """
    try:
        # Get current positions from Hyperliquid
        user_state = await hyperliquid_trading_service.get_user_state_async()
        positions = user_state.get('assetPositions', [])

        if not positions:
            logger.debug("No open positions to check for momentum exit")
            return

        logger.info(f"🔍 Checking {len(positions)} positions for momentum reversal")

        # Get current momentum scores for all coins
        try:
            momentum_data = get_hourly_momentum_scores(top_n=50)  # Get top 50 to detect drops
            current_momentum = {coin['symbol']: coin for coin in momentum_data}
        except Exception as e:
            logger.error(f"Failed to get momentum scores: {e}", exc_info=True)
            return

        # Check each position
        for pos in positions:
            try:
                symbol = pos['position']['coin']
                szi = float(pos['position']['szi'])
                side = 'LONG' if szi > 0 else 'SHORT'
                entry_px = float(pos['position']['entryPx'])
                unrealized_pnl = float(pos['position']['unrealizedPnl'])

                # Get current momentum for this symbol
                current_mom = current_momentum.get(symbol)

                if not current_mom:
                    # Coin not in top 50 momentum anymore → STRONG EXIT SIGNAL
                    logger.warning(
                        f"⚠️ {symbol} {side} dropped out of top 50 momentum → EXIT"
                    )
                    await _execute_momentum_exit(
                        symbol, abs(szi), side,
                        f"Dropped out of momentum leaders (was in position)"
                    )
                    continue

                # Get current momentum score and rank
                momentum_score = current_mom['momentum_score']
                rank = current_mom.get('rank', 999)

                # Exit triggers for LONG positions
                if side == 'LONG':
                    # Trigger 1: Momentum becomes negative (downtrend started)
                    if momentum_score < 0:
                        logger.info(
                            f"📉 {symbol} LONG momentum turned negative ({momentum_score:.3f}) → EXIT"
                        )
                        await _execute_momentum_exit(
                            symbol, abs(szi), side,
                            f"Momentum reversed to negative: {momentum_score:.3f}"
                        )
                        continue

                    # Trigger 2: Dropped out of top 20 (losing momentum leadership)
                    if rank > 20:
                        logger.info(
                            f"📉 {symbol} LONG dropped to rank #{rank} (out of top 20) → EXIT"
                        )
                        await _execute_momentum_exit(
                            symbol, abs(szi), side,
                            f"Dropped to rank #{rank}, momentum weakening"
                        )
                        continue

                # Exit triggers for SHORT positions
                elif side == 'SHORT':
                    # Trigger 1: Momentum becomes positive (uptrend started - bad for SHORT)
                    if momentum_score > 0:
                        logger.info(
                            f"📈 {symbol} SHORT momentum turned positive ({momentum_score:.3f}) → EXIT"
                        )
                        await _execute_momentum_exit(
                            symbol, abs(szi), side,
                            f"Momentum reversed to positive: {momentum_score:.3f}"
                        )
                        continue

                    # Trigger 2: Momentum weakening (less negative = upward pressure)
                    # For SHORT, we want strong negative momentum
                    if momentum_score > -0.02:  # Close to zero = trend exhausted
                        logger.info(
                            f"📈 {symbol} SHORT momentum weakening ({momentum_score:.3f}) → EXIT"
                        )
                        await _execute_momentum_exit(
                            symbol, abs(szi), side,
                            f"Downward momentum exhausted: {momentum_score:.3f}"
                        )
                        continue

                # If still in top 20, log status but don't exit yet
                logger.debug(
                    f"✅ {symbol} {side} still strong: rank #{rank}, "
                    f"momentum {momentum_score:.3f}, P&L ${unrealized_pnl:.2f}"
                )

            except Exception as e:
                logger.error(
                    f"Error checking momentum exit for position: {e}",
                    extra={"context": {"symbol": pos.get('position', {}).get('coin')}},
                    exc_info=True
                )
                continue

    except Exception as e:
        logger.error(f"Momentum exit check failed: {e}", exc_info=True)


async def _execute_momentum_exit(
    symbol: str,
    size: float,
    side: str,
    reason: str
) -> None:
    """Execute market order to close position due to momentum reversal.

    Args:
        symbol: Coin symbol (e.g., 'BTC')
        size: Position size to close (absolute value)
        side: 'LONG' or 'SHORT'
        reason: Exit reason for logging
    """
    try:
        logger.info(
            f"🔴 MOMENTUM EXIT: Closing {symbol} {side} position "
            f"(size: {size:.6f}) - Reason: {reason}"
        )

        # Determine order direction
        # LONG position → SELL to close
        # SHORT position → BUY to close
        is_buy = (side == 'SHORT')

        # Execute market order with reduce_only=True
        result = await hyperliquid_trading_service.place_order_async(
            symbol=symbol,
            is_buy=is_buy,
            size=size,
            limit_px=None,  # Market order
            reduce_only=True,
            order_type={'limit': {'tif': 'Ioc'}}  # Immediate or cancel
        )

        if result.get('status') == 'ok':
            logger.info(
                f"✅ Momentum exit executed: {symbol} {side} closed",
                extra={"context": {"symbol": symbol, "side": side, "reason": reason}}
            )
        else:
            logger.error(
                f"❌ Momentum exit failed: {result}",
                extra={"context": {"symbol": symbol, "side": side}}
            )

    except Exception as e:
        logger.error(
            f"Failed to execute momentum exit: {e}",
            extra={"context": {"symbol": symbol, "side": side, "reason": reason}},
            exc_info=True
        )


def check_momentum_exit_sync() -> None:
    """Synchronous wrapper for scheduler."""
    try:
        asyncio.run(check_momentum_exit_async())
    except Exception as e:
        logger.error(f"Momentum exit check (sync wrapper) failed: {e}", exc_info=True)
