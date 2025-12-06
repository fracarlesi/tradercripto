"""HFT Performance Metrics Module.

Tracks CPU, RAM, decision latency and other performance metrics
for HFT testing (Test A, B, C from specifications).

Test A: No AI (regime detection disabled) - targets <500ms latency
Test B: With AI but no blocking - targets <1s latency
Test C: Full pipeline with AI - targets <2s latency
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

logger = logging.getLogger(__name__)


@dataclass
class LatencyMetrics:
    """Latency metrics for a single measurement."""
    timestamp: datetime
    loop_ms: float  # Total loop time
    strategy_ms: float  # Strategy evaluation time
    risk_ms: float  # Risk engine time
    execution_ms: float  # Execution time
    regime_ms: float  # Regime detection time (0 if not run)


@dataclass
class SystemMetrics:
    """System resource metrics."""
    timestamp: datetime
    cpu_percent: float
    memory_mb: float
    memory_percent: float
    threads: int


@dataclass
class HFTSnapshot:
    """Complete HFT metrics snapshot."""
    timestamp: datetime

    # Latency stats (last N samples)
    avg_loop_ms: float
    p50_loop_ms: float
    p95_loop_ms: float
    p99_loop_ms: float
    max_loop_ms: float

    avg_strategy_ms: float
    avg_risk_ms: float
    avg_execution_ms: float
    avg_regime_ms: float

    # System metrics
    cpu_percent: float
    memory_mb: float

    # Counts
    loop_count: int
    signals_generated: int
    orders_executed: int
    regime_detections: int


class HFTMetricsCollector:
    """
    Collects and aggregates HFT performance metrics.

    Usage:
        metrics = HFTMetricsCollector()

        # In main loop:
        with metrics.measure_loop() as m:
            m.strategy_start()
            # ... evaluate strategies
            m.strategy_end()

            m.risk_start()
            # ... risk checks
            m.risk_end()
    """

    def __init__(self, history_size: int = 1000):
        """
        Initialize collector.

        Args:
            history_size: Number of latency samples to keep in memory
        """
        self._history_size = history_size
        self._latency_history: Deque[LatencyMetrics] = deque(maxlen=history_size)
        self._system_history: Deque[SystemMetrics] = deque(maxlen=60)  # ~1 min at 1/sec

        # Counters
        self._loop_count = 0
        self._signals_count = 0
        self._orders_count = 0
        self._regime_count = 0

        # Current measurement context
        self._current_measurement: Optional[LoopMeasurement] = None

        # Dry-run mode
        self._dry_run = False
        self._dry_run_orders: List[Dict] = []

        logger.info(f"HFT metrics collector initialized (history={history_size})")

    def enable_dry_run(self):
        """Enable dry-run mode (simulate without real orders)."""
        self._dry_run = True
        logger.info("HFT dry-run mode ENABLED")

    def disable_dry_run(self):
        """Disable dry-run mode."""
        self._dry_run = False
        self._dry_run_orders.clear()
        logger.info("HFT dry-run mode DISABLED")

    @property
    def is_dry_run(self) -> bool:
        return self._dry_run

    def record_dry_run_order(self, order_info: Dict):
        """Record a simulated order in dry-run mode."""
        if self._dry_run:
            self._dry_run_orders.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **order_info
            })

    def get_dry_run_orders(self) -> List[Dict]:
        """Get list of simulated orders."""
        return list(self._dry_run_orders)

    def measure_loop(self) -> "LoopMeasurement":
        """
        Context manager for measuring loop latency.

        Returns:
            LoopMeasurement context for tracking sub-timings
        """
        self._current_measurement = LoopMeasurement(self)
        return self._current_measurement

    def record_signal(self):
        """Record that a trading signal was generated."""
        self._signals_count += 1

    def record_order(self):
        """Record that an order was executed."""
        self._orders_count += 1

    def record_regime_detection(self):
        """Record that regime detection was run."""
        self._regime_count += 1

    def _record_loop_completion(self, metrics: LatencyMetrics):
        """Internal: Record completed loop metrics."""
        self._latency_history.append(metrics)
        self._loop_count += 1

    def sample_system_metrics(self):
        """Sample current system metrics (CPU, RAM)."""
        if not HAS_PSUTIL:
            return

        try:
            process = psutil.Process()

            # CPU percent (interval=None for instant reading)
            cpu = process.cpu_percent(interval=None)

            # Memory info
            mem_info = process.memory_info()
            memory_mb = mem_info.rss / (1024 * 1024)
            memory_percent = process.memory_percent()

            # Thread count
            threads = process.num_threads()

            self._system_history.append(SystemMetrics(
                timestamp=datetime.now(timezone.utc),
                cpu_percent=cpu,
                memory_mb=memory_mb,
                memory_percent=memory_percent,
                threads=threads,
            ))
        except Exception as e:
            logger.warning(f"Failed to sample system metrics: {e}")

    def get_snapshot(self) -> HFTSnapshot:
        """Get current aggregated metrics snapshot."""
        now = datetime.now(timezone.utc)

        # Latency percentiles
        if self._latency_history:
            loop_times = sorted(m.loop_ms for m in self._latency_history)
            n = len(loop_times)

            avg_loop = sum(loop_times) / n
            p50_loop = loop_times[int(n * 0.50)]
            p95_loop = loop_times[int(n * 0.95)] if n > 20 else loop_times[-1]
            p99_loop = loop_times[int(n * 0.99)] if n > 100 else loop_times[-1]
            max_loop = loop_times[-1]

            avg_strategy = sum(m.strategy_ms for m in self._latency_history) / n
            avg_risk = sum(m.risk_ms for m in self._latency_history) / n
            avg_execution = sum(m.execution_ms for m in self._latency_history) / n

            regime_samples = [m.regime_ms for m in self._latency_history if m.regime_ms > 0]
            avg_regime = sum(regime_samples) / len(regime_samples) if regime_samples else 0
        else:
            avg_loop = p50_loop = p95_loop = p99_loop = max_loop = 0
            avg_strategy = avg_risk = avg_execution = avg_regime = 0

        # System metrics
        if self._system_history:
            recent_sys = list(self._system_history)[-10:]  # Last 10 samples
            cpu = sum(s.cpu_percent for s in recent_sys) / len(recent_sys)
            mem = sum(s.memory_mb for s in recent_sys) / len(recent_sys)
        else:
            cpu = mem = 0

        return HFTSnapshot(
            timestamp=now,
            avg_loop_ms=avg_loop,
            p50_loop_ms=p50_loop,
            p95_loop_ms=p95_loop,
            p99_loop_ms=p99_loop,
            max_loop_ms=max_loop,
            avg_strategy_ms=avg_strategy,
            avg_risk_ms=avg_risk,
            avg_execution_ms=avg_execution,
            avg_regime_ms=avg_regime,
            cpu_percent=cpu,
            memory_mb=mem,
            loop_count=self._loop_count,
            signals_generated=self._signals_count,
            orders_executed=self._orders_count,
            regime_detections=self._regime_count,
        )

    def get_latency_history(self, limit: int = 100) -> List[Dict]:
        """Get recent latency history as list of dicts."""
        recent = list(self._latency_history)[-limit:]
        return [
            {
                "timestamp": m.timestamp.isoformat(),
                "loop_ms": round(m.loop_ms, 2),
                "strategy_ms": round(m.strategy_ms, 2),
                "risk_ms": round(m.risk_ms, 2),
                "execution_ms": round(m.execution_ms, 2),
                "regime_ms": round(m.regime_ms, 2),
            }
            for m in recent
        ]

    def reset_counters(self):
        """Reset all counters (for test phases)."""
        self._loop_count = 0
        self._signals_count = 0
        self._orders_count = 0
        self._regime_count = 0
        self._latency_history.clear()
        self._system_history.clear()
        self._dry_run_orders.clear()
        logger.info("HFT metrics counters reset")


class LoopMeasurement:
    """Context manager for measuring individual loop iteration timing."""

    def __init__(self, collector: HFTMetricsCollector):
        self._collector = collector
        self._loop_start: float = 0
        self._strategy_start: float = 0
        self._strategy_time: float = 0
        self._risk_start: float = 0
        self._risk_time: float = 0
        self._execution_start: float = 0
        self._execution_time: float = 0
        self._regime_start: float = 0
        self._regime_time: float = 0

    def __enter__(self) -> "LoopMeasurement":
        self._loop_start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        loop_end = time.perf_counter()
        loop_ms = (loop_end - self._loop_start) * 1000

        metrics = LatencyMetrics(
            timestamp=datetime.now(timezone.utc),
            loop_ms=loop_ms,
            strategy_ms=self._strategy_time,
            risk_ms=self._risk_time,
            execution_ms=self._execution_time,
            regime_ms=self._regime_time,
        )
        self._collector._record_loop_completion(metrics)

        # Also sample system metrics periodically
        if self._collector._loop_count % 10 == 0:
            self._collector.sample_system_metrics()

        return False

    def strategy_start(self):
        """Mark start of strategy evaluation."""
        self._strategy_start = time.perf_counter()

    def strategy_end(self):
        """Mark end of strategy evaluation."""
        self._strategy_time = (time.perf_counter() - self._strategy_start) * 1000

    def risk_start(self):
        """Mark start of risk engine processing."""
        self._risk_start = time.perf_counter()

    def risk_end(self):
        """Mark end of risk engine processing."""
        self._risk_time = (time.perf_counter() - self._risk_start) * 1000

    def execution_start(self):
        """Mark start of order execution."""
        self._execution_start = time.perf_counter()

    def execution_end(self):
        """Mark end of order execution."""
        self._execution_time = (time.perf_counter() - self._execution_start) * 1000

    def regime_start(self):
        """Mark start of regime detection."""
        self._regime_start = time.perf_counter()

    def regime_end(self):
        """Mark end of regime detection."""
        self._regime_time = (time.perf_counter() - self._regime_start) * 1000


# Singleton instance for easy access
_metrics_collector: Optional[HFTMetricsCollector] = None


def get_metrics_collector() -> HFTMetricsCollector:
    """Get the global metrics collector instance."""
    global _metrics_collector
    if _metrics_collector is None:
        _metrics_collector = HFTMetricsCollector()
    return _metrics_collector


def reset_metrics_collector():
    """Reset the global metrics collector."""
    global _metrics_collector
    _metrics_collector = HFTMetricsCollector()
    return _metrics_collector
