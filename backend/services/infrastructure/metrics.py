"""Prometheus metrics exporter for the trading system.

Provides application and business metrics for monitoring system health,
performance, and trading activity.
"""

from decimal import Decimal

from config.logging import get_logger
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

logger = get_logger(__name__)

# Create global registry
registry = CollectorRegistry()

# ============================================================================
# Application Metrics (T127)
# ============================================================================

# System uptime
uptime_seconds = Gauge(
    name="trading_system_uptime_seconds",
    documentation="System uptime in seconds since startup",
    registry=registry,
)

# Sync metrics
sync_success_total = Counter(
    name="trading_system_sync_success_total",
    documentation="Total number of successful account sync operations",
    labelnames=["account_id"],
    registry=registry,
)

sync_failure_total = Counter(
    name="trading_system_sync_failure_total",
    documentation="Total number of failed account sync operations",
    labelnames=["account_id", "error_type"],
    registry=registry,
)

sync_duration_seconds = Histogram(
    name="trading_system_sync_duration_seconds",
    documentation="Duration of account sync operations in seconds",
    labelnames=["account_id"],
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
    registry=registry,
)

# API metrics
api_requests_total = Counter(
    name="trading_system_api_requests_total",
    documentation="Total number of API requests",
    labelnames=["method", "endpoint", "status_code"],
    registry=registry,
)

api_request_duration_seconds = Histogram(
    name="trading_system_api_request_duration_seconds",
    documentation="API request duration in seconds",
    labelnames=["method", "endpoint"],
    buckets=(0.01, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0),
    registry=registry,
)

# Database connection pool metrics
db_pool_size = Gauge(
    name="trading_system_db_pool_size",
    documentation="Total size of database connection pool",
    registry=registry,
)

db_pool_available = Gauge(
    name="trading_system_db_pool_available",
    documentation="Number of available connections in pool",
    registry=registry,
)

db_pool_overflow = Gauge(
    name="trading_system_db_pool_overflow",
    documentation="Number of overflow connections in use",
    registry=registry,
)

db_pool_checkedout = Gauge(
    name="trading_system_db_pool_checkedout",
    documentation="Number of connections currently checked out",
    registry=registry,
)

# ============================================================================
# Business Metrics (T128)
# ============================================================================

# Account balance
account_balance_usd = Gauge(
    name="trading_system_account_balance_usd",
    documentation="Current account balance in USD",
    labelnames=["account_id", "account_name"],
    registry=registry,
)

account_frozen_balance_usd = Gauge(
    name="trading_system_account_frozen_balance_usd",
    documentation="Current frozen balance (margin) in USD",
    labelnames=["account_id", "account_name"],
    registry=registry,
)

# AI decision metrics
ai_decisions_total = Counter(
    name="trading_system_ai_decisions_total",
    documentation="Total number of AI trading decisions made",
    labelnames=["decision_type"],  # BUY, SELL, HOLD
    registry=registry,
)

ai_decision_duration_seconds = Histogram(
    name="trading_system_ai_decision_duration_seconds",
    documentation="Duration of AI decision making in seconds",
    buckets=(0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
    registry=registry,
)

ai_api_calls_total = Counter(
    name="trading_system_ai_api_calls_total",
    documentation="Total number of AI API calls made",
    labelnames=["provider"],  # deepseek, etc.
    registry=registry,
)

ai_cache_hits_total = Counter(
    name="trading_system_ai_cache_hits_total",
    documentation="Total number of AI decision cache hits",
    registry=registry,
)

ai_cache_misses_total = Counter(
    name="trading_system_ai_cache_misses_total",
    documentation="Total number of AI decision cache misses",
    registry=registry,
)

# Order metrics
orders_placed_total = Counter(
    name="trading_system_orders_placed_total",
    documentation="Total number of orders placed",
    labelnames=["account_id", "symbol", "side", "order_type"],
    registry=registry,
)

orders_filled_total = Counter(
    name="trading_system_orders_filled_total",
    documentation="Total number of orders filled",
    labelnames=["account_id", "symbol", "side"],
    registry=registry,
)

orders_cancelled_total = Counter(
    name="trading_system_orders_cancelled_total",
    documentation="Total number of orders cancelled",
    labelnames=["account_id", "symbol"],
    registry=registry,
)

order_success_rate = Gauge(
    name="trading_system_order_success_rate",
    documentation="Order success rate (filled / placed)",
    labelnames=["account_id"],
    registry=registry,
)

# Position metrics
positions_count = Gauge(
    name="trading_system_positions_count",
    documentation="Current number of open positions",
    labelnames=["account_id"],
    registry=registry,
)

position_value_usd = Gauge(
    name="trading_system_position_value_usd",
    documentation="Total value of positions in USD",
    labelnames=["account_id", "symbol"],
    registry=registry,
)

# Trade metrics
trades_executed_total = Counter(
    name="trading_system_trades_executed_total",
    documentation="Total number of trades executed",
    labelnames=["account_id", "symbol", "side"],
    registry=registry,
)

trade_volume_usd = Counter(
    name="trading_system_trade_volume_usd",
    documentation="Total trade volume in USD",
    labelnames=["account_id", "symbol"],
    registry=registry,
)

# Profit/Loss metrics
realized_pnl_usd = Gauge(
    name="trading_system_realized_pnl_usd",
    documentation="Realized profit and loss in USD",
    labelnames=["account_id"],
    registry=registry,
)

unrealized_pnl_usd = Gauge(
    name="trading_system_unrealized_pnl_usd",
    documentation="Unrealized profit and loss in USD",
    labelnames=["account_id"],
    registry=registry,
)

# Circuit breaker metrics
circuit_breaker_state = Gauge(
    name="trading_system_circuit_breaker_state",
    documentation="Circuit breaker state (0=closed, 1=open, 2=half_open)",
    labelnames=["service"],
    registry=registry,
)

circuit_breaker_failures = Counter(
    name="trading_system_circuit_breaker_failures_total",
    documentation="Total number of circuit breaker failures",
    labelnames=["service"],
    registry=registry,
)


class MetricsService:
    """Service for managing and updating Prometheus metrics."""

    def __init__(self) -> None:
        """Initialize metrics service."""
        self._start_time = None
        logger.info("MetricsService initialized")

    def start(self) -> None:
        """Start metrics service and initialize uptime counter."""
        import time

        self._start_time = time.time()
        logger.info("MetricsService started")

    def update_uptime(self) -> None:
        """Update system uptime metric."""
        if self._start_time is not None:
            import time

            uptime = time.time() - self._start_time
            uptime_seconds.set(uptime)

    def record_sync_success(self, account_id: int, duration: float) -> None:
        """Record successful sync operation.

        Args:
            account_id: Account ID
            duration: Sync duration in seconds
        """
        sync_success_total.labels(account_id=str(account_id)).inc()
        sync_duration_seconds.labels(account_id=str(account_id)).observe(duration)

    def record_sync_failure(self, account_id: int, error_type: str, duration: float) -> None:
        """Record failed sync operation.

        Args:
            account_id: Account ID
            error_type: Type of error (e.g., "timeout", "connection", "invalid_data")
            duration: Sync duration in seconds
        """
        sync_failure_total.labels(account_id=str(account_id), error_type=error_type).inc()
        sync_duration_seconds.labels(account_id=str(account_id)).observe(duration)

    def record_api_request(
        self, method: str, endpoint: str, status_code: int, duration: float
    ) -> None:
        """Record API request.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint path
            status_code: HTTP status code
            duration: Request duration in seconds
        """
        api_requests_total.labels(
            method=method, endpoint=endpoint, status_code=str(status_code)
        ).inc()
        api_request_duration_seconds.labels(method=method, endpoint=endpoint).observe(duration)

    def update_db_pool_metrics(
        self, pool_size: int, available: int, overflow: int, checkedout: int
    ) -> None:
        """Update database connection pool metrics.

        Args:
            pool_size: Total pool size
            available: Available connections
            overflow: Overflow connections in use
            checkedout: Checked out connections
        """
        db_pool_size.set(pool_size)
        db_pool_available.set(available)
        db_pool_overflow.set(overflow)
        db_pool_checkedout.set(checkedout)

    def update_account_balance(
        self, account_id: int, account_name: str, balance: Decimal, frozen: Decimal
    ) -> None:
        """Update account balance metrics.

        Args:
            account_id: Account ID
            account_name: Account name
            balance: Current available balance
            frozen: Frozen balance (margin)
        """
        account_balance_usd.labels(account_id=str(account_id), account_name=account_name).set(
            float(balance)
        )
        account_frozen_balance_usd.labels(
            account_id=str(account_id), account_name=account_name
        ).set(float(frozen))

    def record_ai_decision(self, decision_type: str, duration: float) -> None:
        """Record AI trading decision.

        Args:
            decision_type: Decision type (BUY, SELL, HOLD)
            duration: Decision making duration in seconds
        """
        ai_decisions_total.labels(decision_type=decision_type).inc()
        ai_decision_duration_seconds.observe(duration)

    def record_ai_api_call(self, provider: str = "deepseek") -> None:
        """Record AI API call.

        Args:
            provider: AI provider name
        """
        ai_api_calls_total.labels(provider=provider).inc()

    def record_ai_cache_hit(self) -> None:
        """Record AI decision cache hit."""
        ai_cache_hits_total.inc()

    def record_ai_cache_miss(self) -> None:
        """Record AI decision cache miss."""
        ai_cache_misses_total.inc()

    def record_order_placed(self, account_id: int, symbol: str, side: str, order_type: str) -> None:
        """Record order placement.

        Args:
            account_id: Account ID
            symbol: Trading symbol
            side: Order side (BUY, SELL)
            order_type: Order type (MARKET, LIMIT)
        """
        orders_placed_total.labels(
            account_id=str(account_id),
            symbol=symbol,
            side=side,
            order_type=order_type,
        ).inc()

    def record_order_filled(self, account_id: int, symbol: str, side: str) -> None:
        """Record order fill.

        Args:
            account_id: Account ID
            symbol: Trading symbol
            side: Order side (BUY, SELL)
        """
        orders_filled_total.labels(account_id=str(account_id), symbol=symbol, side=side).inc()

    def update_order_success_rate(self, account_id: int, rate: float) -> None:
        """Update order success rate.

        Args:
            account_id: Account ID
            rate: Success rate (0.0 to 1.0)
        """
        order_success_rate.labels(account_id=str(account_id)).set(rate)

    def update_positions_count(self, account_id: int, count: int) -> None:
        """Update positions count.

        Args:
            account_id: Account ID
            count: Number of open positions
        """
        positions_count.labels(account_id=str(account_id)).set(count)

    def update_circuit_breaker_state(
        self, service: str, state: str, failure_count: int = 0
    ) -> None:
        """Update circuit breaker state.

        Args:
            service: Service name (e.g., "sync", "trading")
            state: Circuit state (closed, open, half_open)
            failure_count: Number of failures
        """
        state_value = {"closed": 0, "open": 1, "half_open": 2}.get(state.lower(), 0)
        circuit_breaker_state.labels(service=service).set(state_value)

        if failure_count > 0:
            circuit_breaker_failures.labels(service=service).inc(failure_count)

    def get_metrics_text(self) -> bytes:
        """Get Prometheus metrics in text exposition format.

        Returns:
            Metrics in Prometheus text format
        """
        # Update uptime before generating metrics
        self.update_uptime()

        return generate_latest(registry)


# Global metrics service instance
metrics_service = MetricsService()
