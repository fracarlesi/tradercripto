#!/usr/bin/env python3
from services.market_data import get_last_price
from services.trading_commands import AI_TRADING_SYMBOLS

print(f"Total symbols to check: {len(AI_TRADING_SYMBOLS)}")
print("=" * 60)

failed = []
successful = 0

for i, symbol in enumerate(AI_TRADING_SYMBOLS):
    try:
        price = get_last_price(symbol, "CRYPTO")
        if price and price > 0:
            successful += 1
            if i < 5:  # Show first 5
                print(f"✓ {symbol}: ${price:.4f}")
        else:
            print(f"✗ {symbol}: INVALID price ({price})")
            failed.append(symbol)
    except Exception as e:
        print(f"✗ {symbol}: ERROR - {str(e)[:80]}")
        failed.append(symbol)

print("=" * 60)
print("\nSummary:")
print(f"  Successful: {successful}/{len(AI_TRADING_SYMBOLS)}")
print(f"  Failed: {len(failed)}/{len(AI_TRADING_SYMBOLS)}")

if failed:
    print(f"\nFailed symbols ({len(failed)}):")
    for symbol in failed:
        print(f"  - {symbol}")
