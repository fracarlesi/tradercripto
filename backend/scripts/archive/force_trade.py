#!/usr/bin/env python3
"""Force immediate trading decision"""

from dotenv import load_dotenv

load_dotenv()

from services.trading_commands import place_ai_driven_crypto_order

print("\n🚀 Forcing DeepSeek trading decision NOW...\n")

place_ai_driven_crypto_order(max_ratio=0.2)

print("\n✅ Trading decision executed! Check the logs above.\n")
