"""Database connection pool metrics tracking.

Provides gauges for monitoring connection pool health.
"""

from config.logging import get_logger
from database.connection import engine
from sqlalchemy.pool import QueuePool

logger = get_logger(__name__)


def get_pool_metrics() -> dict[str, int]:
    """Get current connection pool metrics.

    Returns:
        Dict with pool metrics:
        {
            "pool_size": N,          # Total pool size
            "pool_available": M,     # Available connections
            "pool_overflow": K,      # Overflow connections
            "pool_checkedout": L     # Checked out connections
        }

    Note: Metrics only available for PostgreSQL with QueuePool.
    Returns zeros for SQLite (NullPool).
    """
    pool = engine.pool

    if not isinstance(pool, QueuePool):
        # NullPool (SQLite) doesn't have metrics
        return {
            "pool_size": 0,
            "pool_available": 0,
            "pool_overflow": 0,
            "pool_checkedout": 0,
        }

    try:
        # QueuePool metrics (T058)
        return {
            "pool_size": pool.size(),
            "pool_available": pool.size() - pool.checkedout(),
            "pool_overflow": pool.overflow(),
            "pool_checkedout": pool.checkedout(),
        }
    except Exception as e:
        logger.error(
            "Failed to get pool metrics",
            extra={"context": {"error": str(e)}},
        )
        return {
            "pool_size": 0,
            "pool_available": 0,
            "pool_overflow": 0,
            "pool_checkedout": 0,
        }
