#!/usr/bin/env python3
"""
Minimal Test - 5 Core Symbols Only

Tests JSON generation with the 5 most liquid symbols on Hyperliquid.
This is a sanity check to verify the orchestrator works before scaling up.

Usage:
    cd backend/
    python3 scripts/testing/test_minimal_5symbols.py
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


# Core symbols with guaranteed data availability
CORE_SYMBOLS = ["BTC", "ETH", "SOL", "AVAX", "ARB"]


def print_header(title: str):
    """Print formatted header."""
    print("\n" + "=" * 80)
    print(f"  {title}")
    print("=" * 80 + "\n")


async def test_minimal_generation():
    """
    Test JSON generation with 5 core symbols.

    Verifies:
    1. Orchestrator completes without errors
    2. JSON structure is valid
    3. All 5 symbols have complete data
    4. Cache system works (2nd run faster)
    """
    print_header("MINIMAL TEST - 5 CORE SYMBOLS")

    print(f"Testing symbols: {', '.join(CORE_SYMBOLS)}")
    print(f"Total symbols: {len(CORE_SYMBOLS)}\n")

    cache = get_cache_manager()

    # ==============================================
    # RUN 1: Cold Cache
    # ==============================================
    print_header("RUN 1: Cold Cache (First Execution)")

    start1 = time.time()

    try:
        snapshot1 = await build_market_data_snapshot(
            account_id=1,
            enable_prophet=False,  # Disable Prophet for speed
            prophet_mode=None,
            symbols_filter=CORE_SYMBOLS,
        )
        duration1 = time.time() - start1

        print(f"✅ Status: SUCCESS")
        print(f"⏱️  Duration: {duration1:.1f}s")
        print(f"📊 Symbols in snapshot: {len(snapshot1.get('symbols', []))}")

        stats1 = cache.get_stats()
        print(f"💾 Cache hits: {stats1['hits']}")
        print(f"💾 Cache misses: {stats1['misses']}")
        print(f"💾 Hit rate: {stats1['hit_rate']:.1f}%")

    except Exception as e:
        print(f"❌ FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False

    # Verify all symbols present
    symbols_in_snapshot = {s["symbol"] for s in snapshot1.get("symbols", [])}
    missing_symbols = set(CORE_SYMBOLS) - symbols_in_snapshot

    if missing_symbols:
        print(f"\n⚠️  WARNING: Missing symbols: {missing_symbols}")
    else:
        print(f"\n✅ All {len(CORE_SYMBOLS)} symbols present in snapshot")

    # Display snapshot structure
    print("\n📋 Snapshot Structure:")
    print(f"  metadata: {list(snapshot1.get('metadata', {}).keys())}")
    print(f"  symbols: {len(snapshot1.get('symbols', []))} items")
    print(f"  global_indicators: {list(snapshot1.get('global_indicators', {}).keys())}")
    print(f"  portfolio: {list(snapshot1.get('portfolio', {}).keys())}")

    # Display first symbol details
    if snapshot1.get("symbols"):
        first_symbol = snapshot1["symbols"][0]
        print(f"\n📊 Sample Symbol Data ({first_symbol['symbol']}):")
        print(f"  price: ${first_symbol.get('price', 'N/A')}")
        print(f"  technical_analysis: {list(first_symbol.get('technical_analysis', {}).keys())}")
        print(f"  pivot_points: {list(first_symbol.get('pivot_points', {}).keys()) if first_symbol.get('pivot_points') else 'None'}")
        print(f"  prophet_forecast: {first_symbol.get('prophet_forecast', 'None')}")

    # ==============================================
    # RUN 2: Warm Cache
    # ==============================================
    print_header("RUN 2: Warm Cache (Immediate Re-run)")

    await asyncio.sleep(2)  # Short delay

    start2 = time.time()

    try:
        snapshot2 = await build_market_data_snapshot(
            account_id=1,
            enable_prophet=False,
            prophet_mode=None,
            symbols_filter=CORE_SYMBOLS,
        )
        duration2 = time.time() - start2

        print(f"✅ Status: SUCCESS")
        print(f"⏱️  Duration: {duration2:.1f}s")
        print(f"📊 Symbols in snapshot: {len(snapshot2.get('symbols', []))}")

        stats2 = cache.get_stats()
        print(f"💾 Cache hits: {stats2['hits']} (+{stats2['hits'] - stats1['hits']})")
        print(f"💾 Cache misses: {stats2['misses']} (+{stats2['misses'] - stats1['misses']})")
        print(f"💾 Hit rate: {stats2['hit_rate']:.1f}%")

        speedup = duration1 / duration2 if duration2 > 0 else 1.0
        print(f"\n⚡ Speedup: {speedup:.1f}x faster")

    except Exception as e:
        print(f"❌ FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False

    # ==============================================
    # VERIFICATION
    # ==============================================
    print_header("VERIFICATION RESULTS")

    checks = {
        "Run 1 completed": snapshot1 is not None,
        "Run 2 completed": snapshot2 is not None,
        "All symbols present (Run 1)": len(missing_symbols) == 0,
        "All symbols present (Run 2)": len(snapshot2.get("symbols", [])) == len(CORE_SYMBOLS),
        "Cache hits increased": stats2['hits'] > stats1['hits'],
        "Performance improved": duration2 < duration1,
    }

    for check, passed in checks.items():
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {status} - {check}")

    all_passed = all(checks.values())

    # ==============================================
    # FINAL VERDICT
    # ==============================================
    print_header("FINAL VERDICT")

    if all_passed:
        print("✅ ALL TESTS PASSED")
        print("\nSystem is working correctly with 5 core symbols:")
        print(f"  - First run: {duration1:.1f}s")
        print(f"  - Second run: {duration2:.1f}s (cache working!)")
        print(f"  - Symbols analyzed: {len(CORE_SYMBOLS)}")
        print("\n🚀 Ready to expand to more symbols!")
        return True
    else:
        print("❌ SOME TESTS FAILED")
        print("\nIssues detected:")
        for check, passed in checks.items():
            if not passed:
                print(f"  - {check}")
        print("\n⚠️  Fix these issues before expanding to more symbols")
        return False


async def main():
    """Main entry point."""
    try:
        success = await test_minimal_generation()
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
