"""Rate limiter for order execution."""

import asyncio
import time
from collections import deque
from typing import Optional


class OrderRateLimiter:
    """
    Rate limiter for Hyperliquid API order endpoints.

    More conservative than official limits to avoid hitting them.
    """

    def __init__(
        self,
        max_orders_per_second: int = 8,  # HL allows 10, we use 8
        max_requests_per_minute: int = 80,  # HL allows 100, we use 80
    ):
        self.max_orders_per_second = max_orders_per_second
        self.max_requests_per_minute = max_requests_per_minute

        # Track recent requests
        self._second_window: deque = deque()  # Timestamps in last second
        self._minute_window: deque = deque()  # Timestamps in last minute

        self._lock = asyncio.Lock()

    async def acquire(self, timeout: float = 30.0) -> bool:
        """
        Acquire permission to make an order request.

        Returns True if acquired, False if timeout.
        """
        start = time.time()

        while True:
            async with self._lock:
                now = time.time()
                self._cleanup_windows(now)

                # Check limits
                if (
                    len(self._second_window) < self.max_orders_per_second
                    and len(self._minute_window) < self.max_requests_per_minute
                ):
                    self._second_window.append(now)
                    self._minute_window.append(now)
                    return True

            # Check timeout
            if time.time() - start > timeout:
                return False

            # Calculate wait time
            wait_time = self._calculate_wait_time()
            await asyncio.sleep(min(wait_time, 0.1))

    def _cleanup_windows(self, now: float):
        """Remove old timestamps from windows."""
        # Clean second window
        cutoff_second = now - 1.0
        while self._second_window and self._second_window[0] < cutoff_second:
            self._second_window.popleft()

        # Clean minute window
        cutoff_minute = now - 60.0
        while self._minute_window and self._minute_window[0] < cutoff_minute:
            self._minute_window.popleft()

    def _calculate_wait_time(self) -> float:
        """Calculate how long to wait before retrying."""
        now = time.time()

        # If per-second limit hit, wait until oldest expires
        if len(self._second_window) >= self.max_orders_per_second:
            oldest = self._second_window[0]
            return max(0, 1.0 - (now - oldest))

        # If per-minute limit hit, wait until oldest expires
        if len(self._minute_window) >= self.max_requests_per_minute:
            oldest = self._minute_window[0]
            return max(0, 60.0 - (now - oldest))

        return 0.01  # Small delay

    @property
    def current_rate(self) -> dict:
        """Get current rate usage."""
        return {
            "per_second": len(self._second_window),
            "max_per_second": self.max_orders_per_second,
            "per_minute": len(self._minute_window),
            "max_per_minute": self.max_requests_per_minute,
        }

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass
