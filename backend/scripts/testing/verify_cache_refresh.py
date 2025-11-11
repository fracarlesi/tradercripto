#!/usr/bin/env python3
"""
Cache Refresh Verification Script

Verifies that cached data is refreshed according to TTL:
- Technical Analysis: 180s (3 min)
- Pivot Points: 3600s (1 hour)
- Prophet Forecasts: 86400s (24 hours)
- Sentiment: 3600s (1 hour)

This script:
1. Runs orchestrator and captures cache timestamps
2. Waits for TTL to expire
3. Runs again and verifies data was re-fetched
4. Reports which caches are working correctly

Usage:
    python scripts/testing/verify_cache_refresh.py

Options:
    --quick     Test only technical cache (3min wait)
    --medium    Test technical + pivot (1h wait)
    --full      Test all including prophet (24h wait)
"""

import asyncio
import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

# Add backend to path
backend_path = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(backend_path))

from services.orchestrator.market_data_orchestrator import build_market_data_snapshot
from services.orchestrator.cache_manager import get_cache_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def print_header(title: str):
    """Print formatted header."""
    print("\n" + "=" * 80)
    print(f"  {title}")
    print("=" * 80 + "\n")


def print_result(name: str, status: str, details: str = ""):
    """Print test result."""
    emoji = "✅" if status == "PASS" else "❌" if status == "FAIL" else "⏭️"
    print(f"{emoji} {name:30s} {status:10s} {details}")


async def get_cache_stats():
    """Get current cache statistics."""
    cache = get_cache_manager()
    stats = cache.get_stats()

    # Get individual cache ages
    cache_ages = {}
    for cache_type in ["technical_analysis", "pivot_points", "prophet_forecasts"]:
        if cache._cache.get(cache_type):
            # Get age of first cached item
            first_key = next(iter(cache._cache[cache_type].keys()), None)
            if first_key:
                cached_data = cache._cache[cache_type][first_key]
                age = time.time() - cached_data["timestamp"]
                cache_ages[cache_type] = age
            else:
                cache_ages[cache_type] = None
        else:
            cache_ages[cache_type] = None

    return stats, cache_ages


async def run_orchestrator_cycle(cycle_num: int):
    """Run single orchestrator cycle and return snapshot."""
    logger.info(f"Running cycle #{cycle_num}...")

    snapshot = await build_market_data_snapshot(
        account_id=1,
        enable_prophet=True,
        prophet_mode="lite"
    )

    stats, ages = await get_cache_stats()

    return snapshot, stats, ages


async def verify_cache_refresh(test_mode: str = "quick"):
    """
    Verify cache refresh behavior.

    Args:
        test_mode: "quick" (3min), "medium" (1h), or "full" (24h)
    """
    print_header("CACHE REFRESH VERIFICATION TEST")

    # Test configuration
    tests = {
        "quick": {
            "name": "Quick Test (Technical Analysis)",
            "wait_time": 200,  # 3min 20s (slightly over 180s TTL)
            "caches_to_test": ["technical_analysis"],
        },
        "medium": {
            "name": "Medium Test (Technical + Pivot)",
            "wait_time": 3700,  # 1h 1min 40s (slightly over 3600s TTL)
            "caches_to_test": ["technical_analysis", "pivot_points"],
        },
        "full": {
            "name": "Full Test (All Caches)",
            "wait_time": 86500,  # 24h 1min 40s (slightly over 86400s TTL)
            "caches_to_test": ["technical_analysis", "pivot_points", "prophet_forecasts"],
        },
    }

    config = tests[test_mode]
    print(f"Test Mode: {config['name']}")
    print(f"Wait Time: {config['wait_time']}s ({config['wait_time'] / 60:.1f} min)")
    print(f"Caches to Test: {', '.join(config['caches_to_test'])}")

    # CYCLE 1: Initial run (cold cache)
    print_header("CYCLE 1: Initial Run (Cold Cache)")

    snapshot1, stats1, ages1 = await run_orchestrator_cycle(1)

    print(f"Cache Stats:")
    print(f"  Hits: {stats1['hits']}")
    print(f"  Misses: {stats1['misses']}")
    print(f"  Hit Rate: {stats1['hit_rate']:.1f}%")

    print(f"\nCache Ages:")
    for cache_type, age in ages1.items():
        if age is not None:
            print(f"  {cache_type:25s}: {age:.1f}s")
        else:
            print(f"  {cache_type:25s}: NOT CACHED")

    # Store baseline data for comparison
    baseline = {
        "symbols": snapshot1["symbols"][:5],  # First 5 symbols
        "timestamp": time.time(),
    }

    # CYCLE 2: Immediate run (should use cache)
    print_header("CYCLE 2: Immediate Run (Should Use Cache)")

    await asyncio.sleep(5)  # Small delay

    snapshot2, stats2, ages2 = await run_orchestrator_cycle(2)

    print(f"Cache Stats:")
    print(f"  Hits: {stats2['hits']} (+{stats2['hits'] - stats1['hits']})")
    print(f"  Misses: {stats2['misses']} (+{stats2['misses'] - stats1['misses']})")
    print(f"  Hit Rate: {stats2['hit_rate']:.1f}%")

    # Verify cache was used
    hits_increased = stats2['hits'] > stats1['hits']
    print_result(
        "Cache Used",
        "PASS" if hits_increased else "FAIL",
        f"Hits: {stats1['hits']} → {stats2['hits']}"
    )

    # CYCLE 3: Wait for TTL expiration
    print_header(f"WAITING {config['wait_time']}s for Cache Expiration...")

    print(f"Started waiting at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Expected completion: {datetime.fromtimestamp(time.time() + config['wait_time']).strftime('%Y-%m-%d %H:%M:%S')}")

    # Wait with progress updates
    elapsed = 0
    update_interval = min(60, config['wait_time'] // 10)  # Update every minute or 10% of wait time

    while elapsed < config['wait_time']:
        remaining = config['wait_time'] - elapsed
        pct = (elapsed / config['wait_time']) * 100
        print(f"  Progress: {pct:5.1f}% | Elapsed: {elapsed:5d}s | Remaining: {remaining:5d}s", end='\r')

        sleep_time = min(update_interval, remaining)
        await asyncio.sleep(sleep_time)
        elapsed += sleep_time

    print("\n")  # New line after progress

    # CYCLE 4: After TTL expiration (should re-fetch)
    print_header("CYCLE 3: After TTL Expiration (Should Re-fetch)")

    snapshot3, stats3, ages3 = await run_orchestrator_cycle(3)

    print(f"Cache Stats:")
    print(f"  Hits: {stats3['hits']}")
    print(f"  Misses: {stats3['misses']}")
    print(f"  Hit Rate: {stats3['hit_rate']:.1f}%")

    print(f"\nCache Ages:")
    for cache_type, age in ages3.items():
        if age is not None:
            print(f"  {cache_type:25s}: {age:.1f}s")
        else:
            print(f"  {cache_type:25s}: NOT CACHED")

    # VERIFICATION
    print_header("VERIFICATION RESULTS")

    results = []

    # Verify each cache type
    for cache_type in config['caches_to_test']:
        age_before = ages2.get(cache_type)
        age_after = ages3.get(cache_type)

        if age_before is None or age_after is None:
            print_result(cache_type, "SKIP", "Cache data not available")
            results.append(False)
            continue

        # Age should be LESS after re-fetch (reset to near 0)
        was_refreshed = age_after < age_before

        if was_refreshed:
            print_result(
                cache_type,
                "PASS",
                f"Age: {age_before:.0f}s → {age_after:.0f}s (refreshed)"
            )
            results.append(True)
        else:
            print_result(
                cache_type,
                "FAIL",
                f"Age: {age_before:.0f}s → {age_after:.0f}s (NOT refreshed!)"
            )
            results.append(False)

    # Data comparison (optional)
    print("\nData Changes:")

    # Compare top 3 symbols' technical scores
    for i in range(3):
        sym1 = baseline["symbols"][i]
        sym3 = snapshot3["symbols"][i]

        score_before = sym1["technical_analysis"]["score"]
        score_after = sym3["technical_analysis"]["score"]
        score_diff = abs(score_after - score_before)

        changed = "✓ Changed" if score_diff > 0.001 else "✗ Same"
        print(f"  {sym1['symbol']:6s} Score: {score_before:.3f} → {score_after:.3f} ({changed})")

    # Final verdict
    print_header("FINAL VERDICT")

    all_passed = all(results)

    if all_passed:
        print("✅ ALL TESTS PASSED - Cache refresh is working correctly!")
    else:
        print("❌ SOME TESTS FAILED - Cache refresh may not be working!")
        print("\nRecommendations:")
        print("  1. Check cache TTL settings in cache_manager.py")
        print("  2. Verify cache timestamps are being updated")
        print("  3. Review cache invalidation logic")

    return all_passed


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Verify cache refresh behavior")
    parser.add_argument(
        "--mode",
        choices=["quick", "medium", "full"],
        default="quick",
        help="Test mode: quick (3min), medium (1h), full (24h)",
    )

    args = parser.parse_args()

    try:
        success = await verify_cache_refresh(args.mode)
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n❌ Test interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Test failed with error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
