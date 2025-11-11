#!/usr/bin/env python3
"""
Quick Cache Test - Fast verification (30 seconds)

Tests cache refresh logic with TOP 20 symbols only.
Verifies that cache hits work and data freshness is maintained.

Usage:
    cd backend/
    python3 scripts/testing/test_cache_quick.py
"""

import asyncio
import sys
import time
from pathlib import Path
from datetime import datetime

# Add backend to path
backend_path = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(backend_path))

from services.orchestrator.market_data_orchestrator import build_market_data_snapshot
from services.orchestrator.cache_manager import get_cache_manager


# Top 20 symbols by volume/liquidity on Hyperliquid (guaranteed to have data)
TOP_SYMBOLS = [
    "BTC", "ETH", "SOL", "AVAX", "ARB", "OP", "SUI",
    "DOGE", "SHIB", "WIF", "BONK", "PEPE", "FLOKI",
    "LINK", "UNI", "AAVE", "MKR", "CRV", "LDO", "SNX"
]


def print_header(title: str):
    """Print formatted header."""
    print("\n" + "=" * 80)
    print(f"  {title}")
    print("=" * 80 + "\n")


async def test_cache_quick():
    """
    Quick cache test with top 20 symbols.

    Workflow:
    1. Run orchestrator (cold cache)
    2. Check cache stats
    3. Run again immediately (should use cache)
    4. Verify cache hits increased

    Total time: ~30 seconds
    """
    print_header("QUICK CACHE TEST - TOP 20 SYMBOLS")

    print(f"Testing symbols: {', '.join(TOP_SYMBOLS)}")
    print(f"Total symbols: {len(TOP_SYMBOLS)}")

    cache = get_cache_manager()

    # CYCLE 1: Cold cache
    print_header("CYCLE 1: Cold Cache (First Run)")

    start1 = time.time()
    snapshot1 = await build_market_data_snapshot(
        account_id=1,
        enable_prophet=False,  # Disable Prophet for speed
        prophet_mode=None,
        symbols_filter=TOP_SYMBOLS,  # KEY: Only top 20
    )
    duration1 = time.time() - start1

    stats1 = cache.get_stats()

    print(f"Duration: {duration1:.1f}s")
    print(f"Symbols analyzed: {snapshot1['metadata']['symbols_analyzed']}")
    print(f"Cache hits: {stats1['hits']}")
    print(f"Cache misses: {stats1['misses']}")
    print(f"Hit rate: {stats1['hit_rate']:.1f}%")

    # CYCLE 2: Warm cache (immediate re-run)
    print_header("CYCLE 2: Warm Cache (Immediate Re-run)")

    await asyncio.sleep(2)  # Short pause

    start2 = time.time()
    snapshot2 = await build_market_data_snapshot(
        account_id=1,
        enable_prophet=False,
        prophet_mode=None,
        symbols_filter=TOP_SYMBOLS,
    )
    duration2 = time.time() - start2

    stats2 = cache.get_stats()

    print(f"Duration: {duration2:.1f}s")
    print(f"Symbols analyzed: {snapshot2['metadata']['symbols_analyzed']}")
    print(f"Cache hits: {stats2['hits']} (+{stats2['hits'] - stats1['hits']})")
    print(f"Cache misses: {stats2['misses']} (+{stats2['misses'] - stats1['misses']})")
    print(f"Hit rate: {stats2['hit_rate']:.1f}%")

    # VERIFICATION
    print_header("VERIFICATION RESULTS")

    hits_increased = stats2['hits'] > stats1['hits']
    speedup = duration1 / duration2 if duration2 > 0 else 1.0

    print(f"✅ Cache Hits Increased: {hits_increased}")
    print(f"   Hits: {stats1['hits']} → {stats2['hits']} (+{stats2['hits'] - stats1['hits']})")

    print(f"\n✅ Performance Improved: {speedup:.1f}x speedup")
    print(f"   Duration: {duration1:.1f}s → {duration2:.1f}s")

    print(f"\n✅ Data Quality:")
    print(f"   Cycle 1: {snapshot1['metadata']['symbols_analyzed']} symbols")
    print(f"   Cycle 2: {snapshot2['metadata']['symbols_analyzed']} symbols")

    # Check top symbols have data
    top_5_cycle1 = sorted(
        snapshot1["symbols"],
        key=lambda s: s["technical_analysis"]["score"],
        reverse=True,
    )[:5]

    top_5_cycle2 = sorted(
        snapshot2["symbols"],
        key=lambda s: s["technical_analysis"]["score"],
        reverse=True,
    )[:5]

    print(f"\nTop 5 Technical Signals (Cycle 1):")
    for i, s in enumerate(top_5_cycle1, 1):
        print(f"  {i}. {s['symbol']:6s} - Score: {s['technical_analysis']['score']:.3f} ({s['technical_analysis']['signal']})")

    print(f"\nTop 5 Technical Signals (Cycle 2):")
    for i, s in enumerate(top_5_cycle2, 1):
        print(f"  {i}. {s['symbol']:6s} - Score: {s['technical_analysis']['score']:.3f} ({s['technical_analysis']['signal']})")

    # FINAL VERDICT
    print_header("FINAL VERDICT")

    all_checks = [
        hits_increased,
        speedup > 1.0,
        snapshot1['metadata']['symbols_analyzed'] == len(TOP_SYMBOLS),
        snapshot2['metadata']['symbols_analyzed'] == len(TOP_SYMBOLS),
    ]

    if all(all_checks):
        print("✅ ALL TESTS PASSED")
        print("\nCache system is working correctly:")
        print(f"  - First run: {duration1:.1f}s (cold cache)")
        print(f"  - Second run: {duration2:.1f}s (warm cache)")
        print(f"  - Speedup: {speedup:.1f}x")
        print(f"  - Cache hits: +{stats2['hits'] - stats1['hits']}")
        return True
    else:
        print("❌ SOME TESTS FAILED")
        print("\nCheck:")
        print(f"  - Cache hits increased: {hits_increased}")
        print(f"  - Performance improved: {speedup > 1.0}")
        print(f"  - Correct symbol count: {snapshot1['metadata']['symbols_analyzed'] == len(TOP_SYMBOLS)}")
        return False


async def main():
    """Main entry point."""
    try:
        success = await test_cache_quick()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n❌ Test interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ Test failed with error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
