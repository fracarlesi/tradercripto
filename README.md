# Bitcoin Trading System

Modern async-first cryptocurrency trading system with Hyperliquid DEX integration and AI-driven decision making.

## Overview

This system provides **automated cryptocurrency trading** on Hyperliquid DEX with intelligent AI-powered decisions using DeepSeek. Built with FastAPI and async/await patterns throughout, it features real-time synchronization, comprehensive monitoring, and production-ready deployment options.

**Key Features:**
- **Real Trading on Hyperliquid DEX** - All orders executed on live exchange
- **AI-Driven Decisions** - DeepSeek API analyzes market data and news
- **Self-Learning System** - AI learns from past decisions via counterfactual analysis
- **Async Architecture** - SQLAlchemy 2.0 async ORM with connection pooling
- **Real-Time Sync** - Periodic synchronization with Hyperliquid (60s interval)
- **Cost Optimization** - Multi-layer caching (news: 1h, decisions: 10m, prices: 30s)
- **Production Ready** - Docker deployment, monitoring, backups, and health checks

## Technology Stack

### Backend
- **Python**: 3.11+ (currently 3.13)
- **Web Framework**: FastAPI with async/await patterns
- **Database**: PostgreSQL with SQLAlchemy 2.0 (async ORM)
- **Trading Platform**: Hyperliquid DEX via `hyperliquid-python-sdk >=0.20.0`
- **AI Model**: DeepSeek API for trading decisions
- **Task Scheduling**: APScheduler for periodic sync and maintenance
- **Server**: uvicorn with WebSocket support

### Frontend
- **React** with TypeScript
- **Real-time updates** via WebSocket
- **REST API** client for data fetching

### Infrastructure
- **Docker & Docker Compose** for containerization
- **PostgreSQL 14+** for production database
- **Traefik** for reverse proxy and SSL/TLS
- **Prometheus & Grafana** for monitoring (optional)

## Quick Start

### Prerequisites
- **Python**: 3.11+ and `uv` package manager
- **Node.js**: 20+ and `pnpm`
- **Docker**: 20+ with Docker Compose V2
- **PostgreSQL**: 12+ (production) or SQLite (development)
- **Hyperliquid Account**: Private key and wallet address
- **DeepSeek API Key**: For AI trading decisions

### Installation

**For detailed setup instructions**, see [📘 Quickstart Guide](specs/001-production-refactor/quickstart.md)

```bash
# Clone repository
git clone <repository-url>
cd trader_bitcoin
git checkout 001-production-refactor

# Backend setup
cd backend
uv sync

# Frontend setup
cd ../frontend
pnpm install

# Configure environment
cp .env.example .env
# Edit .env with your API credentials

# Run database migrations
cd backend
alembic upgrade head

# Start development servers
cd backend
uv run uvicorn main:app --reload --port 5611  # Terminal 1

cd frontend
pnpm run dev  # Terminal 2
```

**Access:**
- Frontend: http://localhost:5621
- Backend API: http://localhost:5611/api
- API Docs: http://localhost:5611/docs

### Production Deployment

```bash
# Build and start with Docker
docker-compose up -d --build

# View logs
docker-compose logs -f app

# Check health
curl http://localhost:5611/api/health
```

**For comprehensive deployment guide**, see [📘 Quickstart Guide - Production Deployment](specs/001-production-refactor/quickstart.md#production-deployment)

## Documentation

### Core Documentation
- **[Backend README](backend/README.md)** - Detailed backend architecture, development guide, testing
- **[Architecture Guide](backend/ARCHITECTURE.md)** - System design, patterns, and technical decisions
- **[Quickstart Guide](specs/001-production-refactor/quickstart.md)** - Step-by-step setup and deployment

### API Documentation
- **Swagger UI**: http://localhost:5611/docs (when running)
- **OpenAPI Specs**: `specs/001-production-refactor/contracts/*.yaml`

### Technical Specs
- **[Data Model](specs/001-production-refactor/data-model.md)** - Database schema and relationships
- **[Feature Spec](specs/001-production-refactor/spec.md)** - Detailed feature requirements
- **[Tasks](specs/001-production-refactor/tasks.md)** - Implementation tracking

## Project Structure

```
trader_bitcoin/
├── backend/                # FastAPI backend application
│   ├── api/                # Route handlers (async)
│   │   ├── accounts_async.py
│   │   ├── orders_async.py
│   │   ├── market_data_async.py
│   │   ├── ai_routes.py
│   │   ├── health_routes.py
│   │   ├── sync_routes.py
│   │   └── ws_async.py     # WebSocket real-time updates
│   ├── database/           # Database models and connection
│   │   ├── connection.py   # AsyncEngine with pool config
│   │   └── models.py       # SQLAlchemy models
│   ├── repositories/       # Data access layer (async)
│   │   ├── account_repo.py
│   │   ├── position_repo.py
│   │   ├── order_repo.py
│   │   └── trade_repo.py
│   ├── services/           # Business logic
│   │   ├── market_data/    # Market data services
│   │   ├── trading/        # Trading and sync services
│   │   ├── infrastructure/ # Scheduler, metrics, tracking
│   │   └── ai_decision_service.py
│   ├── scripts/            # Utility scripts
│   │   ├── debug/
│   │   ├── maintenance/    # Backup, cost analysis
│   │   └── deployment/     # Deployment scripts
│   ├── tests/              # Test suite
│   │   ├── unit/           # Unit tests
│   │   └── integration/    # Integration tests
│   ├── main.py             # FastAPI application entry
│   ├── README.md           # Backend documentation
│   ├── ARCHITECTURE.md     # Architecture guide
│   └── pyproject.toml      # Dependencies and config
├── frontend/               # React frontend application
│   ├── src/
│   └── package.json
├── specs/                  # Feature specifications
│   └── 001-production-refactor/
│       ├── spec.md         # Feature requirements
│       ├── tasks.md        # Implementation tasks
│       ├── quickstart.md   # Setup guide
│       └── data-model.md   # Database schema
├── docker-compose.yml      # Docker orchestration
├── .env.example            # Environment template
└── README.md               # This file
```

## Key Concepts

### Data Source Strategy
**Hyperliquid as Single Source of Truth** - Hyperliquid DEX is the authoritative source for all trading data. The local PostgreSQL database acts as a synchronized cache for display and analysis.

**Sync Flow:**
```
AI Analysis → Decision → Hyperliquid Order → Immediate Sync → DB Update
                              ↓
                       Periodic Sync (60s)
```

### AI-Driven Trading
1. AI analyzes market data, news, and current portfolio
2. AI makes trading decision (logged to `ai_decision_logs`)
3. Order placed on Hyperliquid via `place_market_order()`
4. **Immediate sync** after order execution
5. **Periodic sync** every 60 seconds for consistency
6. Database reflects current Hyperliquid state

### Self-Learning System (Counterfactual Analysis)
The system learns from every decision to improve trading strategy over time:

1. **Decision Capture** - Every AI decision (LONG/SHORT/HOLD) saved with complete context
2. **Counterfactual Calculation** - After 24h, calculates P&L for all 3 possible actions
3. **Pattern Analysis** - DeepSeek analyzes 50+ decisions to identify systematic errors
4. **Weight Optimization** - Suggests optimal indicator weights based on actual performance

**Example Insights**:
- "Ignored Prophet 12 times when RSI >70 → lost $145"
- "HOLD when Sentiment >80 + Whale sell → avoided -$230"

**Health Check**: Use `./check_learning_system.sh` to verify the system is working.

See [CLAUDE.md](CLAUDE.md#-counterfactual-learning-system) for complete documentation.

### Async Architecture
All operations use async/await for non-blocking I/O:
- **Database**: SQLAlchemy 2.0 async ORM with AsyncSession
- **HTTP Requests**: httpx async client
- **WebSocket**: FastAPI WebSocket for real-time updates
- **Connection Pool**: 10 base + 5 overflow connections

See [Architecture Guide](backend/ARCHITECTURE.md) for detailed patterns.

## Development

### Running Tests

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

**Coverage Target**: 70%+ for critical services

### Code Quality

```bash
# Linter
ruff check backend/

# Formatter
ruff format backend/

# Type checker
mypy --strict backend/services/

# Run all quality checks
ruff check backend/ && ruff format backend/ && mypy --strict backend/services/ && pytest --cov
```

### Development Guidelines

1. **Type Hints**: All code uses Python 3.11+ type hints
2. **Docstrings**: Google-style docstrings for all public functions
3. **Async/Await**: Use async patterns throughout
4. **Testing**: Add tests for new features
5. **Code Style**: Follow ruff formatting rules

See [Backend README](backend/README.md#development-guidelines) for detailed guidelines.

## Configuration

### Environment Variables

**Required Variables:**
```bash
# Hyperliquid API
HYPERLIQUID_PRIVATE_KEY=0x...     # Your private key
HYPERLIQUID_WALLET_ADDRESS=0x...  # Your wallet address
MAX_CAPITAL_USD=53.0               # Maximum trading capital

# DeepSeek AI
DEEPSEEK_API_KEY=sk-...           # DeepSeek API key
DEEPSEEK_BASE_URL=https://api.deepseek.com

# Database (choose one)
DATABASE_URL=sqlite+aiosqlite:///./data.db                    # Development
DATABASE_URL=postgresql+asyncpg://user:pass@localhost/db      # Production
```

**Optional Settings:**
```bash
# Application
DEBUG=false                        # Debug logging
SQL_DEBUG=false                    # SQL query logging
DB_POOL_SIZE=10                    # Connection pool size
DB_MAX_OVERFLOW=5                  # Max overflow connections

# Scheduler
SYNC_INTERVAL_SECONDS=60           # Hyperliquid sync interval
AI_DECISION_INTERVAL=180           # AI decision interval (3 minutes)
```

**For complete configuration reference**, see [Quickstart Guide - Configuration](specs/001-production-refactor/quickstart.md#configuration-reference)

## Monitoring

### Health Checks
- **GET /api/health** - Basic health check (always returns 200)
- **GET /api/ready** - Readiness check (database connection, sync status)

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

**For monitoring setup**, see [Architecture Guide - Monitoring](backend/ARCHITECTURE.md#monitoring)

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

### Automated Tasks
- **Daily**: Database backups at 2 AM
- **Hourly**: AI usage tracking reset
- **Every 60s**: Hyperliquid synchronization
- **Every 3 min**: AI trading decisions

## Troubleshooting

### Common Issues

**Database connection pool exhausted**
- Symptom: 503 errors with "Pool exhausted" message
- Solution: Increase `DB_POOL_SIZE` or `DB_MAX_OVERFLOW`

**Sync failures**
- Check Hyperliquid API status
- Verify API key and wallet address
- Review logs: `docker-compose logs -f app | grep sync`

**High AI costs**
- Review cache hit rates: `GET /api/ai/usage`
- Check if caches are working (news: 1h TTL, decisions: 10m)
- Run cost analysis: `python scripts/maintenance/analyze_ai_costs.py`

**For detailed troubleshooting**, see [Quickstart Guide - Troubleshooting](specs/001-production-refactor/quickstart.md#troubleshooting)

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Follow existing code patterns and style guidelines
4. Add type hints to all new code
5. Write docstrings for public functions
6. Add tests for new features
7. Run quality checks:
   ```bash
   ruff check backend/
   ruff format backend/
   mypy --strict backend/services/
   pytest --cov
   ```
8. Commit your changes (`git commit -m 'Add amazing feature'`)
9. Push to the branch (`git push origin feature/amazing-feature`)
10. Open a Pull Request

## Security

- **API Keys**: Encrypted storage, never logged
- **Database**: SSL connections, parameterized queries
- **Rate Limiting**: Per-account + global limits
- **Input Validation**: Pydantic models, type checking

**Important**: Never commit `.env` files or API credentials to version control!

## License

See LICENSE file in repository root.

## Support and Resources

- **Backend Documentation**: [backend/README.md](backend/README.md)
- **Architecture Guide**: [backend/ARCHITECTURE.md](backend/ARCHITECTURE.md)
- **Setup Guide**: [specs/001-production-refactor/quickstart.md](specs/001-production-refactor/quickstart.md)
- **API Documentation**: http://localhost:5611/docs (when running)
- **Hyperliquid API**: https://hyperliquid.gitbook.io/
- **DeepSeek API**: https://www.deepseek.com/

---

**Status**: Production-ready (Feature 001-production-refactor - 68% complete)

For questions or issues, please check the troubleshooting sections in the documentation above.
