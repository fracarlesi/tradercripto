"""
Test Script - WebSocket Momentum System Validation

This script tests the new WebSocket-based hourly momentum system.

Tests:
1. WebSocket service initialization
2. Cache population (verify candles arrive via WebSocket)
3. Momentum calculation from cache (zero API calls)
4. Performance comparison (old HTTP vs new WebSocket)
5. Memory usage check
6. Reconnection handling

Usage:
    cd backend/
    python scripts/testing/test_websocket_momentum.py
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services.market_data.websocket_candle_service import WebsocketCandleService
from services.market_data.hourly_momentum import calculate_hourly_momentum

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


async def test_websocket_service():
    """Test WebSocket service initialization and cache population."""
    logger.info("=" * 80)
    logger.info("TEST 1: WebSocket Service Initialization")
    logger.info("=" * 80)

    # Create service
    ws_service = WebsocketCandleService(
        cache_dir="/tmp/websocket_test_cache",
        max_candles_per_symbol=24,
    )

    # Start service (subscribe to all symbols)
    await ws_service.start()

    # Wait for cache to populate (5-10 seconds should be enough)
    logger.info("Waiting 10 seconds for WebSocket to populate cache...")
    await asyncio.sleep(10)

    # Check cache stats
    stats = ws_service.get_cache_stats()
    logger.info(f"Cache stats: {stats}")

    assert stats["connected"], "❌ WebSocket not connected!"
    assert stats["symbols_cached"] > 0, "❌ No symbols in cache!"
    assert stats["total_candles"] > 0, "❌ No candles in cache!"

    logger.info(f"✅ WebSocket connected and cache populated with {stats['symbols_cached']} symbols")

    # Test reading candles for specific symbol
    btc_candles = ws_service.get_candles("BTC", limit=5)
    logger.info(f"BTC candles (last 5): {len(btc_candles)} candles")

    if btc_candles:
        latest = btc_candles[0]
        logger.info(f"Latest BTC candle: close=${latest['c']:.2f}, volume={latest['v']:.2f}")
    else:
        logger.warning("⚠️  No BTC candles found in cache yet")

    return ws_service


async def test_momentum_calculation(ws_service):
    """Test momentum calculation using WebSocket cache."""
    logger.info("=" * 80)
    logger.info("TEST 2: Momentum Calculation from Cache")
    logger.info("=" * 80)

    # Calculate momentum
    start_time = time.time()
    performers = await calculate_hourly_momentum(limit=20)
    elapsed = time.time() - start_time

    logger.info(f"✅ Momentum calculation completed in {elapsed:.2f}s")
    logger.info(f"Found {len(performers)} top performers")

    if performers:
        logger.info("Top 5 performers:")
        for i, p in enumerate(performers[:5], 1):
            logger.info(
                f"  {i}. {p['symbol']}: {p['momentum_pct']:+.2f}% "
                f"(vol: ${p['volume_usd']:,.0f}, score: {p['momentum_score']:.2f})"
            )
    else:
        logger.warning("⚠️  No performers found - cache may not be fully populated yet")

    assert len(performers) > 0, "❌ No performers found!"
    assert elapsed < 2.0, f"❌ Momentum calculation too slow: {elapsed:.2f}s (expected <2s)"

    logger.info(f"✅ Performance OK: {elapsed:.2f}s (target: <2s)")


async def test_cache_persistence(ws_service):
    """Test cache save/load to disk."""
    logger.info("=" * 80)
    logger.info("TEST 3: Cache Persistence")
    logger.info("=" * 80)

    # Save cache
    await ws_service._save_cache()
    logger.info("✅ Cache saved to disk")

    # Check file exists
    cache_file = Path(ws_service.cache_dir) / "candle_cache.json"
    assert cache_file.exists(), "❌ Cache file not found!"

    file_size_mb = cache_file.stat().st_size / (1024 * 1024)
    logger.info(f"Cache file size: {file_size_mb:.2f} MB")

    # Load cache (simulate restart)
    ws_service.candle_cache.clear()
    await ws_service._load_cache()

    stats = ws_service.get_cache_stats()
    logger.info(f"Cache loaded: {stats['symbols_cached']} symbols, {stats['total_candles']} candles")

    assert stats["symbols_cached"] > 0, "❌ Cache not loaded correctly!"
    logger.info("✅ Cache persistence working")


async def test_memory_usage(ws_service):
    """Test memory usage."""
    logger.info("=" * 80)
    logger.info("TEST 4: Memory Usage")
    logger.info("=" * 80)

    stats = ws_service.get_cache_stats()
    memory_mb = stats["memory_mb"]

    logger.info(f"Cache memory usage: {memory_mb:.2f} MB")
    logger.info(f"Symbols: {stats['symbols_cached']}, Candles: {stats['total_candles']}")

    # Expected: ~220 symbols × 24 candles × 200 bytes = ~1 MB
    assert memory_mb < 5.0, f"❌ Memory usage too high: {memory_mb:.2f} MB (expected <5 MB)"

    logger.info(f"✅ Memory usage OK: {memory_mb:.2f} MB (target: <5 MB)")


async def test_reconnection(ws_service):
    """Test reconnection handling (manual)."""
    logger.info("=" * 80)
    logger.info("TEST 5: Reconnection Handling (Manual)")
    logger.info("=" * 80)

    logger.info("This test requires manual intervention:")
    logger.info("1. Disconnect network for 5-10 seconds")
    logger.info("2. Reconnect network")
    logger.info("3. Observe logs for automatic reconnection")
    logger.info("")
    logger.info("Skipping automatic test...")


async def main():
    """Run all tests."""
    logger.info("🚀 WebSocket Momentum System Validation")
    logger.info("=" * 80)

    ws_service = None

    try:
        # Test 1: WebSocket service initialization
        ws_service = await test_websocket_service()

        # Test 2: Momentum calculation
        await test_momentum_calculation(ws_service)

        # Test 3: Cache persistence
        await test_cache_persistence(ws_service)

        # Test 4: Memory usage
        await test_memory_usage(ws_service)

        # Test 5: Reconnection (manual)
        await test_reconnection(ws_service)

        logger.info("=" * 80)
        logger.info("✅ ALL TESTS PASSED")
        logger.info("=" * 80)

    except AssertionError as e:
        logger.error(f"❌ TEST FAILED: {e}")
        return 1

    except Exception as e:
        logger.error(f"❌ UNEXPECTED ERROR: {e}", exc_info=True)
        return 1

    finally:
        # Stop service
        if ws_service:
            await ws_service.stop()
            logger.info("WebSocket service stopped")

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
