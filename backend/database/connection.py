"""Async database connection and session management."""

from collections.abc import AsyncGenerator
from typing import Any

from config.settings import settings
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool, QueuePool


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
        # SQLite configuration
        engine_kwargs["poolclass"] = NullPool  # SQLite doesn't support connection pooling well
        engine_kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # PostgreSQL configuration
        engine_kwargs["poolclass"] = QueuePool
        engine_kwargs["pool_size"] = settings.db_pool_size
        engine_kwargs["max_overflow"] = settings.db_max_overflow
        engine_kwargs["pool_timeout"] = settings.db_pool_timeout
        engine_kwargs["pool_pre_ping"] = True  # Verify connections before using

    return create_async_engine(settings.database_url, **engine_kwargs)


# Global async engine instance
engine = create_engine()

# Async session factory
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

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

    Example:
        @app.get("/accounts")
        async def get_accounts(db: AsyncSession = Depends(get_db)):
            result = await db.execute(select(Account))
            return result.scalars().all()
    """
    from services.exceptions import PoolExhaustedException
    from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

    try:
        async with async_session_factory() as session:
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
    await engine.dispose()
