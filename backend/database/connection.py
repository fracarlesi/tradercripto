"""Async database connection and session management."""

from collections.abc import AsyncGenerator
from typing import Any

from config.settings import settings
from sqlalchemy import create_engine as create_sync_engine, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool, StaticPool


def _set_sqlite_pragma(dbapi_conn, connection_record):
    """Enable WAL mode and other performance optimizations for SQLite."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")  # Enable Write-Ahead Logging
    cursor.execute("PRAGMA synchronous=NORMAL")  # Balance between safety and speed
    cursor.execute("PRAGMA busy_timeout=30000")  # 30 seconds timeout
    cursor.close()


def create_engine() -> AsyncEngine:
    """Create async database engine with appropriate configuration.

    Returns:
        AsyncEngine configured for either SQLite (dev) or PostgreSQL (prod)
    """
    is_sqlite = settings.database_url.startswith("sqlite")

    engine_kwargs: dict[str, Any] = {
        "echo": settings.sql_debug,
        "future": True,
    }

    if is_sqlite:
        # SQLite configuration - use StaticPool for async to share single connection
        # StaticPool maintains ONE persistent connection shared across all sessions
        # This prevents "database is locked" errors in async environments
        engine_kwargs["poolclass"] = StaticPool
        engine_kwargs["connect_args"] = {
            "check_same_thread": False,
            "timeout": 30.0,  # Increased timeout for concurrent access (default is 5.0)
        }
    else:
        # PostgreSQL configuration (asyncpg uses its own async pool)
        # Do NOT set poolclass for async engines - let SQLAlchemy use AsyncAdaptedQueuePool
        engine_kwargs["pool_size"] = settings.db_pool_size
        engine_kwargs["max_overflow"] = settings.db_max_overflow
        engine_kwargs["pool_timeout"] = settings.db_pool_timeout
        engine_kwargs["pool_pre_ping"] = True  # Verify connections before using

    engine = create_async_engine(settings.database_url, **engine_kwargs)

    # Register SQLite optimizations event listener
    if is_sqlite:
        event.listen(engine.sync_engine, "connect", _set_sqlite_pragma)

    return engine


# Global async engine instance - initialized in lifespan, NOT at import time
# This prevents "Task got Future attached to a different loop" errors
engine: AsyncEngine | None = None
async_session_factory: async_sessionmaker | None = None


def init_async_engine() -> tuple[AsyncEngine, async_sessionmaker]:
    """Initialize async engine and session factory.

    Must be called inside FastAPI lifespan to bind to correct event loop.

    Returns:
        Tuple of (engine, async_session_factory)
    """
    global engine, async_session_factory

    engine = create_engine()
    async_session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )

    return engine, async_session_factory


def get_async_session_factory() -> async_sessionmaker:
    """Get the async session factory.

    Raises:
        RuntimeError: If engine not initialized (lifespan not started)
    """
    if async_session_factory is None:
        raise RuntimeError(
            "Database not initialized. Ensure FastAPI lifespan has started."
        )
    return async_session_factory

# Legacy sync engine and session for compatibility (used in main.py startup)
# Convert sync database URL for sync engine
sync_database_url = settings.database_url.replace("+aiosqlite", "").replace("+asyncpg", "+psycopg2")
sync_engine = create_sync_engine(sync_database_url, echo=settings.sql_debug)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=sync_engine)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency injection for FastAPI routes.

    Yields:
        AsyncSession instance for database operations

    Raises:
        PoolExhaustedException: When connection pool is exhausted
        RuntimeError: If database not initialized

    Example:
        @app.get("/accounts")
        async def get_accounts(db: AsyncSession = Depends(get_db)):
            result = await db.execute(select(Account))
            return result.scalars().all()
    """
    from services.exceptions import PoolExhaustedException
    from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

    session_factory = get_async_session_factory()

    try:
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()
    except SQLAlchemyTimeoutError as e:
        # Connection pool exhausted (T057)
        raise PoolExhaustedException(
            "Database connection pool exhausted. All connections are in use."
        ) from e


async def init_db() -> None:
    """Initialize database schema.

    Note: In production, use Alembic migrations instead.
    This is only for development/testing.
    """
    from .models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose_engine() -> None:
    """Dispose database engine on application shutdown."""
    global engine, async_session_factory

    if engine is not None:
        await engine.dispose()
        engine = None
        async_session_factory = None
