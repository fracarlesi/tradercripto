#!/usr/bin/env python3
"""
HLQuantBot Position Verification Script
========================================

Queries Hyperliquid exchange directly to verify:
1. All open positions
2. Open orders (limit orders, trigger orders)
3. Position/order alignment

Usage:
    cd simple_bot && python scripts/verify_positions.py
"""

import asyncio
import os
import sys
from datetime import datetime, timezone
from typing import Any

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

# Load environment variables
load_dotenv()

from api.hyperliquid import HyperliquidClient


async def get_trigger_orders(client: HyperliquidClient) -> list[dict[str, Any]]:
    """
    Get trigger orders (TP/SL) from Hyperliquid.

    The standard get_open_orders only returns limit orders.
    Trigger orders are fetched separately.
    """
    try:
        # Use the SDK's info client directly for trigger orders
        async def fetch():
            return client._info.query_order_by_user(client._account.address)

        result = await client._run_sync(
            lambda: client._info.query_order_by_user(client._account.address)
        )
        return result if result else []
    except Exception as e:
        print(f"Warning: Could not fetch trigger orders: {e}")
        return []


async def get_all_orders_raw(client: HyperliquidClient) -> dict[str, Any]:
    """Get raw order data from user_state for analysis."""
    try:
        result = await client._run_sync(
            lambda: client._info.user_state(client._account.address)
        )
        return result
    except Exception as e:
        print(f"Warning: Could not fetch user state: {e}")
        return {}


async def main():
    """Main verification function."""
    print("=" * 70)
    print("HLQuantBot Position Verification Report")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    # Check environment
    testnet = os.getenv("HYPERLIQUID_TESTNET", "false").lower() == "true"
    network = "TESTNET" if testnet else "MAINNET"
    print(f"\nNetwork: {network}")

    # Initialize client
    client = HyperliquidClient(testnet=testnet)

    try:
        await client.connect()
        print(f"Wallet: {client.address}")

        # Get account state
        print("\n" + "-" * 70)
        print("1. ACCOUNT STATE")
        print("-" * 70)

        account = await client.get_account_state()
        print(f"  Equity:           ${account['equity']:.2f}")
        print(f"  Available:        ${account['availableBalance']:.2f}")
        print(f"  Margin Used:      ${account['marginUsed']:.2f}")
        print(f"  Unrealized PnL:   ${account['unrealizedPnl']:.2f}")

        # Get positions
        print("\n" + "-" * 70)
        print("2. OPEN POSITIONS")
        print("-" * 70)

        positions = await client.get_positions()

        if not positions:
            print("  No open positions")
        else:
            print(f"  Total Positions: {len(positions)}")
            print()
            for pos in positions:
                print(f"  [{pos['symbol']}] {pos['side'].upper()}")
                print(f"    Size:         {pos['size']:.6f}")
                print(f"    Entry Price:  ${pos['entryPrice']:.4f}")
                print(f"    Mark Price:   ${pos['markPrice']:.4f}")
                print(f"    Unrealized:   ${pos['unrealizedPnl']:.2f}")
                print(f"    Leverage:     {pos['leverage']}x")
                liq = pos.get('liquidationPrice')
                print(f"    Liquidation:  ${liq:.4f}" if liq else "    Liquidation:  N/A")
                print()

        # Get open orders (limit orders)
        print("-" * 70)
        print("3. OPEN LIMIT ORDERS")
        print("-" * 70)

        limit_orders = await client.get_open_orders()

        if not limit_orders:
            print("  No open limit orders")
        else:
            print(f"  Total Limit Orders: {len(limit_orders)}")
            print()
            for order in limit_orders:
                reduce_only = "[REDUCE-ONLY]" if order.get('reduceOnly') else ""
                print(f"  [{order['symbol']}] {order['side'].upper()} {order['orderType']} {reduce_only}")
                print(f"    Order ID:     {order['orderId']}")
                print(f"    Size:         {order['size']:.6f}")
                print(f"    Price:        ${order['price']:.4f}")
                print()

        # Get trigger orders (TP/SL)
        print("-" * 70)
        print("4. TRIGGER ORDERS (TP/SL)")
        print("-" * 70)

        # Get raw user state to inspect trigger orders
        user_state = await get_all_orders_raw(client)

        # Check for trigger orders in user state
        trigger_orders = []

        # Parse assetPositions for associated trigger orders
        for pos_data in user_state.get("assetPositions", []):
            position = pos_data.get("position", {})
            coin = position.get("coin")

            # Check for TP/SL orders associated with this position
            # These are typically in the "szi" (size) and other fields

        # Alternative: query open orders which includes trigger orders in Hyperliquid
        try:
            raw_orders = await client._run_sync(
                lambda: client._info.open_orders(client._account.address)
            )

            # Filter for trigger orders (they have trigger price)
            for order in raw_orders:
                order_type = order.get("orderType", "")
                if "trigger" in str(order_type).lower() or order.get("triggerCondition"):
                    trigger_orders.append(order)

            # Also check frontend open orders API for more details
            try:
                all_orders_response = await client._run_sync(
                    lambda: client._info.frontend_open_orders(client._account.address)
                )
                if all_orders_response:
                    for order in all_orders_response:
                        # Look for trigger/tpsl orders
                        order_type_info = order.get("order", {}).get("orderType", {})
                        if isinstance(order_type_info, dict) and "trigger" in order_type_info:
                            trigger_info = order_type_info.get("trigger", {})
                            trigger_orders.append({
                                "oid": order.get("order", {}).get("oid"),
                                "coin": order.get("order", {}).get("coin"),
                                "side": order.get("order", {}).get("side"),
                                "sz": order.get("order", {}).get("sz"),
                                "triggerPx": trigger_info.get("triggerPx"),
                                "tpsl": trigger_info.get("tpsl"),
                                "isMarket": trigger_info.get("isMarket"),
                            })
            except Exception as e:
                print(f"  Note: Could not fetch frontend_open_orders: {e}")

        except Exception as e:
            print(f"  Warning: Error fetching trigger orders: {e}")

        if not trigger_orders:
            print("  No trigger orders (TP/SL) found")
        else:
            print(f"  Total Trigger Orders: {len(trigger_orders)}")
            print()
            for order in trigger_orders:
                tpsl = order.get("tpsl", "unknown")
                tpsl_label = "TAKE PROFIT" if tpsl == "tp" else "STOP LOSS" if tpsl == "sl" else tpsl.upper()
                side = "BUY" if str(order.get("side", "")).upper() == "B" else "SELL"
                print(f"  [{order.get('coin')}] {tpsl_label} - {side}")
                print(f"    Order ID:      {order.get('oid')}")
                print(f"    Size:          {order.get('sz')}")
                print(f"    Trigger Price: ${float(order.get('triggerPx', 0)):.4f}")
                print(f"    Market Order:  {order.get('isMarket', False)}")
                print()

        # Verification Summary
        print("=" * 70)
        print("5. VERIFICATION SUMMARY")
        print("=" * 70)

        # Build position-order mapping
        position_symbols = {pos['symbol'] for pos in positions}

        # Find positions with TP orders
        tp_symbols = set()
        sl_symbols = set()

        for order in limit_orders:
            if order.get('reduceOnly'):
                tp_symbols.add(order['symbol'])

        for order in trigger_orders:
            coin = order.get('coin')
            tpsl = order.get('tpsl', '')
            if tpsl == 'tp':
                tp_symbols.add(coin)
            elif tpsl == 'sl':
                sl_symbols.add(coin)

        # Report
        print(f"\n  Open Positions:        {len(positions)}")
        print(f"  Positions with TP:     {len(tp_symbols & position_symbols)}")
        print(f"  Positions with SL:     {len(sl_symbols & position_symbols)}")

        # Check for unprotected positions
        unprotected_tp = position_symbols - tp_symbols
        unprotected_sl = position_symbols - sl_symbols

        if unprotected_tp or unprotected_sl:
            print("\n  [CRITICAL] UNPROTECTED POSITIONS DETECTED:")

            for symbol in unprotected_tp:
                print(f"    - {symbol}: MISSING TP ORDER")

            for symbol in unprotected_sl:
                print(f"    - {symbol}: MISSING SL ORDER")

            print("\n  ACTION REQUIRED: Place TP/SL orders for these positions!")
        else:
            if positions:
                print("\n  [OK] All positions have TP and SL orders")
            else:
                print("\n  [OK] No positions to protect")

        # Check for orphaned orders
        order_symbols_tp = tp_symbols - position_symbols
        order_symbols_sl = sl_symbols - position_symbols
        limit_order_symbols = {o['symbol'] for o in limit_orders if o.get('reduceOnly')} - position_symbols

        orphaned = order_symbols_tp | order_symbols_sl | limit_order_symbols

        if orphaned:
            print(f"\n  [WARNING] ORPHANED ORDERS (no matching position):")
            for symbol in orphaned:
                print(f"    - {symbol}")
            print("\n  Consider canceling these orders.")

        print("\n" + "=" * 70)
        print("Verification Complete")
        print("=" * 70)

    except Exception as e:
        print(f"\nError during verification: {e}")
        import traceback
        traceback.print_exc()

    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
