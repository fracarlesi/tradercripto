#!/usr/bin/env python3
"""
HLQuantBot - Fix Missing Stop Loss Orders
==========================================

Places stop loss trigger orders for ALL positions that are missing them.

SL calculation:
- LONG positions: SL = entry_price * (1 - 0.02)  (2% below entry)
- SHORT positions: SL = entry_price * (1 + 0.02) (2% above entry)

Usage:
    cd simple_bot && python scripts/fix_missing_sl.py
    
Options:
    --dry-run    Show what would be done without placing orders
    --sl-pct     Stop loss percentage (default: 2.0)
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


async def get_existing_trigger_orders(client: HyperliquidClient) -> dict[str, dict[str, Any]]:
    """
    Get existing trigger orders indexed by symbol and type.
    
    Returns:
        Dict mapping symbol -> {"tp": order, "sl": order}
    """
    trigger_orders: dict[str, dict[str, Any]] = {}
    
    try:
        all_orders_response = await client._run_sync(
            lambda: client._info.frontend_open_orders(client._account.address)
        )
        
        if all_orders_response:
            for order in all_orders_response:
                order_data = order.get("order", {})
                order_type_info = order_data.get("orderType", {})
                
                if isinstance(order_type_info, dict) and "trigger" in order_type_info:
                    trigger_info = order_type_info.get("trigger", {})
                    coin = order_data.get("coin")
                    tpsl = trigger_info.get("tpsl")
                    
                    if coin and tpsl:
                        if coin not in trigger_orders:
                            trigger_orders[coin] = {}
                        trigger_orders[coin][tpsl] = {
                            "oid": order_data.get("oid"),
                            "triggerPx": float(trigger_info.get("triggerPx", 0)),
                            "sz": order_data.get("sz"),
                            "side": order_data.get("side"),
                        }
    except Exception as e:
        print(f"Warning: Could not fetch existing trigger orders: {e}")
    
    return trigger_orders


def calculate_sl_price(entry_price: float, side: str, sl_pct: float) -> float:
    """
    Calculate stop loss price based on position side.
    
    Args:
        entry_price: Position entry price
        side: "long" or "short"
        sl_pct: Stop loss percentage (e.g., 2.0 for 2%)
    
    Returns:
        Stop loss trigger price
    """
    sl_factor = sl_pct / 100.0
    
    if side.lower() == "long":
        # For LONG: SL below entry price
        return entry_price * (1 - sl_factor)
    else:
        # For SHORT: SL above entry price
        return entry_price * (1 + sl_factor)


async def place_sl_for_position(
    client: HyperliquidClient,
    position: dict[str, Any],
    sl_pct: float,
    dry_run: bool = False
) -> dict[str, Any]:
    """
    Place a stop loss order for a single position.
    
    Args:
        client: HyperliquidClient instance
        position: Position dict from get_positions()
        sl_pct: Stop loss percentage
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
    
    # Calculate SL price
    sl_price = calculate_sl_price(entry_price, side, sl_pct)
    
    # For SL, we need to close the position:
    # - LONG position -> SELL to close -> is_buy=False
    # - SHORT position -> BUY to close -> is_buy=True
    is_buy = side.lower() == "short"
    
    result = {
        "symbol": symbol,
        "side": side.upper(),
        "size": size,
        "entry_price": entry_price,
        "mark_price": mark_price,
        "sl_price": sl_price,
        "leverage": leverage,
        "is_buy": is_buy,
        "success": False,
        "message": "",
    }
    
    # Validate SL price makes sense
    if side.lower() == "long" and sl_price >= mark_price:
        result["message"] = f"SL price {sl_price:.4f} >= mark {mark_price:.4f}, would trigger immediately"
        result["warning"] = True
    elif side.lower() == "short" and sl_price <= mark_price:
        result["message"] = f"SL price {sl_price:.4f} <= mark {mark_price:.4f}, would trigger immediately"
        result["warning"] = True
    
    if dry_run:
        result["success"] = True
        result["message"] = "DRY RUN - would place order"
        return result
    
    try:
        order_result = await client.place_trigger_order(
            symbol=symbol,
            is_buy=is_buy,
            size=size,
            trigger_price=sl_price,
            limit_price=None,  # Market order on trigger
            tpsl="sl",
            reduce_only=True,
        )
        
        result["success"] = order_result.get("success", False)
        result["order_id"] = order_result.get("orderId")
        result["message"] = "SL order placed successfully"
        
    except Exception as e:
        result["success"] = False
        result["message"] = f"Failed to place SL: {str(e)}"
    
    return result


async def main():
    """Main function to fix missing stop loss orders."""
    parser = argparse.ArgumentParser(description="Place stop loss orders for all positions")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without placing orders")
    parser.add_argument("--sl-pct", type=float, default=2.0, help="Stop loss percentage (default: 2.0)")
    args = parser.parse_args()
    
    print("=" * 70)
    print("HLQuantBot - Fix Missing Stop Loss Orders")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)
    
    if args.dry_run:
        print("\n[DRY RUN MODE] No orders will be placed\n")
    
    print(f"Stop Loss Percentage: {args.sl_pct}%")
    
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
        
        # Get existing trigger orders
        print("\n" + "-" * 70)
        print("2. CHECKING EXISTING TRIGGER ORDERS")
        print("-" * 70)
        
        existing_triggers = await get_existing_trigger_orders(client)
        
        # Find positions without SL
        positions_without_sl = []
        positions_with_sl = []
        
        for pos in positions:
            symbol = pos["symbol"]
            triggers = existing_triggers.get(symbol, {})
            
            if "sl" in triggers:
                positions_with_sl.append((pos, triggers["sl"]))
            else:
                positions_without_sl.append(pos)
        
        print(f"Positions WITH SL:    {len(positions_with_sl)}")
        print(f"Positions WITHOUT SL: {len(positions_without_sl)}")
        
        if not positions_without_sl:
            print("\nAll positions already have stop loss orders. Nothing to do.")
            return
        
        # Show positions that need SL
        print("\n" + "-" * 70)
        print("3. POSITIONS NEEDING STOP LOSS ORDERS")
        print("-" * 70)
        
        for pos in positions_without_sl:
            sl_price = calculate_sl_price(
                float(pos["entryPrice"]), 
                pos["side"], 
                args.sl_pct
            )
            print(f"\n  [{pos['symbol']}] {pos['side'].upper()}")
            print(f"    Entry:  ${float(pos['entryPrice']):.4f}")
            print(f"    Mark:   ${float(pos['markPrice']):.4f}")
            print(f"    Size:   {float(pos['size']):.6f}")
            print(f"    Lever:  {pos.get('leverage', '?')}x")
            print(f"    SL ->   ${sl_price:.4f} ({args.sl_pct}% from entry)")
        
        # Place SL orders
        print("\n" + "-" * 70)
        print("4. PLACING STOP LOSS ORDERS")
        print("-" * 70)
        
        success_count = 0
        fail_count = 0
        results = []
        
        for pos in positions_without_sl:
            result = await place_sl_for_position(
                client=client,
                position=pos,
                sl_pct=args.sl_pct,
                dry_run=args.dry_run,
            )
            results.append(result)
            
            status = "[OK]" if result["success"] else "[FAILED]"
            if result.get("warning"):
                status = "[WARN]"
            
            print(f"\n  {status} {result['symbol']} {result['side']}")
            print(f"    SL Price: ${result['sl_price']:.4f}")
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
        
        print(f"\n  Total positions processed: {len(positions_without_sl)}")
        print(f"  Successful:                {success_count}")
        print(f"  Failed:                    {fail_count}")
        
        if fail_count > 0:
            print("\n  [ATTENTION] Some orders failed. Review the output above.")
            print("  You may need to manually place SL orders for failed positions.")
        elif args.dry_run:
            print("\n  [DRY RUN] No orders were placed.")
            print("  Run without --dry-run to actually place the orders.")
        else:
            print("\n  [SUCCESS] All stop loss orders placed successfully!")
        
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
