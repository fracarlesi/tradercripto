# Research: Production-Ready Bitcoin Trading System Refactoring

**Feature**: 001-production-refactor
**Date**: 2025-10-31
**Purpose**: Resolve technical unknowns before design phase

## Overview

This document captures research findings for critical technical decisions required to implement the production refactoring. Each section follows the format: Decision → Rationale → Alternatives Considered.

## 1. Hyperliquid Python SDK Async Support

### Decision
**Use `asyncio.to_thread()` wrapper pattern** to run synchronous Hyperliquid SDK calls in thread pool without blocking FastAPI event loop.

### Rationale
1. **SDK is synchronous-only**: Examination of hyperliquid-python-sdk >=0.20.0 shows no native async/await support - all methods are synchronous
2. **Minimal changes required**: Can wrap existing `hyperliquid_trading_service` methods without rewriting business logic
3. **Performance acceptable**: Thread pool execution adds ~1-5ms overhead per call, well within p95 <200ms budget
4. **Type safety maintained**: Async wrapper preserves existing type hints and error handling

### Implementation Pattern
```python
import asyncio
from typing import TypeVar, Callable, Any

T = TypeVar('T')

async def run_in_thread(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run synchronous Hyperliquid SDK call in thread pool"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))

# Usage in service:
async def get_account_state_async(self, account: Account) -> dict:
    return await run_in_thread(self._sdk.get_user_state, account.wallet_address)
```

### Alternatives Considered
1. **Direct REST API calls with httpx.AsyncClient**: Rejected because SDK handles signing, nonce management, rate limiting automatically - reimplementing would be error-prone and maintenance burden
2. **Fork SDK to add async support**: Rejected - too much maintenance overhead, would diverge from official SDK updates
3. **Keep synchronous, use BackgroundTasks**: Rejected - doesn't solve event loop blocking, just defers it

### Testing Plan
- Load test wrapped implementation with 10+ concurrent requests to verify <200ms p95 latency (SC-006)
- Monitor thread pool exhaustion under concurrent load
- Validate no race conditions in SDK stateful operations

### References
- FastAPI Background Tasks: https://fastapi.tiangolo.com/tutorial/background-tasks/
- asyncio.to_thread(): https://docs.python.org/3/library/asyncio-task.html#asyncio.to_thread
- SQLAlchemy async with sync drivers pattern (similar use case)

---

## 2. Async SQLAlchemy 2.0 with Dual Database Support

### Decision
**Use SQLAlchemy 2.0 AsyncEngine with database URL environment variable** to support both PostgreSQL (asyncpg driver) and SQLite (aiosqlite driver) with minimal code duplication.

### Rationale
1. **Single codebase for both databases**: AsyncEngine + AsyncSession work identically for both databases, only connection string differs
2. **Feature parity acceptable**: SQLite async limitations (write contention, no JSONB) only affect unused scenarios in single-user context
3. **Migration path clear**: Alembic supports both databases with same migration files (DDL differences handled automatically)
4. **Connection pooling built-in**: AsyncEngine provides connection pooling for PostgreSQL (NullPool for SQLite)

### Implementation Pattern
```python
# backend/database/connection.py
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
import os

# Environment-based database selection
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data.db")

# PostgreSQL example: postgresql+asyncpg://user:pass@localhost/dbname
# SQLite example: sqlite+aiosqlite:///./data.db

engine = create_async_engine(
    DATABASE_URL,
    echo=os.getenv("SQL_DEBUG", "false").lower() == "true",
    pool_size=int(os.getenv("DB_POOL_SIZE", "10")),  # PostgreSQL only
    max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "5")),  # PostgreSQL only
    pool_timeout=30,  # FR-016: 30s queue timeout
    pool_pre_ping=True,  # Verify connections before use
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

async def get_db() -> AsyncSession:
    """Dependency for FastAPI endpoints"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
```

### PostgreSQL vs SQLite Feature Matrix

| Feature | PostgreSQL | SQLite | Impact |
|---------|-----------|--------|--------|
| Concurrent writes | ✅ Full support | ⚠️ Serialized (file locking) | Dev only - acceptable |
| JSONB type | ✅ Native | ❌ Requires TEXT serialization | Not currently used |
| Transaction isolation | ✅ All levels | ⚠️ Limited (SERIALIZABLE default) | Acceptable for sync operations |
| Connection pooling | ✅ Required | ❌ NullPool only | Dev only - acceptable |
| Online DDL | ✅ Most operations | ❌ Requires table rebuild | Migration downtime acceptable |

### Alternatives Considered
1. **Separate code paths for each database**: Rejected - duplicates all repository code, high maintenance burden
2. **PostgreSQL-only, no dev/prod parity**: Rejected - developers need local PostgreSQL setup, increases friction
3. **Use psycopg2 (sync) driver for PostgreSQL**: Rejected - defeats async architecture benefits (FR-012)

### Migration Strategy (SQLite → PostgreSQL)
1. **Export**: Use `sqlite3 .dump` or Python script to export data as SQL/JSON
2. **Schema**: Run Alembic migrations on empty PostgreSQL database
3. **Import**: Use `COPY` command or SQLAlchemy bulk insert to load data
4. **Verify**: Run data integrity checks comparing row counts, checksums

### Testing Plan
- Run identical test suite against both databases (FR-061)
- Verify migration script preserves 100% of data (SC-021)
- Load test PostgreSQL connection pool under concurrent load

### References
- SQLAlchemy 2.0 Async: https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html
- asyncpg driver: https://magicstack.github.io/asyncpg/
- aiosqlite driver: https://github.com/omnilib/aiosqlite

---

## 3. Docker Multi-Stage Build Optimization

### Decision
**Use 3-stage build: Node builder → Python builder → Slim runtime** with layer caching and non-root user (UID 1000) to achieve <500MB final image.

### Rationale
1. **Frontend build artifacts only**: Stage 1 builds React app, only dist/ copied to final image (~2MB)
2. **Python deps cached separately**: Stage 2 installs deps with uv, leveraging layer cache when requirements unchanged
3. **Slim base image**: python:3.11-slim (~150MB) vs python:3.11 (~1GB) - removes build tools, docs, unused packages
4. **Security**: Non-root user prevents container escape vulnerabilities

### Optimized Dockerfile
```dockerfile
# Stage 1: Frontend build
FROM node:20-alpine AS frontend-build
WORKDIR /app
RUN npm install -g pnpm
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ .
RUN pnpm run build
# Result: /app/dist (~2MB)

# Stage 2: Python dependencies
FROM python:3.11-slim AS python-deps
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev
# Result: /app/.venv (~200MB)

# Stage 3: Runtime
FROM python:3.11-slim
RUN useradd -m -u 1000 appuser  # Non-root user (FR-044)
WORKDIR /app

# Copy Python environment and backend code
COPY --from=python-deps /app/.venv /app/.venv
COPY --from=frontend-build /app/dist /app/static
COPY backend/ /app/

# Set up virtual environment
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
ENV PYTHONPATH=/app

# Switch to non-root user
USER appuser

# Health check (FR-045)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:5611/api/health', timeout=3).raise_for_status()" || exit 1

EXPOSE 5611
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5611"]
```

### Image Size Breakdown (Target)
- Base python:3.11-slim: ~150MB
- Python dependencies (.venv): ~200MB
- Application code: ~10MB
- Frontend static files: ~2MB
- **Total: ~362MB** ✅ Under 500MB target (FR-043)

### Alternatives Considered
1. **Alpine Linux base**: Rejected - musl libc incompatibility with some Python packages (numpy, pandas), slower builds
2. **Single-stage build**: Rejected - includes Node.js, build tools in final image (~800MB)
3. **Distroless base image**: Rejected - harder to debug, health check requires extra tooling

### Layer Caching Strategy
1. **Dependencies first**: COPY pyproject.toml before application code - cache invalidates only when deps change
2. **Separate frontend/backend**: Frontend changes don't invalidate Python layer cache
3. **Use BuildKit**: Enable `DOCKER_BUILDKIT=1` for parallel stage execution

### Testing Plan
- Measure final image size: `docker images | grep trader_bitcoin`
- Verify health check works: `docker inspect --format='{{.State.Health.Status}}' <container>`
- Test non-root permissions: `docker exec <container> whoami` should return `appuser`
- Verify build caching: Re-build after code change should skip dependency layers

### References
- Docker Multi-Stage Builds: https://docs.docker.com/build/building/multi-stage/
- Python slim images: https://hub.docker.com/_/python
- Security best practices: https://docs.docker.com/develop/security-best-practices/

---

## 4. FastAPI Async Architecture Best Practices

### Decision
**Use dependency injection with `Depends()` for service instances**, ensuring all I/O operations (database, HTTP, file) use async functions to prevent event loop blocking.

### Rationale
1. **Dependency injection enables testing**: Services can be mocked/replaced in tests without global state
2. **Async consistency**: Mixing sync/async code causes event loop blocking - all I/O must be async
3. **Database session lifecycle**: `Depends(get_db)` manages session creation/cleanup automatically
4. **Service lifecycle**: Lifespan context manager handles startup/shutdown (scheduler, connections)

### Async Patterns

#### Pattern 1: Async Route Handler
```python
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from database.connection import get_db
from services.hyperliquid_sync_service import HyperliquidSyncService

router = APIRouter()

@router.post("/api/sync/account/{account_id}")
async def sync_account(
    account_id: int,
    db: AsyncSession = Depends(get_db),
    sync_service: HyperliquidSyncService = Depends(),
):
    """Async endpoint: non-blocking I/O"""
    result = await sync_service.sync_account(db, account_id)
    return result
```

#### Pattern 2: Async Repository
```python
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from database.models import Account

class AccountRepository:
    async def get_by_id(self, db: AsyncSession, account_id: int) -> Account | None:
        result = await db.execute(select(Account).where(Account.id == account_id))
        return result.scalar_one_or_none()

    async def update_balance(self, db: AsyncSession, account: Account, balance: float):
        account.current_cash = balance
        db.add(account)  # Mark for update
        await db.commit()  # Async commit
        await db.refresh(account)  # Reload from DB
```

#### Pattern 3: Async Service with External API
```python
import asyncio
from typing import Dict

class HyperliquidSyncService:
    def __init__(self, trading_service: HyperliquidTradingService):
        self.trading_service = trading_service

    async def sync_account(self, db: AsyncSession, account_id: int) -> Dict:
        # Fetch account from DB (async)
        account = await self.account_repo.get_by_id(db, account_id)

        # Fetch from Hyperliquid (sync SDK → async wrapper)
        state = await asyncio.to_thread(
            self.trading_service._sdk.get_user_state,
            account.wallet_address
        )

        # Update database (async)
        await self.account_repo.update_balance(db, account, state['balance'])

        return {'success': True, 'balance': state['balance']}
```

### Concurrency Considerations

#### Database Connection Pool (FR-016)
- **Pool size**: 3-15 connections for single-user load (typical: 5-10 concurrent operations)
- **Queue timeout**: 30s with 503 response on exhaustion
- **Monitor**: Track pool utilization, alert if >70% sustained

#### APScheduler with Async
```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

scheduler = AsyncIOScheduler()

async def periodic_sync_job():
    """Async job function"""
    async with AsyncSessionLocal() as db:
        await sync_service.sync_all_accounts(db)

scheduler.add_job(
    periodic_sync_job,
    trigger=IntervalTrigger(seconds=30),  # FR-004: 30s sync interval
    id='hyperliquid_sync',
    replace_existing=True,
)

scheduler.start()
```

### Alternatives Considered
1. **Sync endpoints with ThreadPoolExecutor**: Rejected - loses async benefits, higher memory usage per request
2. **Background tasks for all I/O**: Rejected - still blocks on database queries, complicates error handling
3. **Celery for background jobs**: Rejected - overkill for single-user system, adds Redis/RabbitMQ dependency

### Common Pitfalls to Avoid
1. ❌ **Don't mix sync/async database sessions**: Use AsyncSession everywhere, not Session
2. ❌ **Don't use blocking I/O in async functions**: File operations, `requests` library → use `aiofiles`, `httpx`
3. ❌ **Don't forget connection pool limits**: Configure pool_size appropriately for expected load
4. ❌ **Don't block event loop**: CPU-intensive work → use `asyncio.to_thread()` or ProcessPoolExecutor

### Testing Plan
- Load test with 10+ concurrent requests: `locust` or `hey` tool
- Verify p95 latency <200ms (SC-006)
- Monitor event loop blocking: `asyncio.get_running_loop().slow_callback_duration`
- Check connection pool exhaustion handling

### References
- FastAPI Async: https://fastapi.tiangolo.com/async/
- SQLAlchemy Async ORM: https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html
- APScheduler AsyncIO: https://apscheduler.readthedocs.io/en/3.x/modules/schedulers/asyncio.html

---

## 5. AI Cost Optimization Strategies

### Decision
**Implement 3-tier caching: news feed (1h TTL) → prompt optimization (40% token reduction) → decision deduplication (10m window)** targeting 30%+ cost reduction.

### Rationale
1. **News caching highest impact**: News fetched every 3 minutes currently, 1-hour cache reduces calls by 20x (SC-012: 80% reduction)
2. **Prompt optimization cost-effective**: Summarizing news vs full text reduces tokens without accuracy loss (SC-013: 40% reduction)
3. **Decision deduplication prevents redundancy**: Identical market state → same decision, no need to call AI twice
4. **Monitoring validates savings**: Track API usage metrics to verify 30% reduction target (SC-011)

### Implementation Strategy

#### Tier 1: News Feed Caching
```python
from datetime import datetime, timedelta
from typing import List, Optional
import json

class NewsFeedCache:
    def __init__(self, ttl_seconds: int = 3600):  # FR-034: 1 hour TTL
        self.ttl_seconds = ttl_seconds
        self.cache: Optional[dict] = None
        self.cached_at: Optional[datetime] = None

    async def get_news(self, fetch_func) -> List[dict]:
        now = datetime.utcnow()

        # Check cache validity
        if self.cache and self.cached_at:
            age = (now - self.cached_at).total_seconds()
            if age < self.ttl_seconds:
                return self.cache['articles']

        # Cache miss - fetch fresh
        articles = await fetch_func()
        self.cache = {'articles': articles}
        self.cached_at = now
        return articles
```

**Impact**: News fetch every 3 minutes → every 60 minutes = 20x reduction (assumes API call per news fetch)

#### Tier 2: Prompt Optimization
```python
def optimize_ai_prompt(market_data: dict, news: List[dict]) -> str:
    # Before: Full news text (avg 500 tokens per article × 10 articles = 5000 tokens)
    # After: Summarize headlines + key points (avg 50 tokens per article × 10 = 500 tokens)

    news_summary = "\n".join([
        f"- {article['title']}: {article['summary'][:100]}..."
        for article in news[:10]  # Top 10 only
    ])

    prompt = f"""Analyze market for trading decision.

Current State:
- Balance: ${market_data['balance']:.2f}
- Position: {market_data['position']}
- BTC Price: ${market_data['price']:.2f}

Recent News (past hour):
{news_summary}

Decision (BUY/SELL/HOLD):"""

    return prompt
```

**Impact**: ~5000 tokens → ~1500 tokens per call = 70% reduction (exceeds 40% target)

#### Tier 3: Decision Deduplication
```python
import hashlib
from datetime import datetime, timedelta

class AIDecisionCache:
    def __init__(self, window_minutes: int = 10):  # FR-036: 10 minute window
        self.window_minutes = window_minutes
        self.cache: dict[str, dict] = {}  # market_hash → {decision, timestamp}

    def _hash_market_state(self, price: float, position: float, news_ids: List[str]) -> str:
        """Hash market state for deduplication"""
        state = f"{price:.2f}|{position:.4f}|{','.join(sorted(news_ids))}"
        return hashlib.md5(state.encode()).hexdigest()

    async def get_or_generate_decision(
        self,
        market_data: dict,
        ai_call_func
    ) -> dict:
        state_hash = self._hash_market_state(
            market_data['price'],
            market_data['position'],
            market_data['recent_news_ids']
        )

        # Check cache
        if state_hash in self.cache:
            cached = self.cache[state_hash]
            age = (datetime.utcnow() - cached['timestamp']).total_seconds()
            if age < self.window_minutes * 60:
                return cached['decision']

        # Generate new decision
        decision = await ai_call_func(market_data)
        self.cache[state_hash] = {
            'decision': decision,
            'timestamp': datetime.utcnow()
        }

        # Clean old cache entries
        cutoff = datetime.utcnow() - timedelta(minutes=self.window_minutes)
        self.cache = {
            k: v for k, v in self.cache.items()
            if v['timestamp'] > cutoff
        }

        return decision
```

**Impact**: Varies by market volatility (estimate 10-20% reduction in high-volatility periods)

### Cost Tracking Implementation
```python
class AIUsageTracker:
    def __init__(self):
        self.stats = {
            'calls_today': 0,
            'tokens_today': 0,
            'cache_hits': 0,
            'cache_misses': 0,
        }

    def log_ai_call(self, tokens_used: int, cache_hit: bool):
        self.stats['calls_today'] += 1
        self.stats['tokens_today'] += tokens_used
        if cache_hit:
            self.stats['cache_hits'] += 1
        else:
            self.stats['cache_misses'] += 1

    def estimate_monthly_cost(self, rate_per_1k_tokens: float = 0.0014) -> float:
        """DeepSeek pricing: $0.14 per 1M input tokens = $0.0014 per 1k"""
        daily_cost = (self.stats['tokens_today'] / 1000) * rate_per_1k_tokens
        return daily_cost * 30

    def get_metrics(self) -> dict:
        total_requests = self.stats['cache_hits'] + self.stats['cache_misses']
        cache_hit_rate = (self.stats['cache_hits'] / total_requests * 100) if total_requests > 0 else 0

        return {
            'calls_today': self.stats['calls_today'],
            'tokens_today': self.stats['tokens_today'],
            'estimated_monthly_cost': self.estimate_monthly_cost(),
            'cache_hit_rate_pct': cache_hit_rate,
        }
```

### Alternatives Considered
1. **Self-hosted DeepSeek**: Deferred pending cost analysis (FR-039) - requires GPU ($50-200/mo cloud GPU vs $10-30/mo API)
2. **Increase decision interval 3m → 5m**: Rejected - reduces opportunity capture, minimal savings vs caching
3. **Use cheaper model (GPT-3.5)**: Rejected - quality degradation unacceptable for trading decisions

### Testing Plan
- Baseline: Track current API usage for 7 days (calls/day, tokens/call, cost)
- Implement caching: Deploy optimizations, track for 30 days
- Verify: Cost reduction ≥30% (SC-011), trading performance maintained (win rate, return comparable)
- Monitor: Alert if cache hit rate <50% (indicates poor effectiveness)

### References
- DeepSeek Pricing: https://www.deepseek.com/pricing
- OpenAI Token Counting: https://platform.openai.com/tokenizer (similar model)
- Caching strategies: Redis TTL patterns, LRU cache

---

## 6. Alembic Migration Strategy

### Decision
**Use Alembic with autogenerate for schema migrations**, supporting both PostgreSQL and SQLite with environment-based configuration.

### Rationale
1. **Autogenerate reduces errors**: Alembic detects model changes automatically vs manual SQL writing
2. **Version control**: Migrations tracked in git, enables rollback and audit trail
3. **Dual database support**: Same migration files work for both databases (DDL differences handled automatically)
4. **Production safety**: Dry-run capability, transactional DDL (PostgreSQL), pre-migration backups

### Setup Configuration

#### alembic.ini (Project Root)
```ini
[alembic]
script_location = backend/alembic
prepend_sys_path = backend
sqlalchemy.url = env:DATABASE_URL  # Read from environment

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
```

#### backend/alembic/env.py
```python
import os
import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from database.models import Base  # Import all models

# Alembic Config object
config = context.config

# Interpret the config file for logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata for autogenerate
target_metadata = Base.metadata

# Database URL from environment
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data.db")

def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (generates SQL file)"""
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()

async def run_migrations_online() -> None:
    """Run migrations in 'online' mode (executes against live DB)"""
    connectable = create_async_engine(DATABASE_URL, poolclass=pool.NullPool)

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()

def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
```

### Migration Workflow

#### 1. Initial Setup
```bash
# Install Alembic
uv add alembic

# Initialize Alembic (already configured above)
alembic init backend/alembic

# Generate initial migration from existing models
alembic revision --autogenerate -m "Initial schema"

# Review migration file: backend/alembic/versions/001_initial_schema.py
```

#### 2. Schema Changes
```bash
# Make changes to database/models.py (e.g., add column)
# Generate migration
alembic revision --autogenerate -m "Add column: last_sync_time to accounts"

# Review generated migration
cat backend/alembic/versions/002_add_last_sync_time.py

# Apply migration (dev)
alembic upgrade head

# Rollback if needed
alembic downgrade -1
```

#### 3. Production Deployment
```bash
# Backup database
pg_dump -U user -d trader_db > backup_$(date +%Y%m%d_%H%M%S).sql

# Dry-run migration (generates SQL)
alembic upgrade head --sql > migration.sql
# Review migration.sql manually

# Apply migration
alembic upgrade head

# Verify schema
psql -U user -d trader_db -c "\d accounts"
```

### SQLite → PostgreSQL Migration Script
```python
# backend/scripts/migrate_sqlite_to_postgres.py
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from database.models import Base, Account, Position, Order, Trade

async def migrate():
    # Source: SQLite
    sqlite_engine = create_async_engine("sqlite+aiosqlite:///./data.db")
    SQLiteSession = sessionmaker(sqlite_engine, class_=AsyncSession, expire_on_commit=False)

    # Target: PostgreSQL
    postgres_engine = create_async_engine("postgresql+asyncpg://user:pass@localhost/trader_db")
    PostgresSession = sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)

    # Create schema in PostgreSQL
    async with postgres_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Migrate data
    async with SQLiteSession() as src_session, PostgresSession() as dst_session:
        # Accounts
        accounts = (await src_session.execute(select(Account))).scalars().all()
        for account in accounts:
            dst_session.add(Account(**account.__dict__))

        # Positions (linked to accounts)
        positions = (await src_session.execute(select(Position))).scalars().all()
        for position in positions:
            dst_session.add(Position(**position.__dict__))

        # Orders and Trades (similar)
        # ...

        await dst_session.commit()

    print(f"Migrated {len(accounts)} accounts, {len(positions)} positions")

if __name__ == "__main__":
    asyncio.run(migrate())
```

### Alternatives Considered
1. **Manual SQL migrations**: Rejected - error-prone, no version control, dual database support difficult
2. **Django migrations**: Rejected - tightly coupled to Django ORM, not compatible with SQLAlchemy
3. **SQLAlchemy-migrate (deprecated)**: Rejected - unmaintained, Alembic is official successor

### Testing Plan
- Test migration on copy of production SQLite database
- Verify data integrity: row counts, foreign keys, constraints
- Test rollback capability: `alembic downgrade -1` and re-upgrade
- Dry-run production migration, review SQL manually

### References
- Alembic Tutorial: https://alembic.sqlalchemy.org/en/latest/tutorial.html
- Autogenerate: https://alembic.sqlalchemy.org/en/latest/autogenerate.html
- Async migrations: https://alembic.sqlalchemy.org/en/latest/cookbook.html#using-asyncio-with-alembic

---

## Summary of Decisions

| Area | Decision | Primary Benefit | Risk Mitigation |
|------|----------|----------------|-----------------|
| Hyperliquid SDK | `asyncio.to_thread()` wrapper | Non-blocking I/O without SDK fork | Load testing, monitor thread pool |
| Database | Async SQLAlchemy 2.0 + dual support | Single codebase for dev/prod | Feature matrix documents limitations |
| Docker | 3-stage multi-stage build | <500MB image, layer caching | Measure size, test health checks |
| Async Architecture | Dependency injection + async everywhere | Testable, non-blocking | Avoid common pitfalls (documented) |
| AI Cost | 3-tier caching (news, prompt, decision) | 30%+ cost reduction | Track metrics, validate savings |
| Migrations | Alembic autogenerate | Version control, rollback, dual DB | Dry-run, backups, manual review |

**Next Steps**: Proceed to Phase 1 (Design & Contracts) - all technical unknowns resolved.
