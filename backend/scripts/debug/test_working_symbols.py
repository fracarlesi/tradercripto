"""
Test to find which crypto symbols actually work with CCXT on Hyperliquid
"""

import ccxt


def test_hyperliquid_markets():
    """Test which markets are actually available on Hyperliquid via CCXT"""
    print("Initializing Hyperliquid exchange...")
    exchange = ccxt.hyperliquid(
        {
            "sandbox": False,
            "enableRateLimit": True,
        }
    )

    print("Loading markets...")
    markets = exchange.load_markets()

    # Filter for USDC markets only
    usdc_markets = {k: v for k, v in markets.items() if "/USDC" in k or "/USD" in k}

    print(f"\n📊 Total markets available: {len(markets)}")
    print(f"💰 USDC/USD markets: {len(usdc_markets)}\n")

    # Separate by type
    perpetual_markets = {
        k: v for k, v in usdc_markets.items() if v.get("type") == "swap" or ":" in k
    }
    spot_markets = {
        k: v for k, v in usdc_markets.items() if v.get("type") == "spot" and ":" not in k
    }

    print(f"🔄 Perpetual swaps: {len(perpetual_markets)}")
    print(f"💵 Spot markets: {len(spot_markets)}\n")

    # Show first 20 perpetual markets
    print("=" * 80)
    print("PERPETUAL SWAPS (first 30):")
    print("=" * 80)
    for i, (symbol, market) in enumerate(list(perpetual_markets.items())[:30]):
        base = market.get("base", "N/A")
        quote = market.get("quote", "N/A")
        active = market.get("active", False)
        print(f"{i + 1:3}. {symbol:20} | Base: {base:10} | Quote: {quote:10} | Active: {active}")

    print(f"\n... and {len(perpetual_markets) - 30} more\n")

    # Test actual price fetching for popular cryptos
    print("=" * 80)
    print("TESTING PRICE FETCHING:")
    print("=" * 80)

    test_symbols = ["BTC", "ETH", "SOL", "DOGE", "ATOM", "MATIC", "AVAX", "ARB", "OP", "SUI"]

    working_symbols = []

    for symbol in test_symbols:
        # Try different formats
        formats_to_try = [
            f"{symbol}/USDC:USDC",  # Perpetual format
            f"{symbol}/USDC",  # Spot format
            f"{symbol}/USD:USD",  # Alternative perpetual
            f"{symbol}/USD",  # Alternative spot
        ]

        worked = False
        for fmt in formats_to_try:
            try:
                ticker = exchange.fetch_ticker(fmt)
                price = ticker.get("last")
                if price:
                    print(f"✅ {symbol:10} | Format: {fmt:20} | Price: ${price:,.2f}")
                    working_symbols.append((symbol, fmt, price))
                    worked = True
                    break
            except Exception:
                continue

        if not worked:
            print(f"❌ {symbol:10} | No working format found")

    print(f"\n📊 Working symbols: {len(working_symbols)} / {len(test_symbols)}")

    # Extract just the base symbols that work
    working_bases = [s[0] for s in working_symbols]
    print(f"\n🎯 Recommended symbols for trading: {working_bases}")

    return working_bases, perpetual_markets


if __name__ == "__main__":
    working, all_perps = test_hyperliquid_markets()

    print("\n" + "=" * 80)
    print("RECOMMENDATION:")
    print("=" * 80)
    print("The system should only trade on symbols that are actually available.")
    print(f"Currently {len(all_perps)} perpetual markets are available on Hyperliquid.")
    print("\nTo fix the issue, update the symbol list to include only working symbols,")
    print("or use Hyperliquid's native SDK instead of CCXT for better compatibility.")
