#!/usr/bin/env python3
"""Check recent orders on Hyperliquid"""

import os

from services.hyperliquid_trading_service import hyperliquid_trading_service

print("Checking Hyperliquid trading service...")
print(f"Service enabled: {hyperliquid_trading_service.enabled}")
print()

if hyperliquid_trading_service.enabled:
    # Check account balance
    balance = hyperliquid_trading_service.get_account_balance()
    print("Account Balance:")
    print(f"  {balance}")
    print()

    # Check recent orders
    print("Recent orders (last 10):")
    try:
        from hyperliquid.info import Info
        from hyperliquid.utils import constants

        info = Info(constants.MAINNET_API_URL, skip_ws=True)

        # Get user's address from env
        wallet_address = os.getenv("HYPERLIQUID_WALLET_ADDRESS")
        if wallet_address:
            # Get user fills (executed orders)
            user_fills = info.user_fills(wallet_address)

            if user_fills:
                print(f"Found {len(user_fills)} recent fills:")
                for i, fill in enumerate(user_fills[:10], 1):
                    print(f"\n  Fill {i}:")
                    print(f"    Coin: {fill.get('coin')}")
                    print(f"    Side: {fill.get('side')}")
                    print(f"    Size: {fill.get('sz')}")
                    print(f"    Price: {fill.get('px')}")
                    print(f"    Time: {fill.get('time')}")
            else:
                print("  No recent fills found")
        else:
            print("  HYPERLIQUID_WALLET_ADDRESS not set")

    except Exception as e:
        print(f"  Error getting orders: {e}")
else:
    print("Hyperliquid trading service is NOT enabled")
    print("Check HYPERLIQUID_PRIVATE_KEY in .env")
