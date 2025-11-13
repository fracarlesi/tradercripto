"""
Analyze why AI is not catching top performers.

This script compares:
1. What AI is currently trading (positions)
2. Top performers from last hour
3. Technical scores of both groups
"""
import asyncio
import sys
import time
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from hyperliquid.info import Info
from hyperliquid.utils import constants
from services.trading.hyperliquid_trading_service import hyperliquid_trading_service
from services.market_data.technical_analysis import calculate_technical_factors
from services.trading.pivot_analysis import get_pivot_points


async def analyze_why_missing_top_performers():
    print("=" * 80)
    print("ANALISI: PERCHE NON INTERCETTIAMO I TOP PERFORMERS?")
    print("=" * 80)

    # 1. Get current positions (what AI actually traded)
    user_state = await hyperliquid_trading_service.get_user_state_async()
    current_positions = user_state.get("assetPositions", [])

    print("\n📊 POSIZIONI ATTUALI AI:")
    current_coins = []
    if current_positions:
        for pos in current_positions:
            coin = pos["position"]["coin"]
            current_coins.append(coin)
            print(f"  - {coin}")
    else:
        print("  Nessuna posizione aperta")

    # 2. Get top performers from last hour
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    meta = info.meta()
    all_coins = [asset["name"] for asset in meta["universe"]]

    end_time = int(time.time() * 1000)
    start_time = end_time - (2 * 60 * 60 * 1000)

    print(f"\n🔍 Analyzing {len(all_coins)} symbols...")

    performers = []
    errors = 0
    for i, coin in enumerate(all_coins):
        try:
            candles = info.candles_snapshot(coin, "1h", start_time, end_time)
            if len(candles) >= 2:
                prev_candle = candles[-2]
                curr_candle = candles[-1]
                open_price = float(prev_candle["o"])
                close_price = float(curr_candle["c"])
                volume = float(curr_candle["v"])

                if open_price > 0:
                    pct_change = ((close_price - open_price) / open_price) * 100
                    performers.append({
                        "coin": coin,
                        "change_pct": pct_change,
                        "volume": volume,
                        "price": close_price
                    })

            if (i + 1) % 50 == 0:
                print(f"  Progress: {i+1}/{len(all_coins)}...")

        except Exception as e:
            errors += 1
            if errors <= 3:
                print(f"  Error for {coin}: {str(e)[:50]}")
            continue

    if errors > 3:
        print(f"  ... and {errors - 3} more errors (rate limiting)")

    performers.sort(key=lambda x: x["change_pct"], reverse=True)
    top_10_gainers = performers[:10]
    top_10_losers = performers[-10:]

    print(f"\n✅ Analyzed {len(performers)}/{len(all_coins)} symbols successfully")

    print("\n🚀 TOP 10 GAINERS (ultima ora):")
    print(f"{'Rank':<6} {'Symbol':<12} {'Change':<10} {'Volume':<15} {'Price':<12}")
    print("-" * 80)
    for i, p in enumerate(top_10_gainers, 1):
        vol_str = f"${p['volume']:,.0f}"
        print(f"{i:<6} {p['coin']:<12} {p['change_pct']:+6.2f}%    {vol_str:<15} ${p['price']:<10.4f}")

    # 3. Check which top performers are in AI's analyzed list
    print("\n🔍 VERIFICA: I TOP PERFORMERS SONO TRA I 220 SYMBOLS ANALIZZATI DALL'AI?")

    # Get the list of symbols AI analyzes (from auto_trader.py logic)
    all_mids = await hyperliquid_trading_service.get_all_mids_async()
    ai_analyzed_symbols = list(all_mids.keys())

    print(f"L'AI analizza {len(ai_analyzed_symbols)} symbols totali")

    for i, p in enumerate(top_10_gainers, 1):
        coin = p['coin']
        is_analyzed = coin in ai_analyzed_symbols
        status = "✅ SI" if is_analyzed else "❌ NO"
        print(f"  {i}. {coin} ({p['change_pct']:+.2f}%): {status}")

    # 4. Get technical scores for top performers NOW
    print("\n📈 TECHNICAL SCORES ATTUALI PER TOP 5 GAINERS:")

    for p in top_10_gainers[:5]:
        coin = p['coin']
        try:
            pivot_data = await get_pivot_points(coin)
            if not pivot_data:
                print(f"\n  {coin}: ❌ NO PIVOT DATA (non analizzabile)")
                continue

            technical = calculate_technical_factors(pivot_data)

            print(f"\n  {coin} ({p['change_pct']:+.2f}% ultima ora, volume ${p['volume']:,.0f}):")
            print(f"    Technical Score: {technical.get('technical_score', 0):.3f}")
            print(f"    Momentum:        {technical.get('momentum', 0):.3f}")
            print(f"    Support:         {technical.get('support', 0):.3f}")
            print(f"    RSI:             {technical.get('rsi', 0):.1f}")

        except Exception as e:
            print(f"\n  {coin}: ❌ Error getting technical data - {str(e)[:50]}")

    # 5. Compare with DOOD and ZEC (what AI actually traded)
    print("\n⚖️  CONFRONTO CON LE POSIZIONI ATTUALI AI:")

    for pos in current_positions:
        coin = pos["position"]["coin"]
        try:
            # Find in performers list
            perf = next((p for p in performers if p['coin'] == coin), None)

            pivot_data = await get_pivot_points(coin)
            if not pivot_data:
                print(f"\n  {coin}: ❌ NO PIVOT DATA")
                continue

            technical = calculate_technical_factors(pivot_data)

            print(f"\n  {coin}:")
            if perf:
                print(f"    Performance ultima ora: {perf['change_pct']:+.2f}%")
                print(f"    Volume:                 ${perf['volume']:,.0f}")
            else:
                print(f"    Performance: N/A")
            print(f"    Technical Score: {technical.get('technical_score', 0):.3f}")
            print(f"    Momentum:        {technical.get('momentum', 0):.3f}")
            print(f"    Support:         {technical.get('support', 0):.3f}")
            print(f"    RSI:             {technical.get('rsi', 0):.1f}")

        except Exception as e:
            print(f"\n  {coin}: ❌ Error - {str(e)[:50]}")

    # 6. Summary analysis
    print("\n" + "=" * 80)
    print("💡 IPOTESI SUL PROBLEMA:")
    print("=" * 80)

    # Check how many top performers are even in AI's list
    analyzed_count = sum(1 for p in top_10_gainers if p['coin'] in ai_analyzed_symbols)

    print(f"\n1. COVERAGE: {analyzed_count}/10 top gainers sono tra i symbols analizzati dall'AI")
    if analyzed_count < 10:
        missing = [p['coin'] for p in top_10_gainers if p['coin'] not in ai_analyzed_symbols]
        print(f"   Missing: {', '.join(missing)}")

    print("\n2. TIMING: I gain potrebbero essere avvenuti DOPO l'ultima decisione AI")
    print("   (l'AI decide ogni 10 minuti, il momentum può cambiare velocemente)")

    print("\n3. TECHNICAL SCORE: L'AI potrebbe aver dato score bassi a coins che poi sono saliti")
    print("   (il technical score predice il futuro, ma non è perfetto)")

    print("\n4. FILTERING: Verifica se l'AI filtra per volume minimo/massimo")

    print("\n5. RISK AVERSION: L'AI potrebbe evitare coins con alta volatilità")

if __name__ == "__main__":
    asyncio.run(analyze_why_missing_top_performers())
