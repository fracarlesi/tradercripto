#!/usr/bin/env python3
"""Get asset specifications from Hyperliquid"""

from dotenv import load_dotenv
load_dotenv()

from hyperliquid.info import Info
from hyperliquid.utils import constants

# Initialize Info client
info = Info(constants.MAINNET_API_URL)

# Get all asset metadata
print("\n📊 Fetching asset metadata from Hyperliquid...\n")

meta = info.meta()

print("All Available Assets on Hyperliquid:")
print("-" * 70)
print(f"{'Symbol':<10} | szDecimals | Max Leverage")
print("-" * 70)

all_symbols = []
for asset in meta['universe']:
    symbol = asset['name']
    sz_decimals = asset.get('szDecimals', 'N/A')
    max_leverage = asset.get('maxLeverage', 'N/A')

    print(f"{symbol:<10} | {sz_decimals:^10} | {max_leverage}x")
    all_symbols.append(symbol)

print("-" * 70)
print("\nNote: szDecimals determines the precision for order sizes")
print("      e.g., szDecimals=3 means 1.001 is valid, 1.0001 is not\n")
