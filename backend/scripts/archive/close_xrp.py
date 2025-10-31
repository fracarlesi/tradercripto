#!/usr/bin/env python3
"""Close XRP test position"""

from dotenv import load_dotenv

load_dotenv()

from services.hyperliquid_trading_service import hyperliquid_trading_service

print("\n🔄 Closing XRP test position...\n")

if not hyperliquid_trading_service.enabled:
    print("❌ Hyperliquid trading not enabled!")
    exit(1)

# Close XRP position
result = hyperliquid_trading_service.close_position("XRP")

if result and result.get("success"):
    print("✅ Position closed successfully!")
    print(f"   Symbol: {result['symbol']}")
    print(f"   Size: {result['size']}")
    print(f"   Side: {result['side']}")
else:
    print(f"❌ Failed to close position: {result}")

print("\n")
