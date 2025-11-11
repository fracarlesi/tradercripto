#!/usr/bin/env python3
"""
Test - 20 Symbols + Prophet Forecasting

Tests complete JSON generation with:
- 20 most liquid symbols
- Prophet forecasting enabled (LITE mode)
- All microservices active

Usage:
    cd backend/
    python3 scripts/testing/test_20symbols_prophet.py
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from datetime import datetime

# Add backend to path
backend_path = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(backend_path))

from services.orchestrator.market_data_orchestrator import build_market_data_snapshot
from services.orchestrator.cache_manager import get_cache_manager


# Top 20 symbols by volume/liquidity
TOP_20_SYMBOLS = [
    "BTC", "ETH", "SOL", "AVAX", "ARB", "OP", "SUI",
    "DOGE", "SHIB", "WIF", "BONK", "PEPE", "FLOKI",
    "LINK", "UNI", "AAVE", "CRV", "LDO", "MATIC", "ATOM"
]


def print_header(title: str):
    """Print formatted header."""
    print("\n" + "=" * 80)
    print(f"  {title}")
    print("=" * 80 + "\n")


async def test_with_prophet():
    """
    Test JSON generation with 20 symbols + Prophet.

    Expected duration:
    - First run: ~60-90s (Prophet cache miss + 20 symbols)
    - Second run: ~15-20s (full cache hit)
    """
    print_header("TEST - 20 SYMBOLS + PROPHET FORECASTING")

    print(f"Symbols: {len(TOP_20_SYMBOLS)}")
    print(f"Prophet: ENABLED (LITE mode - 7 days training)")
    print(f"Expected first run: 60-90 seconds")
    print(f"Expected second run: 15-20 seconds (cached)\n")

    cache = get_cache_manager()

    # ==============================================
    # RUN 1: Cold Cache (with Prophet)
    # ==============================================
    print_header("RUN 1: Cold Cache (First Run with Prophet)")
    print("⏳ This may take 60-90 seconds for Prophet training...")

    start1 = time.time()

    try:
        snapshot1 = await build_market_data_snapshot(
            account_id=1,
            enable_prophet=True,  # ✅ Enable Prophet
            prophet_mode="lite",   # 7 days training
            symbols_filter=TOP_20_SYMBOLS,
        )
        duration1 = time.time() - start1

        print(f"\n✅ Status: SUCCESS")
        print(f"⏱️  Duration: {duration1:.1f}s")
        print(f"📊 Symbols in snapshot: {len(snapshot1.get('symbols', []))}")

        stats1 = cache.get_stats()
        print(f"💾 Cache hits: {stats1['hits']}")
        print(f"💾 Cache misses: {stats1['misses']}")
        print(f"💾 Hit rate: {stats1['hit_rate']:.1f}%")

        # Count Prophet forecasts
        prophet_count = sum(1 for s in snapshot1.get("symbols", []) if s.get("prophet_forecast"))
        print(f"🔮 Prophet forecasts: {prophet_count}/{len(snapshot1.get('symbols', []))}")

    except Exception as e:
        print(f"\n❌ FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False

    # Display Prophet sample
    if snapshot1.get("symbols"):
        for symbol_data in snapshot1["symbols"]:
            if symbol_data.get("prophet_forecast"):
                pf = symbol_data["prophet_forecast"]
                print(f"\n📊 Sample Prophet Forecast ({symbol_data['symbol']}):")
                print(f"  trend: {pf.get('trend', 'N/A')}")
                print(f"  change_pct_24h: {pf.get('change_pct_24h', 'N/A'):+.2f}%")
                print(f"  confidence: {pf.get('confidence', 'N/A')}")
                print(f"  forecast_price: ${pf.get('forecast_price', 'N/A')}")
                break

    # ==============================================
    # RUN 2: Warm Cache (should be MUCH faster)
    # ==============================================
    print_header("RUN 2: Warm Cache (Immediate Re-run)")
    print("⏳ Should be ~15-20s with full cache...")

    await asyncio.sleep(3)  # Short delay

    start2 = time.time()

    try:
        snapshot2 = await build_market_data_snapshot(
            account_id=1,
            enable_prophet=True,
            prophet_mode="lite",
            symbols_filter=TOP_20_SYMBOLS,
        )
        duration2 = time.time() - start2

        print(f"\n✅ Status: SUCCESS")
        print(f"⏱️  Duration: {duration2:.1f}s")
        print(f"📊 Symbols in snapshot: {len(snapshot2.get('symbols', []))}")

        stats2 = cache.get_stats()
        print(f"💾 Cache hits: {stats2['hits']} (+{stats2['hits'] - stats1['hits']})")
        print(f"💾 Cache misses: {stats2['misses']} (+{stats2['misses'] - stats1['misses']})")
        print(f"💾 Hit rate: {stats2['hit_rate']:.1f}%")

        speedup = duration1 / duration2 if duration2 > 0 else 1.0
        print(f"\n⚡ Speedup: {speedup:.1f}x faster")

        prophet_count2 = sum(1 for s in snapshot2.get("symbols", []) if s.get("prophet_forecast"))
        print(f"🔮 Prophet forecasts: {prophet_count2}/{len(snapshot2.get('symbols', []))}")

    except Exception as e:
        print(f"\n❌ FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False

    # ==============================================
    # SAVE JSON OUTPUT
    # ==============================================
    output_dir = backend_path / "output"
    output_dir.mkdir(exist_ok=True)

    filename = f"market_snapshot_20symbols_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    filepath = output_dir / filename

    with open(filepath, "w") as f:
        json.dump(snapshot2, f, indent=2, default=str)

    print(f"\n📁 JSON saved to: {filepath}")
    print(f"📊 File size: {filepath.stat().st_size / 1024:.1f} KB")

    # ==============================================
    # VERIFICATION
    # ==============================================
    print_header("VERIFICATION RESULTS")

    checks = {
        "Run 1 completed": snapshot1 is not None,
        "Run 2 completed": snapshot2 is not None,
        "Expected symbols count": len(snapshot2.get("symbols", [])) >= 15,  # Allow some to fail
        "Prophet forecasts present": prophet_count2 > 0,
        "Cache hits increased": stats2['hits'] > stats1['hits'],
        "Performance improved": duration2 < duration1,
        "First run <120s": duration1 < 120,  # Reasonable time
        "Second run <30s": duration2 < 30,   # Cache should speed up
    }

    for check, passed in checks.items():
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {status} - {check}")

    all_passed = all(checks.values())

    # ==============================================
    # TOP SIGNALS
    # ==============================================
    print_header("TOP 5 TECHNICAL SIGNALS")

    top_symbols = sorted(
        snapshot2["symbols"],
        key=lambda s: s["technical_analysis"]["score"],
        reverse=True,
    )[:5]

    for i, s in enumerate(top_symbols, 1):
        ta = s["technical_analysis"]
        pivot = s.get("pivot_points", {})
        prophet = s.get("prophet_forecast", {})

        prophet_str = ""
        if prophet:
            prophet_str = f" | Prophet: {prophet.get('trend', 'N/A')} ({prophet.get('change_pct_24h', 0):+.1f}%)"

        print(
            f"  {i}. {s['symbol']:6s} ${s['price']:>10,.2f} - "
            f"Score: {ta['score']:.3f} ({ta['signal']:12s}) - "
            f"Pivot: {pivot.get('current_zone', 'N/A'):8s}{prophet_str}"
        )

    # ==============================================
    # FINAL VERDICT
    # ==============================================
    print_header("FINAL VERDICT")

    if all_passed:
        print("✅ ALL TESTS PASSED")
        print("\nSystem is working correctly with 20 symbols + Prophet:")
        print(f"  - First run: {duration1:.1f}s (cold cache)")
        print(f"  - Second run: {duration2:.1f}s (warm cache)")
        print(f"  - Speedup: {speedup:.1f}x")
        print(f"  - Symbols analyzed: {len(snapshot2.get('symbols', []))}")
        print(f"  - Prophet forecasts: {prophet_count2}")
        print(f"  - JSON size: {filepath.stat().st_size / 1024:.1f} KB")
        print("\n🚀 READY FOR PRODUCTION DEPLOYMENT!")
        return True
    else:
        print("❌ SOME TESTS FAILED")
        print("\nIssues detected:")
        for check, passed in checks.items():
            if not passed:
                print(f"  - {check}")
        return False


async def main():
    """Main entry point."""
    try:
        success = await test_with_prophet()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n❌ Test interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ Test failed with unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
