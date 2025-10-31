#!/usr/bin/env python3
"""Check open positions on Hyperliquid"""

from dotenv import load_dotenv
load_dotenv()

from services.hyperliquid_trading_service import hyperliquid_trading_service

print("\n📊 Checking Hyperliquid positions...\n")

if not hyperliquid_trading_service.enabled:
    print("❌ Hyperliquid trading not enabled!")
    exit(1)

# Get account balance
balance = hyperliquid_trading_service.get_account_balance()
print(f"💰 Account Balance:")
print(f"   Total Equity: ${balance.get('total_equity', 0):.2f}")
print(f"   Margin Used: ${balance.get('margin_used', 0):.2f}")
print(f"   Available: ${balance.get('available', 0):.2f}")

# Get open positions
positions = hyperliquid_trading_service.get_open_positions()
print(f"\n📈 Open Positions: {len(positions)}")
for pos in positions:
    print(f"   {pos['symbol']}: {pos['size']} @ ${pos['entry_price']:.4f}")
    print(f"      Side: {pos['side']}")
    print(f"      Unrealized PnL: ${pos['unrealized_pnl']:.2f}")

if len(positions) == 0:
    print("   No open positions")

print("\n")
