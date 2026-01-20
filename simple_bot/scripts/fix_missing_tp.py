#!/usr/bin/env python3
"""
HLQuantBot - Fix Missing Take Profit Orders
============================================

Places take profit orders for positions that are missing them.

TP calculation:
- LONG positions: TP = entry_price * (1 + tp_pct)  (above entry)
- SHORT positions: TP = entry_price * (1 - tp_pct) (below entry)

Usage:
    cd simple_bot && python scripts/fix_missing_tp.py

Options:
    --dry-run    Show what would be done without placing orders
    --tp-pct     Take profit percentage (default: 3.0)
"""

import asyncio
import argparse
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

# Load environment variables
load_dotenv()

from api.hyperliquid import HyperliquidClient


async def get_existing_orders(client: HyperliquidClient) -> tuple[set[str], set[str]]:
    """
    Get symbols that have SL and/or TP orders.

    Returns:
        Tuple of (symbols_with_sl, symbols_with_tp)
    """
    sl_symbols = set()
    tp_symbols = set()

    try:
        all_orders_response = await client._run_sync(
            lambda: client._info.frontend_open_orders(client._account.address)
        )

        if all_orders_response:
            for order in all_orders_response:
                order_data = order if isinstance(order, dict) else {}

                # Check for trigger orders (SL)
                order_type = order_data.get("orderType", "")
                coin = order_data.get("coin", "")
                reduce_only = order_data.get("reduceOnly", False)

                if order_type == "Stop Market":
                    sl_symbols.add(coin)
                elif order_type == "Limit" and reduce_only:
                    tp_symbols.add(coin)

    except Exception as e:
        print(f"Warning: Could not fetch existing orders: {e}")

    return sl_symbols, tp_symbols


def calculate_tp_price(entry_price: float, side: str, tp_pct: float) -> float:
    """
    Calculate take profit price based on position side.

    Args:
        entry_price: Position entry price
        side: "long" or "short"
        tp_pct: Take profit percentage (e.g., 3.0 for 3%)

    Returns:
        Take profit price
    """
    tp_factor = tp_pct / 100.0

    if side.lower() == "long":
        # For LONG: TP above entry price
        return entry_price * (1 + tp_factor)
    else:
        # For SHORT: TP below entry price
        return entry_price * (1 - tp_factor)


async def place_tp_for_position(
    client: HyperliquidClient,
    position: dict[str, Any],
    tp_pct: float,
    dry_run: bool = False
) -> dict[str, Any]:
    """
    Place a take profit order for a single position.

    Args:
        client: HyperliquidClient instance
        position: Position dict from get_positions()
        tp_pct: Take profit percentage
        dry_run: If True, don't actually place the order

    Returns:
        Result dict with success status and details
    """
    symbol = position["symbol"]
    side = position["side"]
    size = abs(float(position["size"]))
    entry_price = float(position["entryPrice"])
    mark_price = float(position["markPrice"])
    leverage = position.get("leverage", "?")

    # Calculate TP price
    tp_price = calculate_tp_price(entry_price, side, tp_pct)

    # For TP, we need to close the position:
    # - LONG position -> SELL to close -> is_buy=False
    # - SHORT position -> BUY to close -> is_buy=True
    is_buy = side.lower() == "short"

    result = {
        "symbol": symbol,
        "side": side.upper(),
        "size": size,
        "entry_price": entry_price,
        "mark_price": mark_price,
        "tp_price": tp_price,
        "leverage": leverage,
        "is_buy": is_buy,
        "success": False,
        "message": "",
    }

    if dry_run:
        result["success"] = True
        result["message"] = "DRY RUN - would place order"
        return result

    try:
        # Place TP as trigger order (proper take profit - shows in native TP/SL column)
        order_result = await client.place_trigger_order(
            symbol=symbol,
            is_buy=is_buy,
            size=size,
            trigger_price=tp_price,
            limit_price=None,  # Market order when triggered
            tpsl="tp",  # Take profit type - shows in native TP/SL column
            reduce_only=True,
        )

        result["success"] = order_result.get("success", False)
        result["order_id"] = order_result.get("orderId")
        result["message"] = "TP trigger order placed successfully"

    except Exception as e:
        result["success"] = False
        result["message"] = f"Failed to place TP: {str(e)}"

    return result


async def main():
    """Main function to fix missing take profit orders."""
    parser = argparse.ArgumentParser(description="Place take profit orders for positions missing them")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without placing orders")
    parser.add_argument("--tp-pct", type=float, default=3.0, help="Take profit percentage (default: 3.0)")
    args = parser.parse_args()

    print("=" * 70)
    print("HLQuantBot - Fix Missing Take Profit Orders")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    if args.dry_run:
        print("\n[DRY RUN MODE] No orders will be placed\n")

    print(f"Take Profit Percentage: {args.tp_pct}%")

    # Check environment
    testnet = os.getenv("HYPERLIQUID_TESTNET", "false").lower() == "true"
    network = "TESTNET" if testnet else "MAINNET"
    print(f"Network: {network}")

    # Initialize client
    client = HyperliquidClient(testnet=testnet)

    try:
        await client.connect()
        print(f"Wallet: {client.address}")

        # Get all positions
        print("\n" + "-" * 70)
        print("1. FETCHING CURRENT POSITIONS")
        print("-" * 70)

        positions = await client.get_positions()

        if not positions:
            print("No open positions found. Nothing to do.")
            return

        print(f"Found {len(positions)} open positions")

        # Get existing orders
        print("\n" + "-" * 70)
        print("2. CHECKING EXISTING ORDERS")
        print("-" * 70)

        sl_symbols, tp_symbols = await get_existing_orders(client)

        # Find positions without TP
        positions_without_tp = []
        positions_with_tp = []

        for pos in positions:
            symbol = pos["symbol"]

            if symbol in tp_symbols:
                positions_with_tp.append(pos)
            else:
                positions_without_tp.append(pos)

        print(f"Positions WITH TP:    {len(positions_with_tp)}")
        print(f"Positions WITHOUT TP: {len(positions_without_tp)}")

        if not positions_without_tp:
            print("\nAll positions already have take profit orders. Nothing to do.")
            return

        # Show positions that need TP
        print("\n" + "-" * 70)
        print("3. POSITIONS NEEDING TAKE PROFIT ORDERS")
        print("-" * 70)

        for pos in positions_without_tp:
            tp_price = calculate_tp_price(
                float(pos["entryPrice"]),
                pos["side"],
                args.tp_pct
            )
            print(f"\n  [{pos['symbol']}] {pos['side'].upper()}")
            print(f"    Entry:  ${float(pos['entryPrice']):.4f}")
            print(f"    Mark:   ${float(pos['markPrice']):.4f}")
            print(f"    Size:   {float(pos['size']):.6f}")
            print(f"    Lever:  {pos.get('leverage', '?')}x")
            print(f"    TP ->   ${tp_price:.4f} ({args.tp_pct}% from entry)")

        # Place TP orders
        print("\n" + "-" * 70)
        print("4. PLACING TAKE PROFIT ORDERS")
        print("-" * 70)

        success_count = 0
        fail_count = 0
        results = []

        for pos in positions_without_tp:
            result = await place_tp_for_position(
                client=client,
                position=pos,
                tp_pct=args.tp_pct,
                dry_run=args.dry_run,
            )
            results.append(result)

            status = "[OK]" if result["success"] else "[FAILED]"

            print(f"\n  {status} {result['symbol']} {result['side']}")
            print(f"    TP Price: ${result['tp_price']:.4f}")
            print(f"    Message:  {result['message']}")
            if result.get("order_id"):
                print(f"    Order ID: {result['order_id']}")

            if result["success"]:
                success_count += 1
            else:
                fail_count += 1

            # Small delay between orders to avoid rate limiting
            if not args.dry_run:
                await asyncio.sleep(0.2)

        # Summary
        print("\n" + "=" * 70)
        print("5. SUMMARY")
        print("=" * 70)

        print(f"\n  Total positions processed: {len(positions_without_tp)}")
        print(f"  Successful:                {success_count}")
        print(f"  Failed:                    {fail_count}")

        if fail_count > 0:
            print("\n  [ATTENTION] Some orders failed. Review the output above.")
        elif args.dry_run:
            print("\n  [DRY RUN] No orders were placed.")
            print("  Run without --dry-run to actually place the orders.")
        else:
            print("\n  [SUCCESS] All take profit orders placed successfully!")

        print("\n" + "=" * 70)
        print("Script Complete")
        print("=" * 70)

    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
