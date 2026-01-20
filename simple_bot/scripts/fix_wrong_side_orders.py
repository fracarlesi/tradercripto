#!/usr/bin/env python3
"""
HLQuantBot - Fix Wrong Side Orders
==================================

CRITICAL FIX: Cancels orders that are for the wrong position side
and replaces them with correct SL/TP orders.

Problem: Orders were placed for LONG positions but positions are now SHORT.
This means SL orders would INCREASE the position instead of protecting it!

Usage:
    cd simple_bot && python scripts/fix_wrong_side_orders.py
    cd simple_bot && python scripts/fix_wrong_side_orders.py --dry-run
"""

import asyncio
import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from api.hyperliquid import HyperliquidClient


async def main():
    parser = argparse.ArgumentParser(description="Fix wrong side SL/TP orders")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    parser.add_argument("--sl-pct", type=float, default=1.5, help="Stop loss percentage")
    parser.add_argument("--tp-pct", type=float, default=3.0, help="Take profit percentage")
    args = parser.parse_args()

    print("=" * 70)
    print("HLQuantBot - Fix Wrong Side Orders")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    if args.dry_run:
        print("\n[DRY RUN MODE] No changes will be made\n")

    testnet = os.getenv("HYPERLIQUID_TESTNET", "false").lower() == "true"
    print(f"Network: {'TESTNET' if testnet else 'MAINNET'}")

    client = HyperliquidClient(testnet=testnet)

    try:
        await client.connect()
        print(f"Wallet: {client.address}")

        # Get current positions
        print("\n" + "-" * 70)
        print("1. CURRENT POSITIONS")
        print("-" * 70)

        positions = await client.get_positions()
        pos_by_symbol = {p["symbol"]: p for p in positions}

        for pos in positions:
            side = pos["side"].upper()
            pnl = pos.get("unrealizedPnl", 0)
            print(f"  {pos['symbol']}: {side} {pos['size']} @ {pos['entryPrice']:.4f} (PnL: ${pnl:.2f})")

        # Get current orders with raw data
        print("\n" + "-" * 70)
        print("2. CURRENT ORDERS (checking for wrong side)")
        print("-" * 70)

        raw_orders = await client._run_sync(
            lambda: client._info.frontend_open_orders(client._account.address)
        )

        wrong_orders = []
        correct_orders = []

        for order in raw_orders:
            coin = order.get("coin", "")
            order_type = order.get("orderType", "")
            side = order.get("side", "")  # "A" = sell, "B" = buy
            reduce_only = order.get("reduceOnly", False)
            oid = order.get("oid", "")
            trigger_px = order.get("triggerPx", "")

            if coin not in pos_by_symbol:
                continue

            pos = pos_by_symbol[coin]
            pos_side = pos["side"]  # "long" or "short"

            # For reduce-only orders:
            # - SHORT position needs BUY to close (side="B")
            # - LONG position needs SELL to close (side="A")
            expected_order_side = "B" if pos_side == "short" else "A"

            is_wrong = reduce_only and side != expected_order_side

            order_info = {
                "oid": oid,
                "coin": coin,
                "type": order_type,
                "side": "SELL" if side == "A" else "BUY",
                "trigger": trigger_px,
                "pos_side": pos_side.upper(),
                "expected": "BUY" if expected_order_side == "B" else "SELL",
            }

            if is_wrong:
                wrong_orders.append(order_info)
                print(f"  [WRONG] {coin} {order_type}: {order_info['side']} but position is {pos_side.upper()} (needs {order_info['expected']})")
            else:
                correct_orders.append(order_info)
                print(f"  [OK] {coin} {order_type}: {order_info['side']} for {pos_side.upper()} position")

        if not wrong_orders:
            print("\n  No wrong-side orders found!")
            return

        # Cancel wrong orders
        print("\n" + "-" * 70)
        print("3. CANCELLING WRONG ORDERS")
        print("-" * 70)

        symbols_to_fix = set()
        for order in wrong_orders:
            symbols_to_fix.add(order["coin"])
            print(f"\n  Cancelling {order['coin']} order {order['oid']}...")

            if not args.dry_run:
                try:
                    await client.cancel_order(order["coin"], int(order["oid"]))
                    print(f"    [OK] Cancelled")
                except Exception as e:
                    print(f"    [FAILED] {e}")
            else:
                print(f"    [DRY RUN] Would cancel")

        # Place correct orders
        print("\n" + "-" * 70)
        print("4. PLACING CORRECT SL/TP ORDERS")
        print("-" * 70)

        for symbol in symbols_to_fix:
            pos = pos_by_symbol[symbol]
            is_short = pos["side"] == "short"
            entry = float(pos["entryPrice"])
            size = float(pos["size"])

            # Calculate prices
            sl_pct = args.sl_pct / 100
            tp_pct = args.tp_pct / 100

            if is_short:
                # SHORT: SL above entry (price goes up = loss), TP below entry (price goes down = profit)
                sl_price = entry * (1 + sl_pct)
                tp_price = entry * (1 - tp_pct)
                # To close SHORT, we BUY
                is_buy = True
            else:
                # LONG: SL below entry, TP above entry
                sl_price = entry * (1 - sl_pct)
                tp_price = entry * (1 + tp_pct)
                # To close LONG, we SELL
                is_buy = False

            print(f"\n  {symbol} ({pos['side'].upper()}):")
            print(f"    Entry: ${entry:.6f}")
            print(f"    SL -> ${sl_price:.6f} ({args.sl_pct}%)")
            print(f"    TP -> ${tp_price:.6f} ({args.tp_pct}%)")
            print(f"    Close by: {'BUY' if is_buy else 'SELL'}")

            if args.dry_run:
                print(f"    [DRY RUN] Would place SL and TP")
                continue

            # Place SL trigger order
            try:
                sl_result = await client.place_trigger_order(
                    symbol=symbol,
                    is_buy=is_buy,
                    size=size,
                    trigger_price=sl_price,
                    limit_price=None,
                    tpsl="sl",
                    reduce_only=True,
                )
                print(f"    [OK] SL placed: {sl_result.get('orderId')}")
            except Exception as e:
                print(f"    [FAILED] SL: {e}")

            await asyncio.sleep(0.3)

            # Place TP trigger order
            try:
                tp_result = await client.place_trigger_order(
                    symbol=symbol,
                    is_buy=is_buy,
                    size=size,
                    trigger_price=tp_price,
                    limit_price=None,
                    tpsl="tp",
                    reduce_only=True,
                )
                print(f"    [OK] TP placed: {tp_result.get('orderId')}")
            except Exception as e:
                print(f"    [FAILED] TP: {e}")

            await asyncio.sleep(0.3)

        print("\n" + "=" * 70)
        print("COMPLETE")
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
