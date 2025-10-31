# Bitcoin Trading System - Backend

Modern async FastAPI-based cryptocurrency trading system with Hyperliquid integration and AI-driven decision making.

## Architecture Overview

### Technology Stack
- **Python**: 3.11+ (currently 3.13)
- **Web Framework**: FastAPI with async/await patterns
- **Database**: PostgreSQL with SQLAlchemy 2.0 (async ORM)
- **Trading Platform**: Hyperliquid DEX via hyperliquid-python-sdk >=0.20.0
- **AI Model**: DeepSeek API for trading decisions
- **Task Scheduling**: APScheduler for periodic sync and maintenance
- **Server**: uvicorn with WebSocket support

### Key Features
- **Async-First Design**: All database operations and API calls use async/await
- **Real-Time Sync**: Periodic synchronization with Hyperliquid every 60 seconds
- **AI-Driven Trading**: Automated trading decisions based on market data and news
- **Cost Optimization**: Multi-layer caching (news: 1h TTL, decisions: 10m window)
- **Connection Pooling**: Efficient database connection management
- **Type Safety**: Comprehensive type hints with mypy strict mode
- **Test Coverage**: Unit and integration tests with pytest

## Project Structure

```
backend/
├── api/                    # FastAPI route handlers
│   ├── accounts_async.py   # Account management endpoints (async)
│   ├── ai_routes.py        # AI usage tracking endpoints
│   ├── health_routes.py    # Health check and readiness
│   ├── market_data_async.py # Market data endpoints (async)
│   ├── orders_async.py     # Order endpoints (async)
│   ├── sync_routes.py      # Hyperliquid sync endpoints
│   └── ws_async.py         # WebSocket for real-time updates
├── database/               # Database models and connection
│   ├── connection.py       # AsyncEngine with pool config
│   └── models.py           # SQLAlchemy models
├── repositories/           # Data access layer (async)
│   ├── account_repo.py
│   ├── position_repo.py
│   ├── order_repo.py
│   └── trade_repo.py
├── services/               # Business logic
│   ├── market_data/        # Market data services
│   │   ├── news_cache.py   # News feed caching (1h TTL)
│   │   ├── news_feed.py    # CoinJournal RSS fetcher
│   │   ├── price_cache.py  # Price caching (30s TTL)
│   │   └── hyperliquid_market_data.py
│   ├── trading/            # Trading services
│   │   ├── hyperliquid_sync_service.py  # Sync with Hyperliquid
│   │   ├── hyperliquid_trading_service.py
│   │   └── sync_jobs.py    # Scheduled sync tasks
│   ├── infrastructure/     # Infrastructure services
│   │   ├── scheduler.py    # APScheduler wrapper
│   │   ├── usage_tracker.py # AI API cost tracking
│   │   ├── pool_metrics.py  # DB connection pool metrics
│   │   └── sync_state_tracker.py
│   ├── ai_decision_service.py  # AI trading decisions
│   └── startup.py          # Service initialization
├── scripts/                # Utility scripts
│   ├── debug/              # Debug and testing scripts
│   ├── maintenance/        # Maintenance utilities
│   │   ├── analyze_ai_costs.py
│   │   ├── backup_db.sh
│   │   └── restore_db.sh
│   └── deployment/         # Deployment scripts
│       └── deploy.sh       # Zero-downtime blue-green deploy
├── tests/                  # Test suite
│   ├── unit/               # Unit tests
│   │   ├── test_news_cache.py
│   │   └── test_repositories.py
│   └── integration/        # Integration tests
├── main.py                 # FastAPI application entry point
└── pyproject.toml         # Project dependencies and config
```

## Async Architecture

### Database Operations
All database operations use SQLAlchemy 2.0 async patterns:

```python
from sqlalchemy.ext.asyncio import AsyncSession
from repositories.account_repo import AccountRepository

async def get_account(db: AsyncSession, account_id: int):
    return await AccountRepository.get_by_id(db, account_id)
```

### Connection Pool Configuration
- **Pool Size**: 10 connections (configurable via `DB_POOL_SIZE`)
- **Max Overflow**: 5 additional connections (configurable via `DB_MAX_OVERFLOW`)
- **Pool Timeout**: 30 seconds
- **Pre-Ping**: Enabled for connection health checks
- **Pool Exhaustion**: Returns 503 + `Retry-After: 10` header

### API Routes
All modern routes use async patterns for non-blocking I/O:

```python
@router.get("/accounts/{account_id}")
async def get_account(account_id: int, db: AsyncSession = Depends(get_db)):
    account = await AccountRepository.get_by_id(db, account_id)
    return account
```

## Sync Strategy

### Hyperliquid Synchronization
- **Frequency**: Every 60 seconds (configurable via `SYNC_INTERVAL_SECONDS`)
- **Source of Truth**: Hyperliquid DEX is authoritative
- **Clear & Recreate**: Positions/orders/trades cleared and recreated from Hyperliquid
- **Deduplication**: Orders and trades deduplicated by `order_id` and `trade_id`
- **Retry Logic**: Exponential backoff with circuit breaker (5 failures → open)
- **Error Handling**: Graceful degradation, errors logged but don't stop sync

### Sync Flow
1. Fetch account balance from Hyperliquid
2. Update account current_cash and frozen_cash
3. Clear existing positions
4. Bulk create positions from Hyperliquid state
5. Bulk create/update orders (deduplicated)
6. Bulk create trades (deduplicated)
7. Track sync state (success/failure, duration, timestamp)

## AI Cost Optimization

### Caching Layers
1. **News Cache** (1-hour TTL)
   - Reduces news API calls from every 3 min → every 60 min (20x reduction)
   - Thread-safe with Lock
   - Graceful degradation: returns stale cache on fetch failure

2. **Decision Cache** (10-minute window)
   - Caches AI decisions based on MD5 hash of market state
   - Avoids duplicate API calls for same market conditions
   - State hash includes: price, position, news summary

3. **Price Cache** (30-second TTL)
   - Caches market prices to reduce API calls
   - Automatic expiration and cleanup

### Usage Tracking
- Daily metrics: API calls, tokens (input/output), costs
- Automatic reset at midnight via cron job
- Cost projection: daily → monthly → yearly
- GET `/api/ai/usage` endpoint for monitoring
- Analysis script: `python scripts/maintenance/analyze_ai_costs.py`

## Deployment

### Development
```bash
# Install dependencies
uv sync

# Run migrations
alembic upgrade head

# Start server
uvicorn main:app --reload --port 5611
```

### Production (Docker)
```bash
# Build and start services
docker-compose up -d --build

# Check health
curl http://localhost:5611/api/health

# View logs
docker-compose logs -f app

# Zero-downtime deployment
./scripts/deployment/deploy.sh
```

### Environment Variables
```bash
# Database
DATABASE_URL=postgresql+asyncpg://user:pass@localhost/trading_db
DB_POOL_SIZE=10
DB_MAX_OVERFLOW=5

# Hyperliquid
HYPERLIQUID_API_KEY=your_api_key
HYPERLIQUID_WALLET_ADDRESS=0x...
HYPERLIQUID_PRIVATE_KEY=your_private_key

# AI Model
DEEPSEEK_API_KEY=your_api_key
DEEPSEEK_BASE_URL=https://api.deepseek.com

# Sync Configuration
SYNC_INTERVAL_SECONDS=60

# CORS
CORS_ORIGINS=*  # Or comma-separated list: http://localhost:5173,https://app.example.com
```

## Testing

### Run Tests
```bash
# All tests
pytest

# Unit tests only
pytest tests/unit/

# With coverage
pytest --cov --cov-report=html --cov-report=term

# Specific test file
pytest tests/unit/test_news_cache.py -v
```

### Test Coverage
- **Target**: 70%+ for critical services
- **Current**: Test infrastructure established
  - news_cache.py: 23.88% covered
  - 8 passing tests for NewsFeedCache
  - Repository test suite created

## Monitoring

### Health Checks
- **GET /api/health**: Basic health check (always returns 200)
- **GET /api/ready**: Readiness check (database connection, sync status)

### Metrics
- Database connection pool metrics
- Sync success/failure rates
- AI API usage and costs
- Cache hit rates (news, decisions, prices)

### Logging
- Structured JSON logging (production)
- Request ID tracking
- Error stack traces
- Performance metrics (sync duration, API latency)

## Maintenance

### Database Backup
```bash
# Manual backup
./scripts/maintenance/backup_db.sh

# Restore from backup
./scripts/maintenance/restore_db.sh <backup_file>
```

### AI Cost Analysis
```bash
# Analyze last 30 days
python scripts/maintenance/analyze_ai_costs.py

# Custom analysis
python scripts/maintenance/analyze_ai_costs.py --days 7 --baseline 2.00 --output weekly_report.csv
```

## Development Guidelines

### Type Hints
All code uses Python 3.11+ type hints:
```python
from typing import Optional, List, Dict, Any
from decimal import Decimal

async def update_balance(
    db: AsyncSession,
    account_id: int,
    new_balance: Decimal
) -> Optional[Account]:
    ...
```

### Docstrings
Google-style docstrings for all public functions:
```python
def calculate_cost(tokens: int, provider: str = "deepseek") -> float:
    """
    Calculate API call cost based on token count.

    Args:
        tokens: Number of tokens used
        provider: AI provider name (default: "deepseek")

    Returns:
        Cost in USD

    Raises:
        ValueError: If provider is unknown
    """
    ...
```

### Code Quality
- **Linter**: ruff (`ruff check backend/`)
- **Formatter**: ruff (`ruff format backend/`)
- **Type Checker**: mypy (`mypy --strict backend/services/`)
- **Tests**: pytest with coverage reporting

## Troubleshooting

### Common Issues

**Database connection pool exhausted**
- Symptom: 503 errors with "Pool exhausted" message
- Solution: Increase `DB_POOL_SIZE` or `DB_MAX_OVERFLOW`
- Check: Review slow queries, ensure connections are properly closed

**Sync failures**
- Check Hyperliquid API status
- Verify API key and wallet address
- Review logs: `docker-compose logs -f app | grep sync`
- Check circuit breaker state: GET `/api/sync/status`

**High AI costs**
- Review cache hit rates: GET `/api/ai/usage`
- Check if caches are working (news: 1h TTL, decisions: 10m)
- Run cost analysis: `python scripts/maintenance/analyze_ai_costs.py`
- Consider adjusting trading frequency

## Contributing

1. Follow existing code patterns
2. Add type hints to all new code
3. Write docstrings for public functions
4. Add tests for new features
5. Run quality checks before committing:
   ```bash
   ruff check backend/
   ruff format backend/
   mypy --strict backend/services/
   pytest --cov
   ```

## License

See LICENSE file in repository root.
