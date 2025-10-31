#!/usr/bin/env python3
"""Test placing a small order on Hyperliquid"""

from dotenv import load_dotenv

load_dotenv()

from services.hyperliquid_trading_service import hyperliquid_trading_service

print("\n🧪 Testing XRP order on Hyperliquid...\n")

if not hyperliquid_trading_service.enabled:
    print("❌ Hyperliquid trading not enabled!")
    exit(1)

# Try to place a small XRP buy order ($10)
print("Attempting to BUY $10 worth of XRP...")
result = hyperliquid_trading_service.place_market_order(symbol="XRP", side="buy", size_usd=10.0)

print("\n📋 Order Result:")
print(result)
print("\n")
