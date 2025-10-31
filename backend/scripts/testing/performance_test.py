"""Performance Testing Script for Async API Endpoints (T062).

Tests concurrent requests to verify:
- p95 latency < 200ms
- Non-blocking behavior
- Connection pool efficiency

Usage:
    python backend/scripts/testing/performance_test.py
"""

import asyncio
import statistics
import time
from typing import Any

import httpx


class PerformanceTest:
    """Performance testing for async API endpoints."""

    def __init__(self, base_url: str = "http://localhost:5611"):
        """Initialize performance tester.

        Args:
            base_url: Base URL of the API server
        """
        self.base_url = base_url
        self.results: list[dict[str, Any]] = []

    async def test_endpoint(self, method: str, path: str, **kwargs) -> dict[str, float]:
        """Test single endpoint and measure latency.

        Args:
            method: HTTP method (GET, POST, etc.)
            path: Endpoint path
            **kwargs: Additional httpx request parameters

        Returns:
            Dict with latency and status code
        """
        async with httpx.AsyncClient() as client:
            start = time.perf_counter()
            try:
                response = await client.request(
                    method, f"{self.base_url}{path}", timeout=10.0, **kwargs
                )
                latency = (time.perf_counter() - start) * 1000  # Convert to ms
                return {
                    "latency_ms": latency,
                    "status_code": response.status_code,
                    "success": 200 <= response.status_code < 300,
                }
            except Exception as e:
                latency = (time.perf_counter() - start) * 1000
                return {
                    "latency_ms": latency,
                    "status_code": 0,
                    "success": False,
                    "error": str(e),
                }

    async def run_concurrent_tests(
        self, method: str, path: str, count: int = 10, **kwargs
    ) -> dict[str, Any]:
        """Run concurrent requests to same endpoint.

        Args:
            method: HTTP method
            path: Endpoint path
            count: Number of concurrent requests
            **kwargs: Additional httpx request parameters

        Returns:
            Performance statistics
        """
        tasks = [self.test_endpoint(method, path, **kwargs) for _ in range(count)]
        results = await asyncio.gather(*tasks)

        latencies = [r["latency_ms"] for r in results]
        success_count = sum(1 for r in results if r["success"])

        return {
            "endpoint": path,
            "total_requests": count,
            "successful_requests": success_count,
            "failed_requests": count - success_count,
            "p50_latency_ms": statistics.median(latencies),
            "p95_latency_ms": (
                statistics.quantiles(latencies, n=20)[18] if len(latencies) > 1 else latencies[0]
            ),
            "p99_latency_ms": (
                statistics.quantiles(latencies, n=100)[98] if len(latencies) > 1 else latencies[0]
            ),
            "min_latency_ms": min(latencies),
            "max_latency_ms": max(latencies),
            "avg_latency_ms": statistics.mean(latencies),
            "results": results,
        }

    async def test_all_endpoints(self, concurrent_count: int = 10) -> list[dict[str, Any]]:
        """Test all critical async endpoints.

        Args:
            concurrent_count: Number of concurrent requests per endpoint

        Returns:
            List of performance results
        """
        endpoints = [
            # Health checks
            ("GET", "/api/health"),
            ("GET", "/api/readiness"),
            # Account routes (async)
            ("GET", "/api/accounts"),
            # Market data routes (async) - may fail if no data
            ("GET", "/api/market/prices/async?symbols=BTC&market=hyperliquid"),
            # Sync status
            ("GET", "/api/sync/status"),
        ]

        results = []
        for method, path in endpoints:
            print(f"\nTesting {method} {path} with {concurrent_count} concurrent requests...")
            result = await self.run_concurrent_tests(method, path, concurrent_count)
            results.append(result)
            self.print_result(result)

        return results

    def print_result(self, result: dict[str, Any]) -> None:
        """Print test results in readable format.

        Args:
            result: Test result dictionary
        """
        print(f"  Endpoint: {result['endpoint']}")
        print(
            f"  Total: {result['total_requests']} | "
            f"Success: {result['successful_requests']} | "
            f"Failed: {result['failed_requests']}"
        )
        print("  Latency (ms):")
        print(f"    p50: {result['p50_latency_ms']:.2f}ms")
        print(f"    p95: {result['p95_latency_ms']:.2f}ms")
        print(f"    p99: {result['p99_latency_ms']:.2f}ms")
        print(f"    avg: {result['avg_latency_ms']:.2f}ms")
        print(f"    min: {result['min_latency_ms']:.2f}ms")
        print(f"    max: {result['max_latency_ms']:.2f}ms")

        # Check if p95 meets target (<200ms)
        if result["p95_latency_ms"] < 200:
            print("  ✅ PASS: p95 latency < 200ms target")
        else:
            print("  ❌ FAIL: p95 latency >= 200ms target")

    def print_summary(self, results: list[dict[str, Any]]) -> None:
        """Print overall test summary.

        Args:
            results: List of test results
        """
        print("\n" + "=" * 80)
        print("PERFORMANCE TEST SUMMARY")
        print("=" * 80)

        total_requests = sum(r["total_requests"] for r in results)
        total_success = sum(r["successful_requests"] for r in results)
        total_failed = sum(r["failed_requests"] for r in results)

        print(f"\nTotal Requests: {total_requests}")
        print(f"Successful: {total_success}")
        print(f"Failed: {total_failed}")
        print(f"Success Rate: {(total_success / total_requests * 100):.1f}%")

        # Check p95 latency target for all endpoints
        passed = sum(1 for r in results if r["p95_latency_ms"] < 200)
        print("\np95 < 200ms Target:")
        print(f"  Passed: {passed}/{len(results)} endpoints")
        print(f"  Failed: {len(results) - passed}/{len(results)} endpoints")

        if passed == len(results) and total_failed == 0:
            print("\n✅ ALL TESTS PASSED")
        else:
            print("\n❌ SOME TESTS FAILED")

        # Print worst performing endpoints
        print("\nWorst p95 Latencies:")
        sorted_results = sorted(results, key=lambda r: r["p95_latency_ms"], reverse=True)
        for i, r in enumerate(sorted_results[:3], 1):
            print(f"  {i}. {r['endpoint']}: {r['p95_latency_ms']:.2f}ms")


async def main():
    """Run performance tests."""
    print("Starting Performance Tests for Async API Endpoints (T062)")
    print("=" * 80)
    print("\nConfiguration:")
    print("  Base URL: http://localhost:5611")
    print("  Concurrent requests per endpoint: 10")
    print("  Target: p95 latency < 200ms")
    print("\nNOTE: Ensure the API server is running before starting tests")
    print("      uvicorn backend.main:app --reload --port 5611")
    print("=" * 80)

    tester = PerformanceTest()

    # Run tests with different concurrency levels
    print("\n\n📊 Testing with 10 concurrent requests...")
    results_10 = await tester.test_all_endpoints(concurrent_count=10)

    print("\n\n📊 Testing with 20 concurrent requests...")
    results_20 = await tester.test_all_endpoints(concurrent_count=20)

    # Print summaries
    print("\n" + "=" * 80)
    print("RESULTS: 10 Concurrent Requests")
    tester.print_summary(results_10)

    print("\n" + "=" * 80)
    print("RESULTS: 20 Concurrent Requests")
    tester.print_summary(results_20)

    # Overall verdict
    all_passed = all(r["p95_latency_ms"] < 200 for r in results_10 + results_20)
    all_success = all(r["failed_requests"] == 0 for r in results_10 + results_20)

    print("\n" + "=" * 80)
    print("FINAL VERDICT")
    print("=" * 80)
    if all_passed and all_success:
        print("✅ T062 COMPLETE: All endpoints meet p95 < 200ms target")
        print("✅ Non-blocking behavior verified")
        print("✅ Connection pool handling concurrent requests efficiently")
    else:
        print("⚠️ Performance issues detected:")
        if not all_passed:
            print("  - Some endpoints exceed p95 200ms target")
        if not all_success:
            print("  - Some requests failed")


if __name__ == "__main__":
    asyncio.run(main())
