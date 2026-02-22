"""
Emergency position/order checker for Hyperliquid.
Queries all open positions and ALL orders (including trigger TP/SL).
"""
import asyncio
import json
import os
import sys
from datetime import datetime
from dotenv import load_dotenv

# Load .env
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.utils import constants


async def main():
    private_key = os.getenv("PRIVATE_KEY")
    wallet_address = os.getenv("WALLET_ADDRESS")
    environment = os.getenv("ENVIRONMENT", "mainnet")

    if not private_key:
        print("ERROR: PRIVATE_KEY not found in .env")
        sys.exit(1)

    account = Account.from_key(private_key)
    address = wallet_address or account.address

    base_url = constants.MAINNET_API_URL if environment == "mainnet" else constants.TESTNET_API_URL
    print(f"Environment: {environment.upper()}")
    print(f"Address: {address}")
    print(f"API URL: {base_url}")
    print("=" * 80)

    info = Info(base_url, skip_ws=True)

    # 1. Get full user state (positions + account info)
    print("\n### ACCOUNT STATE ###")
    user_state = info.user_state(address)
    margin_summary = user_state.get("marginSummary", {})
    print(f"  Account Value:    ${float(margin_summary.get('accountValue', 0)):.2f}")
    print(f"  Total Margin Used: ${float(margin_summary.get('totalMarginUsed', 0)):.2f}")
    print(f"  Total Ntl Pos:    ${float(margin_summary.get('totalNtlPos', 0)):.2f}")
    print(f"  Total Raw Usd:    ${float(margin_summary.get('totalRawUsd', 0)):.2f}")

    # 2. Positions
    print("\n### OPEN POSITIONS ###")
    positions = user_state.get("assetPositions", [])
    open_positions = []
    for p in positions:
        pos = p.get("position", {})
        size = float(pos.get("szi", 0))
        if abs(size) < 0.0001:
            continue
        open_positions.append(pos)
        symbol = pos.get("coin", "?")
        entry_px = float(pos.get("entryPx", 0))
        unrealized_pnl = float(pos.get("unrealizedPnl", 0))
        leverage_info = pos.get("leverage", {})
        leverage_val = leverage_info.get("value", "?") if isinstance(leverage_info, dict) else leverage_info
        liq_px = pos.get("liquidationPx")
        margin_used = float(pos.get("marginUsed", 0))
        pos_value = float(pos.get("positionValue", 0))
        return_on_equity = float(pos.get("returnOnEquity", 0))
        side = "LONG" if size > 0 else "SHORT"

        print(f"\n  [{symbol}] {side}")
        print(f"    Size:            {abs(size)}")
        print(f"    Entry Price:     ${entry_px:.6f}")
        print(f"    Position Value:  ${pos_value:.2f}")
        print(f"    Unrealized PnL:  ${unrealized_pnl:.4f}")
        print(f"    ROE:             {return_on_equity:.4f} ({return_on_equity*100:.2f}%)")
        print(f"    Leverage:        {leverage_val}x")
        print(f"    Margin Used:     ${margin_used:.4f}")
        print(f"    Liquidation Px:  {liq_px if liq_px else 'N/A'}")

    if not open_positions:
        print("  (No open positions)")

    # 3. Open LIMIT orders (standard)
    print("\n### OPEN LIMIT ORDERS ###")
    open_orders = info.open_orders(address)
    if open_orders:
        for o in open_orders:
            coin = o.get("coin", "?")
            side = "BUY" if o.get("side", "").upper() == "B" else "SELL"
            sz = float(o.get("sz", 0))
            limit_px = float(o.get("limitPx", 0))
            oid = o.get("oid")
            order_type = o.get("orderType", "?")
            reduce_only = o.get("reduceOnly", False)
            print(f"  [{coin}] {side} {sz} @ ${limit_px:.6f} | type={order_type} | reduceOnly={reduce_only} | oid={oid}")
    else:
        print("  (No open limit orders)")

    # 4. TRIGGER orders (TP/SL) - These are separate from open_orders!
    # The SDK's frontend_open_orders includes trigger orders too.
    # We need to use the info.query method or a direct POST for trigger orders.
    print("\n### TRIGGER ORDERS (TP/SL) ###")
    try:
        # Use the user_state which contains trigger orders in crossMarginSummary
        # Actually, trigger orders are fetched via a different endpoint
        # Let's use the raw API call
        import requests

        payload = {
            "type": "openOrders",
            "user": address,
        }
        resp = requests.post(f"{base_url}/info", json=payload)
        standard_orders = resp.json() if resp.status_code == 200 else []

        # Now get frontend open orders which include trigger orders
        payload2 = {
            "type": "frontendOpenOrders",
            "user": address,
        }
        resp2 = requests.post(f"{base_url}/info", json=payload2)
        frontend_orders = resp2.json() if resp2.status_code == 200 else []

        # Frontend orders include trigger orders that standard openOrders doesn't
        print(f"  Standard open orders: {len(standard_orders)}")
        print(f"  Frontend open orders (includes triggers): {len(frontend_orders)}")

        if frontend_orders:
            for o in frontend_orders:
                coin = o.get("coin", "?")
                side = "BUY" if o.get("side", "").upper() == "B" else "SELL"
                sz = float(o.get("sz", 0))
                limit_px = o.get("limitPx", "?")
                trigger_px = o.get("triggerPx", None)
                order_type = o.get("orderType", "?")
                reduce_only = o.get("reduceOnly", False)
                oid = o.get("oid", "?")
                tpsl = o.get("tpsl", None)
                trigger_cond = o.get("triggerCondition", None)
                is_trigger = o.get("isTrigger", trigger_px is not None)

                label = ""
                if tpsl == "tp":
                    label = " [TAKE PROFIT]"
                elif tpsl == "sl":
                    label = " [STOP LOSS]"
                elif is_trigger or trigger_px:
                    label = " [TRIGGER]"

                print(f"  [{coin}] {side} {sz} | limitPx={limit_px} | triggerPx={trigger_px} | type={order_type} | reduceOnly={reduce_only} | tpsl={tpsl}{label} | oid={oid}")
        else:
            print("  (No frontend/trigger orders)")

    except Exception as e:
        print(f"  ERROR fetching trigger orders: {e}")
        import traceback
        traceback.print_exc()

    # 5. Cross-reference: Which positions have TP/SL?
    print("\n" + "=" * 80)
    print("### PROTECTION STATUS PER POSITION ###")

    position_symbols = set()
    for p in open_positions:
        position_symbols.add(p.get("coin", "?"))

    for symbol in position_symbols:
        # Find trigger orders for this symbol
        symbol_triggers = []
        if frontend_orders:
            for o in frontend_orders:
                if o.get("coin") == symbol and (o.get("triggerPx") or o.get("tpsl")):
                    symbol_triggers.append(o)

        # Find standard reduce-only orders for this symbol
        symbol_reduce_only = []
        for o in open_orders:
            if o.get("coin") == symbol and o.get("reduceOnly"):
                symbol_reduce_only.append(o)

        has_tp = any(o.get("tpsl") == "tp" or (o.get("orderType", "").lower().startswith("take") if o.get("orderType") else False) for o in symbol_triggers)
        has_sl = any(o.get("tpsl") == "sl" or (o.get("orderType", "").lower().startswith("stop") if o.get("orderType") else False) for o in symbol_triggers)

        pos_data = next((p for p in open_positions if p.get("coin") == symbol), {})
        size = float(pos_data.get("szi", 0))
        side = "LONG" if size > 0 else "SHORT"

        status = ""
        if has_tp and has_sl:
            status = "PROTECTED (TP + SL)"
        elif has_tp:
            status = "PARTIAL (TP only, NO SL!)"
        elif has_sl:
            status = "PARTIAL (SL only, NO TP!)"
        else:
            # Check if there are reduce-only limit orders that might be TP/SL
            if symbol_reduce_only:
                status = f"POSSIBLE ({len(symbol_reduce_only)} reduce-only limit orders, but NO trigger TP/SL)"
            else:
                status = "*** UNPROTECTED *** (NO TP, NO SL!)"

        print(f"\n  [{symbol}] {side} | {status}")
        if symbol_triggers:
            for t in symbol_triggers:
                tpsl_label = t.get("tpsl", "unknown")
                trigger_px = t.get("triggerPx", "?")
                print(f"    -> {tpsl_label.upper()}: trigger @ {trigger_px}")
        if symbol_reduce_only:
            for ro in symbol_reduce_only:
                print(f"    -> Reduce-only limit: {ro.get('side')} {ro.get('sz')} @ {ro.get('limitPx')}")
        if not symbol_triggers and not symbol_reduce_only:
            print(f"    -> NO protective orders found!")

    # 6. Dump raw data for debugging
    print("\n" + "=" * 80)
    print("### RAW USER STATE (positions only) ###")
    for p in positions:
        pos = p.get("position", {})
        if abs(float(pos.get("szi", 0))) > 0.0001:
            print(json.dumps(pos, indent=2))

    print("\n### RAW FRONTEND ORDERS ###")
    if frontend_orders:
        print(json.dumps(frontend_orders, indent=2))
    else:
        print("  (empty)")


if __name__ == "__main__":
    asyncio.run(main())
