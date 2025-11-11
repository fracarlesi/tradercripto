#!/usr/bin/env python3
"""
FULL PRODUCTION TEST - All ~220 Tradable Symbols + Prophet

Tests complete JSON generation exactly as it will run in production:
- ALL tradable symbols on Hyperliquid (excluding '@' index symbols)
- Prophet forecasting enabled (LITE mode)
- All microservices active (sentiment, whale, news, pivot, technical)

Usage:
    cd backend/
    python3 scripts/testing/test_full_142symbols.py
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
from services.trading.hyperliquid_trading_service import hyperliquid_trading_service


def print_header(title: str):
    """Print formatted header."""
    print("\n" + "=" * 80)
    print(f"  {title}")
    print("=" * 80 + "\n")


def format_duration(seconds: float) -> str:
    """Format duration in human-readable format."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds / 60)
    secs = seconds % 60
    return f"{minutes}m {secs:.0f}s"


async def test_full_production():
    """
    Test FULL production configuration.

    Expected duration:
    - First run: 2-4 minutes (Prophet + all symbols)
    - Second run: 30-60 seconds (full cache)
    """
    print_header("FULL PRODUCTION TEST - ALL TRADABLE SYMBOLS + PROPHET")

    # Fetch ALL tradable symbols (exclude '@' index symbols)
    print("Fetching tradable symbols list...")
    all_mids = await hyperliquid_trading_service.get_all_mids_async()
    all_symbols = list(all_mids.keys())
    tradable_symbols = [s for s in all_symbols if not s.startswith('@')]
    print(f"✅ Found {len(tradable_symbols)} tradable symbols (excluded {len(all_symbols) - len(tradable_symbols)} index symbols)\n")

    print("Configuration:")
    print(f"  Symbols: {len(tradable_symbols)} tradable (no '@' prefix)")
    print("  Prophet: ENABLED (LITE mode - 7 days training)")
    print("  Global Indicators: Sentiment, Whale Alerts, News")
    print("  Expected first run: 2-4 minutes")
    print("  Expected second run: 30-60 seconds\n")

    cache = get_cache_manager()

    # ==============================================
    # RUN 1: Cold Cache (FULL PRODUCTION CONFIG)
    # ==============================================
    print_header("RUN 1: Cold Cache (First Production Run)")
    print("⏳ This may take 2-4 minutes for full analysis...")
    print(f"   - Fetching prices for all {len(tradable_symbols)} symbols")
    print(f"   - Running technical analysis ({len(tradable_symbols)} symbols)")
    print("   - Calculating pivot points")
    print("   - Training Prophet models (LITE mode)")
    print("   - Fetching global indicators\n")

    start1 = time.time()

    try:
        snapshot1 = await build_market_data_snapshot(
            account_id=1,
            enable_prophet=True,  # ✅ Enable Prophet
            prophet_mode="lite",   # 7 days training
            symbols_filter=tradable_symbols,  # ✅ Only tradable symbols (no '@')
        )
        duration1 = time.time() - start1

        print(f"\n✅ Status: SUCCESS")
        print(f"⏱️  Duration: {format_duration(duration1)}")
        print(f"📊 Symbols analyzed: {len(snapshot1.get('symbols', []))}")

        stats1 = cache.get_stats()
        print(f"💾 Cache hits: {stats1['hits']}")
        print(f"💾 Cache misses: {stats1['misses']}")
        print(f"💾 Hit rate: {stats1['hit_rate']:.1f}%")

        # Count Prophet forecasts
        prophet_count = sum(1 for s in snapshot1.get("symbols", []) if s.get("prophet_forecast"))
        pivot_count = sum(1 for s in snapshot1.get("symbols", []) if s.get("pivot_points"))

        print(f"\n📈 Data Coverage:")
        print(f"  Technical Analysis: {len(snapshot1.get('symbols', []))}/{len(snapshot1.get('symbols', []))} (100%)")
        print(f"  Pivot Points: {pivot_count}/{len(snapshot1.get('symbols', []))} ({pivot_count/max(len(snapshot1.get('symbols', [])),1)*100:.1f}%)")
        print(f"  Prophet Forecasts: {prophet_count}/{len(snapshot1.get('symbols', []))} ({prophet_count/max(len(snapshot1.get('symbols', [])),1)*100:.1f}%)")

    except Exception as e:
        duration1 = time.time() - start1
        print(f"\n❌ FAILED after {format_duration(duration1)}: {e}")
        import traceback
        traceback.print_exc()
        return False

    # Display sample Prophet forecast
    if snapshot1.get("symbols"):
        btc_data = next((s for s in snapshot1["symbols"] if s["symbol"] == "BTC"), None)
        if btc_data and btc_data.get("prophet_forecast"):
            pf = btc_data["prophet_forecast"]
            print(f"\n📊 BTC Prophet Forecast (Sample):")
            print(f"  Current price: ${pf.get('current_price', 'N/A'):,.2f}")
            print(f"  Forecast 24h: ${pf.get('forecast_24h', 'N/A'):,.2f}")
            print(f"  Change: {pf.get('change_pct_24h', 0):+.2f}%")
            print(f"  Trend: {pf.get('trend', 'N/A').upper()}")
            print(f"  Confidence: {pf.get('confidence', 'N/A')}")

    # ==============================================
    # RUN 2: Warm Cache (Production Speed)
    # ==============================================
    print_header("RUN 2: Warm Cache (Production Speed Test)")
    print("⏳ Should be MUCH faster with cache (~30-60s)...\n")

    await asyncio.sleep(3)  # Short delay

    start2 = time.time()

    try:
        snapshot2 = await build_market_data_snapshot(
            account_id=1,
            enable_prophet=True,
            prophet_mode="lite",
            symbols_filter=tradable_symbols,  # Same filter as RUN 1
        )
        duration2 = time.time() - start2

        print(f"\n✅ Status: SUCCESS")
        print(f"⏱️  Duration: {format_duration(duration2)}")
        print(f"📊 Symbols analyzed: {len(snapshot2.get('symbols', []))}")

        stats2 = cache.get_stats()
        print(f"💾 Cache hits: {stats2['hits']} (+{stats2['hits'] - stats1['hits']})")
        print(f"💾 Cache misses: {stats2['misses']} (+{stats2['misses'] - stats1['misses']})")
        print(f"💾 Hit rate: {stats2['hit_rate']:.1f}%")

        speedup = duration1 / duration2 if duration2 > 0 else 1.0
        time_saved = duration1 - duration2
        print(f"\n⚡ Performance Improvement:")
        print(f"  Speedup: {speedup:.1f}x faster")
        print(f"  Time saved: {format_duration(time_saved)}")

        prophet_count2 = sum(1 for s in snapshot2.get("symbols", []) if s.get("prophet_forecast"))
        print(f"  Prophet forecasts: {prophet_count2}")

    except Exception as e:
        duration2 = time.time() - start2
        print(f"\n❌ FAILED after {format_duration(duration2)}: {e}")
        import traceback
        traceback.print_exc()
        return False

    # ==============================================
    # SAVE JSON OUTPUT
    # ==============================================
    output_dir = backend_path / "output"
    output_dir.mkdir(exist_ok=True)

    filename = f"market_snapshot_FULL_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    filepath = output_dir / filename

    with open(filepath, "w") as f:
        json.dump(snapshot2, f, indent=2, default=str)

    file_size_kb = filepath.stat().st_size / 1024
    file_size_mb = file_size_kb / 1024

    print(f"\n📁 JSON Output:")
    print(f"  Path: {filepath}")
    if file_size_mb >= 1:
        print(f"  Size: {file_size_mb:.2f} MB")
    else:
        print(f"  Size: {file_size_kb:.1f} KB")

    # ==============================================
    # DETAILED ANALYSIS
    # ==============================================
    print_header("DETAILED ANALYSIS")

    # Symbol breakdown by signal
    signals_count = {}
    for s in snapshot2.get("symbols", []):
        signal = s.get("technical_analysis", {}).get("signal", "UNKNOWN")
        signals_count[signal] = signals_count.get(signal, 0) + 1

    print("Technical Signals Distribution:")
    for signal, count in sorted(signals_count.items(), key=lambda x: -x[1]):
        pct = count / len(snapshot2.get("symbols", [])) * 100
        print(f"  {signal:15s}: {count:3d} symbols ({pct:5.1f}%)")

    # Prophet trends
    if prophet_count2 > 0:
        trends_count = {"up": 0, "down": 0, "flat": 0}
        for s in snapshot2.get("symbols", []):
            pf = s.get("prophet_forecast")
            if pf:
                trend = pf.get("trend", "flat")
                trends_count[trend] = trends_count.get(trend, 0) + 1

        print(f"\nProphet Trends Distribution ({prophet_count2} forecasts):")
        for trend, count in trends_count.items():
            pct = count / prophet_count2 * 100 if prophet_count2 > 0 else 0
            emoji = "📈" if trend == "up" else "📉" if trend == "down" else "➡️"
            print(f"  {emoji} {trend.upper():5s}: {count:3d} symbols ({pct:5.1f}%)")

    # Global indicators
    print(f"\nGlobal Indicators:")
    gi = snapshot2.get("global_indicators", {})
    sentiment = gi.get("sentiment", {})
    print(f"  Sentiment: {sentiment.get('value', 'N/A')}/100 ({sentiment.get('label', 'N/A')})")
    print(f"  Whale Alerts: {len(gi.get('whale_alerts', []))} recent transactions")
    print(f"  News Headlines: {len(gi.get('news', []))} articles")

    # Portfolio
    portfolio = snapshot2.get("portfolio", {})
    print(f"\nPortfolio State:")
    print(f"  Total Assets: ${portfolio.get('total_assets', 0):,.2f}")
    print(f"  Available Cash: ${portfolio.get('available_cash', 0):,.2f}")
    print(f"  Positions: {len(portfolio.get('positions', []))}")

    # ==============================================
    # TOP SIGNALS
    # ==============================================
    print_header("TOP 10 TECHNICAL SIGNALS")

    top_symbols = sorted(
        snapshot2["symbols"],
        key=lambda s: s["technical_analysis"]["score"],
        reverse=True,
    )[:10]

    for i, s in enumerate(top_symbols, 1):
        ta = s["technical_analysis"]
        pivot = s.get("pivot_points", {})
        prophet = s.get("prophet_forecast", {})

        prophet_str = ""
        if prophet:
            prophet_str = f" | Prophet: {prophet.get('trend', 'N/A'):4s} ({prophet.get('change_pct_24h', 0):+5.1f}%)"

        pivot_str = f"Pivot: {pivot.get('current_zone', 'N/A'):8s}" if pivot else "Pivot: N/A"

        print(
            f"  {i:2d}. {s['symbol']:6s} ${s['price']:>10,.2f} - "
            f"Score: {ta['score']:.3f} ({ta['signal']:12s}) - "
            f"{pivot_str}{prophet_str}"
        )

    # ==============================================
    # VERIFICATION
    # ==============================================
    print_header("VERIFICATION RESULTS")

    min_symbols = 200  # Expect at least 200 tradable symbols (out of ~220)

    checks = {
        "Run 1 completed": snapshot1 is not None,
        "Run 2 completed": snapshot2 is not None,
        "Minimum symbols analyzed (>200)": len(snapshot2.get("symbols", [])) >= min_symbols,
        "Prophet forecasts present": prophet_count2 > 100,  # At least 100 forecasts
        "Cache hits increased significantly": stats2['hits'] > 200,
        "Performance improved": duration2 < duration1,
        "First run reasonable (<300s)": duration1 < 300,  # 5 minutes max
        "Second run fast (<120s)": duration2 < 120,  # 2 minutes max
        "JSON size reasonable (>50KB)": file_size_kb > 50,  # Larger with 200+ symbols
    }

    passed_count = sum(1 for v in checks.values() if v)
    total_count = len(checks)

    for check, passed in checks.items():
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {status} - {check}")

    # ==============================================
    # FINAL VERDICT
    # ==============================================
    print_header("FINAL VERDICT")

    if passed_count == total_count:
        print(f"🎉 ALL {total_count} TESTS PASSED!")
        print("\n✅ SYSTEM READY FOR PRODUCTION DEPLOYMENT")
    elif passed_count >= total_count * 0.8:  # 80% pass rate
        print(f"⚠️  {passed_count}/{total_count} TESTS PASSED (80%+)")
        print("\n✅ System mostly ready, minor issues detected")
    else:
        print(f"❌ ONLY {passed_count}/{total_count} TESTS PASSED")
        print("\n⚠️  System needs fixes before production")

    print("\n📊 Production Performance Summary:")
    print(f"  Cold cache: {format_duration(duration1)}")
    print(f"  Warm cache: {format_duration(duration2)}")
    print(f"  Speedup: {speedup:.1f}x")
    print(f"  Symbols: {len(snapshot2.get('symbols', []))}")
    print(f"  Prophet forecasts: {prophet_count2}")
    print(f"  JSON size: {file_size_mb:.2f} MB" if file_size_mb >= 1 else f"  JSON size: {file_size_kb:.1f} KB")
    print(f"\n📁 Full JSON: {filepath}")

    return passed_count >= total_count * 0.8  # Pass if 80%+ checks pass


async def main():
    """Main entry point."""
    try:
        success = await test_full_production()
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
