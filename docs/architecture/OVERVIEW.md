# System Architecture - Bitcoin Trading System

## Overview

This document describes the architecture of the Bitcoin Trading System, a modern async-first FastAPI application that integrates with Hyperliquid DEX for automated cryptocurrency trading with AI-driven decision making.

## System Design

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Frontend (React)                         │
│                    WebSocket + REST API Client                   │
└────────────────┬────────────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                      FastAPI Backend (main.py)                   │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │                    API Routes Layer                         │ │
│  │  • accounts_async.py  • orders_async.py                    │ │
│  │  • market_data_async.py  • sync_routes.py                  │ │
│  │  • ai_routes.py  • health_routes.py                        │ │
│  │  • ws_async.py (WebSocket)                                 │ │
│  └─────────────┬──────────────────────────────────────────────┘ │
│                │                                                  │
│                ▼                                                  │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │                   Services Layer                            │ │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐ │ │
│  │  │   Trading    │  │ Market Data  │  │ Infrastructure   │ │ │
│  │  │   Services   │  │   Services   │  │    Services      │ │ │
│  │  └──────────────┘  └──────────────┘  └──────────────────┘ │ │
│  └─────────────┬──────────────────────────────────────────────┘ │
│                │                                                  │
│                ▼                                                  │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │                  Repository Layer                           │ │
│  │  • AccountRepository  • PositionRepository                 │ │
│  │  • OrderRepository    • TradeRepository                    │ │
│  └─────────────┬──────────────────────────────────────────────┘ │
└────────────────┼────────────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                    PostgreSQL Database                           │
│              (SQLAlchemy 2.0 async ORM)                         │
└─────────────────────────────────────────────────────────────────┘

External Integrations:
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│  Hyperliquid DEX │  │  DeepSeek AI API │  │  CoinJournal RSS │
│  (Trading)       │  │  (Decisions)     │  │  (News Feed)     │
└──────────────────┘  └──────────────────┘  └──────────────────┘
```

## Data Source Strategy

### Hyperliquid as Single Source of Truth ✅

**Principle**: Hyperliquid DEX is the authoritative source for all trading data. The local PostgreSQL database acts as a **synchronized cache** for display and analysis.

**Trading Mode**: Real trading only - all orders executed on Hyperliquid DEX.

### Data Flow

**AI-Driven Trading Flow:**
```
AI Analysis → Decision → Hyperliquid Order → Immediate Sync → DB Update
                              ↓
                       Periodic Sync (60s)
```

**Steps:**
1. AI analyzes market data, news, and portfolio
2. AI makes trading decision (logged to `ai_decision_logs`)
3. Order placed on Hyperliquid via `place_market_order()`
4. **Immediate sync** after order execution
5. **Periodic sync** every 60 seconds for consistency
6. Database reflects current Hyperliquid state

**Key Points:**
- ✅ All orders executed on Hyperliquid
- ✅ Database synced from Hyperliquid (clear & recreate)
- ✅ Hyperliquid is source of truth for balance, positions, orders, trades
- ✅ AI decisions stored locally for analysis

## Async Architecture Patterns

### Async/Await Throughout the Stack

```python
# API Layer
@router.get("/accounts/{account_id}")
async def get_account(
    account_id: int,
    db: AsyncSession = Depends(get_db)
) -> Account:
    return await AccountRepository.get_by_id(db, account_id)

# Repository Layer
class AccountRepository:
    @staticmethod
    async def get_by_id(db: AsyncSession, account_id: int) -> Account | None:
        result = await db.execute(
            select(Account).where(Account.id == account_id)
        )
        return result.scalar_one_or_none()
```

### Database Connection Pool

**Configuration:**
```python
engine = create_async_engine(
    DATABASE_URL,
    pool_size=10,          # Base pool
    max_overflow=5,        # Additional connections
    pool_timeout=30,       # Timeout before error
    pool_pre_ping=True,    # Health check
)
```

**Pool Management:**
- Base pool: 10 connections
- Max overflow: 5 additional (total 15)
- Timeout: 30 seconds → PoolExhaustedException
- Pre-ping: Validates before use
- Returns 503 + Retry-After: 10 on exhaustion

### Async Context Managers

**Lifespan Management:**
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    Base.metadata.create_all(bind=engine)
    initialize_services()
    scheduler_service.start()

    yield  # App running

    # Shutdown
    scheduler_service.stop()
    shutdown_services()
```

## Database Schema

### Core Tables

**accounts** - Trading accounts
```sql
id, user_id, name, account_type, model, api_key,
initial_capital, current_cash, frozen_cash, is_active
```

**positions** - Open positions (cleared & recreated on sync)
```sql
id, account_id, symbol, market, quantity,
average_price, current_price, unrealized_pnl
UNIQUE(account_id, symbol, market)
```

**orders** - Historical orders (deduplicated by order_id)
```sql
id, account_id, order_id (UNIQUE), symbol, market,
side, order_type, price, quantity, filled_quantity, status
```

**trades** - Execution history (deduplicated by trade_id)
```sql
id, account_id, trade_id (UNIQUE), order_id,
symbol, market, side, price, quantity, commission, realized_pnl
```

### Indexes for Performance

```sql
-- Account lookups
CREATE INDEX idx_accounts_user_id ON accounts(user_id);
CREATE INDEX idx_accounts_is_active ON accounts(is_active);

-- Position queries
CREATE INDEX idx_positions_account_id ON positions(account_id);

-- Order tracking
CREATE INDEX idx_orders_account_id ON orders(account_id);
CREATE INDEX idx_orders_order_id ON orders(order_id);

-- Trade history
CREATE INDEX idx_trades_account_id ON trades(account_id);
CREATE INDEX idx_trades_trade_id ON trades(trade_id);
CREATE INDEX idx_trades_executed_at ON trades(executed_at DESC);
```

## Sync Algorithm

### Synchronization Strategy

**Frequency:** Every 60 seconds (configurable via SYNC_INTERVAL_SECONDS)

**Sync Flow:**
```
1. Fetch account state from Hyperliquid
2. BEGIN TRANSACTION
3. Update account balance (current_cash, frozen_cash)
4. DELETE all positions for account
5. INSERT positions from Hyperliquid state
6. UPSERT orders (deduplicate by order_id)
7. UPSERT trades (deduplicate by trade_id)
8. Update sync_state (success, timestamp, duration)
9. COMMIT TRANSACTION
```

**Implementation:**
```python
async def sync_account(db: AsyncSession, account: Account):
    try:
        # Fetch from Hyperliquid
        hl_state = await hyperliquid_service.get_account_state(account)

        # Update balance
        account.current_cash = Decimal(str(hl_state["cash"]))
        account.frozen_cash = Decimal(str(hl_state["frozen"]))

        # Clear & recreate positions
        await PositionRepository.clear_positions(db, account.id)
        await PositionRepository.bulk_create_positions(
            db, account.id, hl_state["positions"]
        )

        # Deduplicate and upsert
        await OrderRepository.bulk_create_orders(
            db, account.id, hl_state["orders"]
        )
        await TradeRepository.bulk_create_trades(
            db, account.id, hl_state["trades"]
        )

        await db.commit()
        sync_state_tracker.record_success(account.id)

    except Exception as e:
        await db.rollback()
        sync_state_tracker.record_failure(account.id, str(e))
        raise SyncException(f"Sync failed: {e}")
```

### Deduplication

**Orders:** Check `order_id` uniqueness, update if exists
**Trades:** Use `ON CONFLICT DO NOTHING` with `trade_id` index

### Circuit Breaker

- Opens after 5 consecutive failures
- Timeout: 60 seconds
- Half-open: Test with single request
- Close: Success restores operation

## Error Handling

### Exception Hierarchy

```python
TradingException
├── SyncException (Hyperliquid sync failed)
├── AIException (AI decision service error)
├── DatabaseException
│   └── PoolExhaustedException (Pool exhausted)
└── APIException
    ├── RateLimitException (Rate limit exceeded)
    └── CircuitBreakerOpenException (Circuit open)
```

### Recovery Strategies

1. **Retry with Exponential Backoff** - Max 3 attempts, 2^n delay
2. **Graceful Degradation** - Fallback to cached/stale data
3. **Circuit Breaker** - Prevent cascade failures

## AI Cost Optimization

### Caching Layers

1. **News Cache** (1-hour TTL)
   - Reduces API calls 20x
   - Thread-safe with Lock
   - Returns stale on failure

2. **Decision Cache** (10-minute window)
   - MD5 hash of market state
   - ~30% API call reduction
   - Deduplicates identical conditions

3. **Price Cache** (30-second TTL)
   - In-memory storage
   - Automatic cleanup

### Usage Tracking

- Daily metrics: calls, tokens, costs
- Midnight reset via cron job
- Projections: daily → monthly → yearly
- Endpoint: GET `/api/ai/usage`
- Analysis: `analyze_ai_costs.py`

## Monitoring

### Metrics (Prometheus)

```python
sync_success_total = Counter("trading_system_sync_success_total")
sync_failure_total = Counter("trading_system_sync_failure_total")
sync_duration = Histogram("trading_system_sync_duration_seconds")
db_pool_available = Gauge("trading_system_db_pool_available")
ai_cache_hits = Counter("trading_system_ai_cache_hits_total")
```

### Health Checks

**GET /api/health** - Basic health (always 200)
**GET /api/ready** - Readiness (DB + sync status)

### Structured Logging

```python
logger.info(json.dumps({
    "operation": "sync_account",
    "account_id": account_id,
    "duration_ms": duration,
    "success": True,
    "timestamp": datetime.now().isoformat()
}))
```

## Deployment

### Docker Compose

```yaml
services:
  app:
    build: ./backend
    ports: ["5611:5611"]
    environment:
      - DATABASE_URL=postgresql+asyncpg://...
      - DB_POOL_SIZE=10
    depends_on: [postgres]

  postgres:
    image: postgres:15-alpine
    volumes: [postgres_data:/var/lib/postgresql/data]
```

### Zero-Downtime Deployment

```bash
# Blue-green strategy
1. Scale to 2 instances (blue + green)
2. Health check green instance
3. Route traffic to green (Traefik)
4. Scale down blue
```

### Backup Strategy

- Daily: 2 AM, 7-day retention
- Weekly: Sunday, 4-week retention
- Compressed with gzip
- S3 upload for off-site

## Security

- **API Keys**: Encrypted storage, never logged
- **Database**: SSL connections, parameterized queries
- **Rate Limiting**: Per-account + global limits
- **Input Validation**: Pydantic models, type checking

## Performance Optimization

### Query Optimization
- Indexed columns
- Bulk operations
- N+1 prevention with joins

### Connection Pooling
- Pre-warmed: 10 connections
- Overflow: 5 additional
- Recycling: 1 hour

## Configuration

### Environment Variables

```bash
# Database
DATABASE_URL=postgresql+asyncpg://user:pass@localhost/trading_db
DB_POOL_SIZE=10
DB_MAX_OVERFLOW=5

# Hyperliquid
HYPERLIQUID_API_KEY=your_key
HYPERLIQUID_WALLET_ADDRESS=0x...
HYPERLIQUID_PRIVATE_KEY=your_private_key

# AI
DEEPSEEK_API_KEY=your_key
DEEPSEEK_BASE_URL=https://api.deepseek.com

# Sync
SYNC_INTERVAL_SECONDS=60

# CORS
CORS_ORIGINS=*
```

## Best Practices

### ✅ DO:
1. Trust Hyperliquid as source of truth
2. Use database as display cache
3. Let periodic sync maintain consistency
4. Store AI decisions locally for analysis
5. Monitor sync_state for failures

### ❌ DON'T:
1. Create orders in database directly
2. Modify positions outside sync
3. Trust local balance over Hyperliquid
4. Skip error handling on sync
5. Ignore circuit breaker state

## Troubleshooting

### Balance Mismatch
```bash
# Check sync status
curl http://localhost:5611/api/sync/status

# Manual sync
python scripts/maintenance/sync_balance.py
```

### Sync Failures
- Check Hyperliquid API status
- Verify API key and credentials
- Review logs: `docker-compose logs -f app | grep sync`
- Check circuit breaker state

### High AI Costs
```bash
# Analyze costs
python scripts/maintenance/analyze_ai_costs.py

# Check cache hit rates
curl http://localhost:5611/api/ai/usage
```

## Future Enhancements

1. **Horizontal Scaling**: Redis cache, message queues, load balancing
2. **Advanced Monitoring**: Grafana dashboards, distributed tracing
3. **Performance**: Read replicas, CDN, connection pooling tuning
4. **Security**: Vault integration, mTLS, audit logging

## References

- [FastAPI Docs](https://fastapi.tiangolo.com/)
- [SQLAlchemy 2.0 Async](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)
- [Hyperliquid API](https://hyperliquid.xyz/docs)
- [PostgreSQL Performance](https://www.postgresql.org/docs/current/performance-tips.html)
