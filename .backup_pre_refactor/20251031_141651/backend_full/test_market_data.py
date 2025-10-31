#!/usr/bin/env python3
from services.trading_commands import AI_TRADING_SYMBOLS
from services.market_data import get_last_price

print(f'Total symbols: {len(AI_TRADING_SYMBOLS)}')
print(f'Checking first 10 symbols: {AI_TRADING_SYMBOLS[:10]}')
print()

failed = []
for symbol in AI_TRADING_SYMBOLS[:10]:
    try:
        price = get_last_price(symbol, 'CRYPTO')
        if price and price > 0:
            print(f'{symbol}: ${price:.4f}')
        else:
            print(f'{symbol}: INVALID price ({price})')
            failed.append(symbol)
    except Exception as e:
        print(f'{symbol}: ERROR - {e}')
        failed.append(symbol)

if failed:
    print(f'\nFailed symbols: {failed}')
    print(f'Failed count: {len(failed)}/{len(AI_TRADING_SYMBOLS[:10])}')
else:
    print('\nAll symbols OK')
