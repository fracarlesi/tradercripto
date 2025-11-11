#!/usr/bin/env python3
"""
Test Orchestrator - 10 Minute Production Run

This script runs the orchestrator for 10 minutes to verify:
1. JSON generation works correctly
2. All microservices are functioning
3. Output quality and completeness
4. Performance metrics

Usage:
    cd backend
    python scripts/testing/test_orchestrator_10min.py

Options:
    --cycles N          Number of cycles to run (default: 3)
    --save-json         Save JSON output to file
    --show-full-json    Print complete JSON (warning: large!)
    --enable-prophet    Enable Prophet forecasts (slower)
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
from services.orchestrator.schemas import validate_snapshot

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def print_header(title: str):
    """Print formatted header."""
    print("\n" + "=" * 80)
    print(f"  {title}")
    print("=" * 80 + "\n")


def print_section(title: str):
    """Print formatted section."""
    print("\n" + "-" * 80)
    print(f"  {title}")
    print("-" * 80)


def format_json_sample(data: dict, max_items: int = 3) -> str:
    """Format JSON with limited array items."""
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            if isinstance(value, list) and len(value) > max_items:
                result[key] = value[:max_items] + [f"... ({len(value) - max_items} more)"]
            elif isinstance(value, dict):
                result[key] = format_json_sample(value, max_items)
            else:
                result[key] = value
        return result
    return data


async def run_single_cycle(
    account_id: int,
    enable_prophet: bool,
    cycle_num: int,
    save_json: bool = False,
    show_full: bool = False,
):
    """
    Run a single orchestrator cycle and display results.

    Args:
        account_id: Account ID to use
        enable_prophet: Enable Prophet forecasting
        cycle_num: Cycle number (for display)
        save_json: Save JSON to file
        show_full: Show complete JSON

    Returns:
        Tuple of (snapshot, duration_seconds)
    """
    print_header(f"CYCLE #{cycle_num} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    start = time.time()

    try:
        # Build snapshot
        logger.info(f"Starting orchestrator cycle #{cycle_num}...")
        snapshot = await build_market_data_snapshot(
            account_id=account_id,
            enable_prophet=enable_prophet,
            prophet_mode="lite" if enable_prophet else None,
        )

        duration = time.time() - start

        # Validate
        validate_snapshot(snapshot)

        # Display results
        print_section("CYCLE SUMMARY")
        print(f"✅ Status: SUCCESS")
        print(f"⏱️  Duration: {duration:.1f} seconds")
        print(f"📊 Symbols analyzed: {snapshot['metadata']['symbols_analyzed']}")
        print(f"💾 Cache hit rate: {snapshot['metadata']['cache_hit_rate']:.1%}")
        print(f"🔮 Prophet enabled: {'Yes' if enable_prophet else 'No'}")

        # Display top symbols
        print_section("TOP 5 TECHNICAL SIGNALS")
        top_symbols = sorted(
            snapshot["symbols"],
            key=lambda s: s["technical_analysis"]["score"],
            reverse=True,
        )[:5]

        for i, symbol_data in enumerate(top_symbols, 1):
            symbol = symbol_data["symbol"]
            ta = symbol_data["technical_analysis"]
            price = symbol_data["price"]
            pivot = symbol_data["pivot_points"]

            prophet_str = ""
            if symbol_data["prophet_forecast"]:
                pf = symbol_data["prophet_forecast"]
                prophet_str = f" | Prophet: {pf['trend']} ({pf['change_pct_24h']:+.2f}%)"

            print(
                f"  {i}. {symbol:6s} ${price:>10,.2f} - "
                f"Score: {ta['score']:.3f} ({ta['signal']:12s}) - "
                f"Pivot: {pivot['current_zone']:8s}{prophet_str}"
            )

        # Display portfolio
        print_section("PORTFOLIO STATE")
        portfolio = snapshot["portfolio"]
        print(f"  Total Assets:    ${portfolio['total_assets']:>10,.2f}")
        print(f"  Available Cash:  ${portfolio['available_cash']:>10,.2f}")
        print(f"  Positions Value: ${portfolio['positions_value']:>10,.2f}")
        print(f"  Unrealized P&L:  ${portfolio['unrealized_pnl']:>10,.2f}")
        print(f"  Active Positions: {len(portfolio['positions'])}")

        if portfolio["positions"]:
            print("\n  Positions:")
            for pos in portfolio["positions"]:
                pnl_color = "+" if pos["unrealized_pnl"] >= 0 else ""
                print(
                    f"    • {pos['symbol']:6s} {pos['side']:5s}: "
                    f"${pos['market_value']:>8,.2f} "
                    f"(P&L: {pnl_color}{pos['unrealized_pnl_pct']:>6.2f}%)"
                )

        # Display global indicators
        print_section("GLOBAL INDICATORS")
        sentiment = snapshot["global_indicators"]["sentiment"]
        whale_alerts = snapshot["global_indicators"]["whale_alerts"]
        news = snapshot["global_indicators"]["news"]

        print(f"  Sentiment: {sentiment['label']:15s} ({sentiment['value']}/100)")
        print(f"  Signal:    {sentiment['signal']}")
        print(f"  Whale Alerts: {len(whale_alerts)} recent transactions")
        print(f"  News Headlines: {len(news)} articles")

        if news:
            print("\n  Latest Headlines:")
            for i, article in enumerate(news[:3], 1):
                headline = article["headline"][:70]
                print(f"    {i}. {headline}...")

        # Display JSON sample
        print_section("JSON STRUCTURE (Sample)")

        # Show structure with first 2 symbols only
        json_sample = {
            "metadata": snapshot["metadata"],
            "symbols": snapshot["symbols"][:2],  # First 2 symbols
            "global_indicators": {
                "sentiment": sentiment,
                "whale_alerts": whale_alerts[:2] if whale_alerts else [],
                "news": news[:2] if news else [],
            },
            "portfolio": {
                "total_assets": portfolio["total_assets"],
                "available_cash": portfolio["available_cash"],
                "positions": portfolio["positions"][:2] if portfolio["positions"] else [],
            },
        }

        print(json.dumps(json_sample, indent=2, default=str))
        print(f"\n  ... (showing 2/{len(snapshot['symbols'])} symbols)")

        # Save full JSON if requested
        if save_json:
            filename = f"market_snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            filepath = backend_path / "output" / filename
            filepath.parent.mkdir(exist_ok=True)

            with open(filepath, "w") as f:
                json.dump(snapshot, f, indent=2, default=str)

            print(f"\n📁 Full JSON saved to: {filepath}")

        # Show full JSON if requested
        if show_full:
            print_section("COMPLETE JSON OUTPUT")
            print(json.dumps(snapshot, indent=2, default=str))

        return snapshot, duration

    except Exception as e:
        duration = time.time() - start
        logger.error(f"❌ Cycle #{cycle_num} FAILED after {duration:.1f}s: {e}", exc_info=True)
        print_section("ERROR")
        print(f"❌ Cycle #{cycle_num} FAILED")
        print(f"⏱️  Duration: {duration:.1f} seconds")
        print(f"🚨 Error: {str(e)}")
        return None, duration


async def run_test(
    account_id: int = 1,
    cycles: int = 3,
    enable_prophet: bool = False,
    save_json: bool = False,
    show_full: bool = False,
):
    """
    Run orchestrator test for multiple cycles.

    Args:
        account_id: Account ID to use
        cycles: Number of cycles to run
        enable_prophet: Enable Prophet forecasting
        save_json: Save JSON output to files
        show_full: Show complete JSON for each cycle

    Returns:
        True if all cycles succeeded, False otherwise
    """
    print_header("ORCHESTRATOR 10-MINUTE PRODUCTION TEST")
    print(f"Configuration:")
    print(f"  Account ID: {account_id}")
    print(f"  Cycles: {cycles}")
    print(f"  Prophet: {'ENABLED' if enable_prophet else 'DISABLED'}")
    print(f"  Save JSON: {'Yes' if save_json else 'No'}")
    print(f"  Show Full JSON: {'Yes' if show_full else 'No'}")
    print(f"  Estimated duration: {cycles * 90 // 60}-{cycles * 150 // 60} minutes")

    results = []
    total_start = time.time()

    for cycle_num in range(1, cycles + 1):
        snapshot, duration = await run_single_cycle(
            account_id=account_id,
            enable_prophet=enable_prophet,
            cycle_num=cycle_num,
            save_json=save_json,
            show_full=show_full,
        )

        results.append({
            "cycle": cycle_num,
            "success": snapshot is not None,
            "duration": duration,
            "symbols": len(snapshot["symbols"]) if snapshot else 0,
        })

        # Wait before next cycle (simulate production interval)
        if cycle_num < cycles:
            wait_time = 30  # 30 seconds between cycles for testing
            print(f"\n⏳ Waiting {wait_time}s before next cycle...")
            await asyncio.sleep(wait_time)

    total_duration = time.time() - total_start

    # Final summary
    print_header("TEST SUMMARY")

    successful = sum(1 for r in results if r["success"])
    failed = cycles - successful

    print(f"Total Cycles: {cycles}")
    print(f"Successful: {successful} ✅")
    print(f"Failed: {failed} {'❌' if failed > 0 else ''}")
    print(f"Total Duration: {total_duration:.1f}s ({total_duration / 60:.1f} minutes)")

    if results:
        print("\nCycle Performance:")
        print(f"  Average: {sum(r['duration'] for r in results) / len(results):.1f}s")
        print(f"  Fastest: {min(r['duration'] for r in results):.1f}s")
        print(f"  Slowest: {max(r['duration'] for r in results):.1f}s")

        # Show cache improvement
        if len(results) > 1:
            speedup = results[0]["duration"] / results[-1]["duration"]
            print(f"  Speedup (first → last): {speedup:.1f}x")

    print("\nPer-Cycle Results:")
    for r in results:
        status = "✅ SUCCESS" if r["success"] else "❌ FAILED"
        print(f"  Cycle {r['cycle']}: {status} - {r['duration']:.1f}s - {r['symbols']} symbols")

    # Recommendations
    print_header("RECOMMENDATIONS")

    avg_duration = sum(r['duration'] for r in results) / len(results) if results else 0

    if avg_duration < 90:
        print("✅ Performance is EXCELLENT (< 90s average)")
    elif avg_duration < 120:
        print("✅ Performance is GOOD (90-120s average)")
    elif avg_duration < 180:
        print("⚠️  Performance is ACCEPTABLE (120-180s average)")
    else:
        print("🚨 Performance is SLOW (> 180s average)")
        print("   Consider:")
        print("   - Disabling Prophet (use --no-prophet)")
        print("   - Checking network latency")
        print("   - Reviewing Hyperliquid API performance")

    if failed > 0:
        print(f"\n🚨 {failed} cycles FAILED - Review error logs above")
        return False

    print("\n✅ All cycles completed successfully!")
    print("\n🚀 System is READY for production deployment")

    return True


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Test orchestrator for 10 minutes")
    parser.add_argument(
        "--account-id",
        type=int,
        default=1,
        help="Account ID to test (default: 1)",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=3,
        help="Number of cycles to run (default: 3)",
    )
    parser.add_argument(
        "--enable-prophet",
        action="store_true",
        help="Enable Prophet forecasts (slower)",
    )
    parser.add_argument(
        "--save-json",
        action="store_true",
        help="Save JSON output to files",
    )
    parser.add_argument(
        "--show-full-json",
        action="store_true",
        help="Print complete JSON for each cycle",
    )

    args = parser.parse_args()

    # Run test
    success = await run_test(
        account_id=args.account_id,
        cycles=args.cycles,
        enable_prophet=args.enable_prophet,
        save_json=args.save_json,
        show_full=args.show_full_json,
    )

    # Exit code
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
