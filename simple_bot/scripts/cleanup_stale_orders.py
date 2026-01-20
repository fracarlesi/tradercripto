#!/usr/bin/env python3
"""
Clean up stale and duplicate orders on Hyperliquid.

This script:
1. Cancels the stale MOVE entry order (ID: 290631433221)
2. Identifies and cancels duplicate TP orders (keeping only newest)
"""

import asyncio
import os
import sys
from collections import defaultdict

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants


async def main():
    """Clean up stale and duplicate orders."""
    wallet_address = os.environ.get("HYPERLIQUID_WALLET_ADDRESS")
    private_key = os.environ.get("HYPERLIQUID_PRIVATE_KEY")

    if not wallet_address or not private_key:
        print("ERROR: Missing HYPERLIQUID_WALLET_ADDRESS or HYPERLIQUID_PRIVATE_KEY")
        return

    # Initialize clients
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    exchange = Exchange(None, constants.MAINNET_API_URL, vault_address=None, account_address=wallet_address)

    print("=" * 60)
    print("HLQuantBot Order Cleanup")
    print("=" * 60)

    # Get all open orders
    open_orders = info.frontend_open_orders(wallet_address)
    print(f"\nFound {len(open_orders)} open orders")

    # 1. Cancel stale entry order
    stale_order_id = 290631433221
    stale_found = False
    for order in open_orders:
        if order.get("oid") == stale_order_id:
            stale_found = True
            symbol = order.get("coin", "MOVE")
            print(f"\n[1] Found stale entry order: {symbol} ID={stale_order_id}")
            print(f"    Cancelling...")
            try:
                result = exchange.cancel(symbol, stale_order_id)
                print(f"    Result: {result}")
            except Exception as e:
                print(f"    Error: {e}")

    if not stale_found:
        print(f"\n[1] Stale order {stale_order_id} not found (may already be cancelled)")

    # 2. Find and cancel duplicate TP orders
    print("\n[2] Checking for duplicate TP orders...")

    # Group reduce-only orders by symbol
    orders_by_symbol = defaultdict(list)
    for order in open_orders:
        if order.get("reduceOnly", False):
            symbol = order.get("coin")
            orders_by_symbol[symbol].append(order)

    # Cancel duplicates (keep newest = highest order ID)
    cancelled = 0
    for symbol, orders in orders_by_symbol.items():
        if len(orders) > 2:  # More than 2 orders (should be just SL + TP)
            # Sort by order ID (newest first)
            sorted_orders = sorted(orders, key=lambda x: x.get("oid", 0), reverse=True)

            # Keep first 2 (newest), cancel rest
            to_cancel = sorted_orders[2:]
            print(f"\n    {symbol}: {len(orders)} orders, cancelling {len(to_cancel)} duplicates")

            for order in to_cancel:
                oid = order.get("oid")
                price = order.get("limitPx")
                side = order.get("side")
                print(f"      - Cancelling {side} @ {price} (ID: {oid})")
                try:
                    result = exchange.cancel(symbol, oid)
                    cancelled += 1
                except Exception as e:
                    print(f"        Error: {e}")

    print(f"\n" + "=" * 60)
    print(f"Cleanup complete. Cancelled {cancelled} duplicate orders.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
