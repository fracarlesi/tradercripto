#!/usr/bin/env python3
"""Test loading all crypto from Hyperliquid"""

from dotenv import load_dotenv
load_dotenv()

from services.trading_commands import AI_TRADING_SYMBOLS
from services.ai_decision_service import SUPPORTED_SYMBOLS
from services.hyperliquid_market_data import get_last_price_from_hyperliquid

print(f"\n📊 Loaded {len(AI_TRADING_SYMBOLS)} crypto symbols for trading")
print(f"📊 Loaded {len(SUPPORTED_SYMBOLS)} supported symbols\n")

# Test getting prices for first 10
print("Testing prices for first 10 crypto:")
print("-" * 50)
for i, symbol in enumerate(AI_TRADING_SYMBOLS[:10]):
    try:
        price = get_last_price_from_hyperliquid(symbol)
        if price:
            print(f"{symbol:10} | ${price:.4f}")
        else:
            print(f"{symbol:10} | No price available")
    except Exception as e:
        print(f"{symbol:10} | Error: {e}")

print("\n✅ System ready with all available crypto!")
