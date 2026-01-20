#!/usr/bin/env python3
"""
HLQuantBot Diagnostic State Script
===================================

Comprehensive state report:
1. All open positions with entry/current prices and P&L
2. All open orders (limit + trigger)
3. Account equity and margin usage
4. Detection of potential stale orders

Usage:
    cd simple_bot && python scripts/diagnostic_state.py
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


async def get_frontend_orders(client: HyperliquidClient) -> list[dict[str, Any]]:
    """Get all orders including trigger orders from frontend API."""
    try:
        result = await client._run_sync(
            lambda: client._info.frontend_open_orders(client._account.address)
        )
        return result if result else []
    except Exception as e:
        print(f"Warning: Could not fetch frontend orders: {e}")
        return []


async def main():
    """Main diagnostic function."""
    print("=" * 80)
    print("HLQuantBot Diagnostic State Report")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 80)

    # Check environment
    testnet = os.getenv("HYPERLIQUID_TESTNET", "false").lower() == "true"
    network = "TESTNET" if testnet else "MAINNET"
    print(f"\nNetwork: {network}")

    # Initialize client
    client = HyperliquidClient(testnet=testnet)

    try:
        await client.connect()
        print(f"Wallet: {client.address}")

        # =====================================================================
        # 1. ACCOUNT STATE
        # =====================================================================
        print("\n" + "=" * 80)
        print("1. ACCOUNT STATE")
        print("=" * 80)

        account = await client.get_account_state()
        equity = account['equity']
        margin_used = account['marginUsed']
        available = account['availableBalance']
        unrealized_pnl = account['unrealizedPnl']

        print(f"  Equity:           ${equity:.2f}")
        print(f"  Available:        ${available:.2f} ({available/equity*100:.1f}%)")
        print(f"  Margin Used:      ${margin_used:.2f} ({margin_used/equity*100:.1f}%)")
        print(f"  Unrealized PnL:   ${unrealized_pnl:.2f} ({unrealized_pnl/equity*100:.2f}%)")

        # =====================================================================
        # 2. POSITIONS SUMMARY
        # =====================================================================
        print("\n" + "=" * 80)
        print("2. POSITIONS SUMMARY")
        print("=" * 80)

        positions = await client.get_positions()

        if not positions:
            print("  No open positions")
        else:
            print(f"  Total Positions: {len(positions)}")

            # Sort by unrealized P&L
            positions_sorted = sorted(positions, key=lambda p: p['unrealizedPnl'])

            # Top losers
            print("\n  --- WORST PERFORMERS ---")
            for pos in positions_sorted[:5]:
                pnl_pct = (pos['markPrice'] - pos['entryPrice']) / pos['entryPrice'] * 100
                if pos['side'] == 'short':
                    pnl_pct = -pnl_pct
                print(f"    {pos['symbol']:12s} {pos['side']:5s} | Entry: ${pos['entryPrice']:<10.4f} | "
                      f"Mark: ${pos['markPrice']:<10.4f} | PnL: ${pos['unrealizedPnl']:>7.2f} ({pnl_pct:>+6.2f}%)")

            # Top winners
            print("\n  --- BEST PERFORMERS ---")
            for pos in positions_sorted[-5:]:
                pnl_pct = (pos['markPrice'] - pos['entryPrice']) / pos['entryPrice'] * 100
                if pos['side'] == 'short':
                    pnl_pct = -pnl_pct
                print(f"    {pos['symbol']:12s} {pos['side']:5s} | Entry: ${pos['entryPrice']:<10.4f} | "
                      f"Mark: ${pos['markPrice']:<10.4f} | PnL: ${pos['unrealizedPnl']:>7.2f} ({pnl_pct:>+6.2f}%)")

            # Full positions table
            print("\n  --- ALL POSITIONS ---")
            print("  " + "-" * 100)
            print(f"  {'Symbol':<12} {'Side':<6} {'Size':>12} {'Entry':>10} {'Mark':>10} {'PnL':>10} {'Lev':>4}")
            print("  " + "-" * 100)

            total_long_notional = 0
            total_short_notional = 0

            for pos in sorted(positions, key=lambda p: p['symbol']):
                notional = pos['size'] * pos['markPrice']
                if pos['side'] == 'long':
                    total_long_notional += notional
                else:
                    total_short_notional += notional

                print(f"  {pos['symbol']:<12} {pos['side']:<6} {pos['size']:>12.6f} "
                      f"${pos['entryPrice']:>9.4f} ${pos['markPrice']:>9.4f} "
                      f"${pos['unrealizedPnl']:>9.2f} {pos['leverage']:>3}x")

            print("  " + "-" * 100)
            print(f"\n  Long Exposure:  ${total_long_notional:.2f}")
            print(f"  Short Exposure: ${total_short_notional:.2f}")
            print(f"  Net Exposure:   ${total_long_notional - total_short_notional:.2f}")
            print(f"  Gross Exposure: ${total_long_notional + total_short_notional:.2f}")

        # =====================================================================
        # 3. OPEN ORDERS ANALYSIS
        # =====================================================================
        print("\n" + "=" * 80)
        print("3. OPEN ORDERS ANALYSIS")
        print("=" * 80)

        # Get all orders from frontend API (includes trigger orders)
        frontend_orders = await get_frontend_orders(client)

        limit_orders = []
        trigger_orders = []

        for order in frontend_orders:
            order_data = order.get("order", order)
            order_type = order_data.get("orderType", {})

            if isinstance(order_type, dict) and "trigger" in order_type:
                trigger_info = order_type.get("trigger", {})
                trigger_orders.append({
                    "oid": order_data.get("oid"),
                    "coin": order_data.get("coin"),
                    "side": "BUY" if str(order_data.get("side", "")).upper() == "B" else "SELL",
                    "sz": float(order_data.get("sz", 0)),
                    "limitPx": float(order_data.get("limitPx", 0)),
                    "triggerPx": float(trigger_info.get("triggerPx", 0)),
                    "tpsl": trigger_info.get("tpsl", ""),
                    "isMarket": trigger_info.get("isMarket", False),
                    "timestamp": order_data.get("timestamp"),
                })
            else:
                limit_orders.append({
                    "oid": order_data.get("oid"),
                    "coin": order_data.get("coin"),
                    "side": "BUY" if str(order_data.get("side", "")).upper() == "B" else "SELL",
                    "sz": float(order_data.get("sz", 0)),
                    "limitPx": float(order_data.get("limitPx", 0)),
                    "reduceOnly": order_data.get("reduceOnly", False),
                    "timestamp": order_data.get("timestamp"),
                })

        # Also get limit orders from standard API as backup
        standard_orders = await client.get_open_orders()

        print(f"\n  Total Frontend Orders: {len(frontend_orders)}")
        print(f"  Limit Orders: {len(limit_orders)}")
        print(f"  Trigger Orders (TP/SL): {len(trigger_orders)}")
        print(f"  Standard API Orders: {len(standard_orders)}")

        # Group limit orders by symbol and type
        print("\n  --- LIMIT ORDERS BY SYMBOL ---")
        orders_by_symbol: dict[str, list] = {}
        for order in limit_orders:
            symbol = order['coin']
            if symbol not in orders_by_symbol:
                orders_by_symbol[symbol] = []
            orders_by_symbol[symbol].append(order)

        for symbol in sorted(orders_by_symbol.keys()):
            symbol_orders = orders_by_symbol[symbol]
            reduce_only_orders = [o for o in symbol_orders if o.get('reduceOnly')]
            entry_orders = [o for o in symbol_orders if not o.get('reduceOnly')]

            if reduce_only_orders:
                print(f"\n  [{symbol}] TP/SL orders:")
                for order in reduce_only_orders:
                    print(f"    {order['side']:<4} {order['sz']:>12.6f} @ ${order['limitPx']:<10.4f} (ID: {order['oid']})")

            if entry_orders:
                print(f"\n  [{symbol}] Entry orders:")
                for order in entry_orders:
                    print(f"    {order['side']:<4} {order['sz']:>12.6f} @ ${order['limitPx']:<10.4f} (ID: {order['oid']})")

        # Trigger orders
        if trigger_orders:
            print("\n  --- TRIGGER ORDERS (TP/SL) ---")
            for order in trigger_orders:
                tpsl_label = "TP" if order['tpsl'] == 'tp' else "SL" if order['tpsl'] == 'sl' else order['tpsl']
                order_type = "MARKET" if order['isMarket'] else f"LIMIT@${order['limitPx']:.4f}"
                print(f"  [{order['coin']}] {tpsl_label} {order['side']} {order['sz']:.6f} @ trigger ${order['triggerPx']:.4f} ({order_type})")

        # =====================================================================
        # 4. ORDER/POSITION ALIGNMENT CHECK
        # =====================================================================
        print("\n" + "=" * 80)
        print("4. ORDER/POSITION ALIGNMENT CHECK")
        print("=" * 80)

        position_symbols = {pos['symbol'] for pos in positions}

        # Check for positions without any protective orders
        symbols_with_tp = set()
        symbols_with_sl = set()

        for order in limit_orders:
            if order.get('reduceOnly'):
                symbols_with_tp.add(order['coin'])

        for order in trigger_orders:
            if order['tpsl'] == 'tp':
                symbols_with_tp.add(order['coin'])
            elif order['tpsl'] == 'sl':
                symbols_with_sl.add(order['coin'])

        missing_tp = position_symbols - symbols_with_tp
        missing_sl = position_symbols - symbols_with_sl

        if not missing_tp and not missing_sl:
            print("  [OK] All positions have protective orders")
        else:
            if missing_tp:
                print(f"\n  [WARNING] Positions missing TP orders ({len(missing_tp)}):")
                for symbol in sorted(missing_tp):
                    pos = next(p for p in positions if p['symbol'] == symbol)
                    print(f"    - {symbol}: {pos['side']} {pos['size']} @ ${pos['entryPrice']:.4f}")

            if missing_sl:
                print(f"\n  [WARNING] Positions missing SL orders ({len(missing_sl)}):")
                for symbol in sorted(missing_sl):
                    pos = next(p for p in positions if p['symbol'] == symbol)
                    print(f"    - {symbol}: {pos['side']} {pos['size']} @ ${pos['entryPrice']:.4f}")

        # Check for orphaned orders (orders for positions that don't exist)
        order_symbols = set()
        for order in limit_orders:
            order_symbols.add(order['coin'])
        for order in trigger_orders:
            order_symbols.add(order['coin'])

        orphaned_orders = order_symbols - position_symbols
        if orphaned_orders:
            print(f"\n  [WARNING] Orphaned orders (no matching position):")
            for symbol in sorted(orphaned_orders):
                symbol_orders = [o for o in limit_orders if o['coin'] == symbol]
                symbol_triggers = [o for o in trigger_orders if o['coin'] == symbol]
                print(f"    - {symbol}: {len(symbol_orders)} limit + {len(symbol_triggers)} trigger orders")

        # Check for duplicate orders (multiple TP or SL for same position)
        print("\n  --- DUPLICATE ORDER CHECK ---")
        for symbol in position_symbols:
            tp_orders = [o for o in limit_orders if o['coin'] == symbol and o.get('reduceOnly')]
            tp_triggers = [o for o in trigger_orders if o['coin'] == symbol and o['tpsl'] == 'tp']
            sl_triggers = [o for o in trigger_orders if o['coin'] == symbol and o['tpsl'] == 'sl']

            if len(tp_orders) > 2:
                print(f"  [NOTE] {symbol}: {len(tp_orders)} reduce-only limit orders (expected 2 for TP range)")
            if len(tp_triggers) > 1:
                print(f"  [WARN] {symbol}: {len(tp_triggers)} TP trigger orders")
            if len(sl_triggers) > 1:
                print(f"  [WARN] {symbol}: {len(sl_triggers)} SL trigger orders")

        # =====================================================================
        # 5. POTENTIAL STALE ORDERS
        # =====================================================================
        print("\n" + "=" * 80)
        print("5. POTENTIAL STALE ORDERS")
        print("=" * 80)

        # Identify orders that might be "stale" in the ExecutionEngine's pending_orders dict
        # These are typically non-reduce-only orders that haven't been filled
        entry_orders = [o for o in limit_orders if not o.get('reduceOnly')]

        if entry_orders:
            print(f"\n  Entry orders (potential stale pending orders): {len(entry_orders)}")
            for order in entry_orders:
                print(f"    [{order['coin']}] {order['side']} {order['sz']:.6f} @ ${order['limitPx']:.4f} (ID: {order['oid']})")
        else:
            print("  No entry orders found (all orders are reduce-only TP/SL)")

        # Check for old orders based on order ID pattern (lower IDs = older)
        if limit_orders:
            order_ids = sorted([o['oid'] for o in limit_orders])
            oldest_ids = order_ids[:3]
            print(f"\n  Oldest order IDs (may indicate stale orders):")
            for oid in oldest_ids:
                order = next(o for o in limit_orders if o['oid'] == oid)
                print(f"    ID {oid}: [{order['coin']}] {order['side']} @ ${order['limitPx']:.4f}")

        # =====================================================================
        # 6. SUMMARY
        # =====================================================================
        print("\n" + "=" * 80)
        print("6. SUMMARY")
        print("=" * 80)

        print(f"""
  Account Equity:      ${equity:.2f}
  Total Positions:     {len(positions)}
  Unrealized P&L:      ${unrealized_pnl:.2f} ({unrealized_pnl/equity*100:.2f}%)

  Open Limit Orders:   {len(limit_orders)}
  Trigger Orders:      {len(trigger_orders)}
  Entry Orders:        {len(entry_orders)} (potential stale)

  Positions with TP:   {len(symbols_with_tp & position_symbols)}/{len(position_symbols)}
  Positions with SL:   {len(symbols_with_sl & position_symbols)}/{len(position_symbols)}
  Orphaned Orders:     {len(orphaned_orders)} symbols
""")

        print("=" * 80)
        print("Diagnostic Complete")
        print("=" * 80)

    except Exception as e:
        print(f"\nError during diagnostic: {e}")
        import traceback
        traceback.print_exc()

    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
