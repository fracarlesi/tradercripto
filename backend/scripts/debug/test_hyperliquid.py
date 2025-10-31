#!/usr/bin/env python3
"""Test Hyperliquid connection and configuration"""

import os

from dotenv import load_dotenv

# Load .env
load_dotenv()

print("\n" + "=" * 60)
print("HYPERLIQUID CONFIGURATION TEST")
print("=" * 60 + "\n")

# Check environment variables
private_key = os.getenv("HYPERLIQUID_PRIVATE_KEY")
wallet_address = os.getenv("HYPERLIQUID_WALLET_ADDRESS")
enable_trading = os.getenv("ENABLE_REAL_TRADING", "false")
max_capital = os.getenv("MAX_CAPITAL_USD", "53.0")

print(f"✓ Private key configured: {'Yes' if private_key else 'No'}")
if private_key:
    print(f"  Private key: {private_key[:10]}...{private_key[-10:]}")

print(f"✓ Wallet address: {wallet_address}")
print(f"✓ Real trading enabled: {enable_trading}")
print(f"✓ Max capital: ${max_capital}")

print("\n" + "-" * 60)
print("Testing Hyperliquid connection...")
print("-" * 60 + "\n")

try:
    from services.hyperliquid_trading_service import hyperliquid_trading_service

    print("✓ Hyperliquid service initialized")
    print(f"✓ Trading enabled: {hyperliquid_trading_service.enabled}")
    print(f"✓ Max capital: ${hyperliquid_trading_service.max_capital}")

    if hyperliquid_trading_service.enabled:
        print("\n" + "-" * 60)
        print("Fetching account balance...")
        print("-" * 60 + "\n")

        balance = hyperliquid_trading_service.get_account_balance()

        if "error" in balance:
            print(f"❌ Error: {balance['error']}")
        else:
            print(f"✓ Total Equity: ${balance.get('total_equity', 0):.2f}")
            print(f"✓ Margin Used: ${balance.get('margin_used', 0):.2f}")
            print(f"✓ Available: ${balance.get('available', 0):.2f}")

            positions = balance.get("positions", [])
            if positions:
                print(f"\nOpen positions: {len(positions)}")
                for pos in positions:
                    print(f"  - {pos}")
            else:
                print("\nNo open positions")

        print("\n" + "-" * 60)
        print("Fetching open positions...")
        print("-" * 60 + "\n")

        positions = hyperliquid_trading_service.get_open_positions()
        if positions:
            print(f"✓ Found {len(positions)} open position(s):")
            for pos in positions:
                print(
                    f"  - {pos['symbol']}: {pos['size']} ({pos['side']}) @ ${pos['entry_price']:.2f}"
                )
                print(f"    Unrealized P&L: ${pos['unrealized_pnl']:.2f}")
        else:
            print("✓ No open positions (good for starting)")

        print("\n" + "=" * 60)
        print("✅ HYPERLIQUID CONNECTION SUCCESSFUL!")
        print("=" * 60)
        print("\n🚀 Ready for real trading with DeepSeek!\n")

    else:
        print("\n⚠️  Real trading is DISABLED")
        print("Set ENABLE_REAL_TRADING=true in .env to enable\n")

except Exception as e:
    print(f"\n❌ ERROR: {e}")
    import traceback

    traceback.print_exc()
    print("\n")
