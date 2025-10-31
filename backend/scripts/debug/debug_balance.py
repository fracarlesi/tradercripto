#!/usr/bin/env python3
"""Debug Hyperliquid balance reading"""

import json
import os

from dotenv import load_dotenv
from hyperliquid.info import Info
from hyperliquid.utils import constants

load_dotenv()

wallet_address = os.getenv("HYPERLIQUID_WALLET_ADDRESS")

print(f"\n🔍 Debugging balance for: {wallet_address}\n")

try:
    info = Info(constants.MAINNET_API_URL)

    print("=" * 60)
    print("RAW API RESPONSE:")
    print("=" * 60)

    user_state = info.user_state(wallet_address)
    print(json.dumps(user_state, indent=2))

    print("\n" + "=" * 60)
    print("PARSED DATA:")
    print("=" * 60)

    if user_state:
        if "marginSummary" in user_state:
            margin = user_state["marginSummary"]
            print("\nMargin Summary:")
            for key, value in margin.items():
                print(f"  {key}: {value}")

        if "assetPositions" in user_state:
            positions = user_state["assetPositions"]
            print(f"\nAsset Positions: {len(positions)}")
            for pos in positions:
                print(f"  {json.dumps(pos, indent=4)}")

        if "crossMarginSummary" in user_state:
            cross = user_state["crossMarginSummary"]
            print("\nCross Margin Summary:")
            for key, value in cross.items():
                print(f"  {key}: {value}")

    else:
        print("❌ No user state returned!")

except Exception as e:
    print(f"\n❌ ERROR: {e}")
    import traceback

    traceback.print_exc()
