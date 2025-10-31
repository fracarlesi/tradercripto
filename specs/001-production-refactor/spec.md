# Feature Specification: Production-Ready Bitcoin Trading System Refactoring

**Feature Branch**: `001-production-refactor`
**Created**: 2025-10-31
**Status**: Draft
**Input**: User description: "Refactoring Completo: Bitcoin Trading System Production-Ready - Sistema di trading automatico Bitcoin basato su AI (DeepSeek) che opera su Hyperliquid DEX"

## Clarifications

### Session 2025-10-31

- Q: When the AI trading decision service fails or times out, what specific rule-based fallback strategy should the system apply? → A: Hold - Maintain current positions without executing any new trades until AI service recovers
- Q: What are the Hyperliquid API rate limits that the system must respect? → A: 1200 weighted requests per minute (20/second average) - Most sync endpoints weight=1
- Q: When a sync operation fails due to network timeout or API error, what logging level and operational behavior should the system apply? → A: Log at WARNING level with detailed error context, increment failure counter, continue serving cached data with staleness indicator
- Q: When the PostgreSQL connection pool exhausts all connections, how long should queued requests wait before timing out? → A: 30 seconds timeout with 503 response and Retry-After: 10 header
- Q: What is the maximum acceptable monthly operational cost budget for the entire system (hosting + AI API + services)? → A: $50-100 USD per month
- Q: How many users will use this system? → A: Single user (personal use only)

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Reliable Data Synchronization (Priority: P1)

As a **system operator**, I need the trading system to maintain accurate, real-time synchronization with Hyperliquid (the authoritative data source) so that frontend displays correct balances, positions, and order status at all times.

**Why this priority**: Data inconsistencies directly impact trading decisions and user trust. This is the foundation upon which all other improvements depend. Without reliable data, the system cannot operate safely in production.

**Independent Test**: Can be fully tested by monitoring sync operations for 24 hours in a test environment, comparing database state with Hyperliquid API responses, and verifying zero discrepancies occur during normal operations and edge cases (network failures, high load).

**Acceptance Scenarios**:

1. **Given** Hyperliquid shows account balance of $100 and 0.5 BTC position, **When** sync runs successfully, **Then** local database reflects exactly $100 balance and 0.5 BTC position within 30 seconds
2. **Given** network connection temporarily fails during sync, **When** connection restores, **Then** system automatically retries and achieves full synchronization within 2 minutes
3. **Given** multiple concurrent sync operations are triggered, **When** all operations complete, **Then** database reflects the most recent Hyperliquid state with no duplicate or conflicting records
4. **Given** Hyperliquid shows a filled order that doesn't exist locally, **When** sync detects the discrepancy, **Then** system creates the missing order record with correct fills, trades, and position updates
5. **Given** frontend displays stale data older than 60 seconds, **When** user refreshes or new sync completes, **Then** frontend updates to show current Hyperliquid state with visual indicator showing data freshness

---

### User Story 2 - Scalable Async Backend Architecture (Priority: P1)

As a **system operator**, I need the backend to handle concurrent requests and database operations efficiently using async patterns so that the system can operate 24/7 without blocking operations or performance degradation.

**Why this priority**: Current synchronous architecture creates bottlenecks that prevent reliable H24 operations. This refactoring enables the system to handle concurrent internal operations (sync, AI decisions, frontend requests) without blocking, ensuring responsive user experience.

**Independent Test**: Can be fully tested by running load tests simulating typical concurrent load (10+ simultaneous requests) while sync and AI operations run simultaneously, measuring response times (p95 < 200ms) and verifying zero timeout errors or database lock contentions.

**Acceptance Scenarios**:

1. **Given** multiple concurrent requests (frontend polling + sync + AI operations) occur, **When** all requests are processed, **Then** each receives response within 200ms and database handles concurrent reads without locking
2. **Given** sync operation is updating positions, **When** API endpoint requests position data, **Then** endpoint returns data without waiting for sync to complete (reads use appropriate isolation level)
3. **Given** AI decision service calls DeepSeek API (network I/O), **When** other operations run concurrently, **Then** those operations are not blocked and complete independently
4. **Given** database connection pool is configured, **When** multiple concurrent requests arrive (typical load: 5-10 concurrent), **Then** system processes all efficiently without connection exhaustion errors

---

### User Story 3 - Production-Grade Database (Priority: P2)

As a **system operator**, I need to migrate from SQLite to PostgreSQL for production deployments while maintaining SQLite for local development so that the system can handle concurrent access, provide better reliability, and support production-scale operations.

**Why this priority**: SQLite file-based locking prevents concurrent writes needed for H24 operations. PostgreSQL provides ACID guarantees, connection pooling, and scalability required for production. This enables P1 stories to function correctly under load.

**Independent Test**: Can be fully tested by running identical test suite against both SQLite (dev) and PostgreSQL (prod) configurations, verifying all operations work correctly, migration scripts preserve data integrity, and performance meets targets for concurrent operations.

**Acceptance Scenarios**:

1. **Given** existing SQLite database with historical data, **When** migration script runs, **Then** all data transfers to PostgreSQL with zero data loss and correct schema including indexes and constraints
2. **Given** developer sets up local environment, **When** environment variables point to SQLite, **Then** system operates identically to PostgreSQL mode for development/testing purposes
3. **Given** production environment starts with empty database, **When** Alembic migrations run, **Then** schema initializes correctly and system begins syncing data from Hyperliquid
4. **Given** concurrent operations occur (sync + AI + frontend requests), **When** database processes all transactions, **Then** PostgreSQL handles load without locking issues that would affect SQLite

---

### User Story 4 - Docker Production Deployment (Priority: P2)

As a **DevOps engineer**, I need production-ready Docker configuration with multi-stage builds, health checks, and orchestration so that I can deploy the system reliably to any server and maintain 99.5% uptime.

**Why this priority**: Current basic Docker setup lacks production essentials (health monitoring, proper builds, secrets management). This enables actual deployment to servers for H24 operations.

**Independent Test**: Can be fully tested by deploying full stack (PostgreSQL, Redis, app) using docker-compose on clean VM, running for 7 days, and verifying system handles restarts, updates, and failures gracefully with automatic recovery.

**Acceptance Scenarios**:

1. **Given** clean server with Docker installed, **When** `docker-compose up` runs, **Then** entire stack (database, cache, application) starts in correct dependency order with all health checks passing
2. **Given** application container crashes unexpectedly, **When** Docker health check detects failure, **Then** container automatically restarts and resumes operations within 30 seconds
3. **Given** new code version needs deployment, **When** `docker-compose build` runs, **Then** multi-stage build produces optimized final runtime stage image under 500MB (including Python runtime, application code, and all production dependencies; excluding intermediate build stages and development tools)
4. **Given** environment variables contain secrets, **When** configuration is reviewed, **Then** no secrets are committed to version control and all sensitive data uses environment variable substitution
5. **Given** system runs for 7 days continuously, **When** monitoring logs are reviewed, **Then** uptime exceeds 99.5% with automatic recovery from transient failures

---

### User Story 5 - AI Cost Optimization (Priority: P2)

As a **system operator**, I need to analyze and optimize DeepSeek API costs while maintaining trading quality so that monthly operational expenses remain within budget and the system can scale economically.

**Why this priority**: Current AI calls every 3 minutes incur ongoing costs. Optimizing this reduces operational expenses significantly while maintaining or improving trading performance.

**Independent Test**: Can be fully tested by tracking AI API calls over 30 days, calculating total cost, comparing trading performance (win rate, return) before and after optimizations, and verifying cost reduction of at least 30% with no degradation in trading quality.

**Acceptance Scenarios**:

1. **Given** news feed fetches data every AI call, **When** caching layer stores news for 1 hour, **Then** API calls reduce proportionally (up to 20x fewer news fetches) while AI still receives recent news
2. **Given** AI decision service receives long prompts with full news text, **When** prompts are optimized to summarize key points, **Then** token usage decreases by at least 40% per call while decision quality remains comparable
3. **Given** identical market conditions occur twice within 10 minutes, **When** second AI call is triggered, **Then** system returns cached decision without calling DeepSeek API (deduplication)
4. **Given** monthly cost analysis runs, **When** comparing current month to baseline, **Then** detailed report shows cost breakdown (API calls × tokens × rate) and validates cost reduction target of 30%+

---

### User Story 6 - Code Quality & Maintainability (Priority: P3)

As a **developer**, I need clean, well-organized codebase with proper structure, type hints, tests, and documentation so that I can understand, modify, and extend the system confidently without introducing bugs.

**Why this priority**: While not immediately critical for operations, code quality directly impacts long-term maintainability, debugging speed, and ability to add features safely. This is an investment in future productivity.

**Independent Test**: Can be fully tested by running automated code quality tools (mypy, ruff, pytest), measuring code coverage (target: 70%+ for critical services), and having new developer successfully complete typical maintenance task using only documentation.

**Acceptance Scenarios**:

1. **Given** codebase with debug scripts scattered in root, **When** reorganization completes, **Then** all utility scripts move to `scripts/` directory organized by purpose (debug/, maintenance/, deployment/)
2. **Given** service layer has mixed responsibilities, **When** refactoring completes, **Then** services follow single responsibility principle with clear directory structure (trading/, market_data/, infrastructure/)
3. **Given** Python codebase runs type checker, **When** mypy runs in strict mode, **Then** zero type errors occur in core services (trading, sync, AI) with 90%+ code annotated
4. **Given** critical sync service needs testing, **When** unit tests run, **Then** test coverage exceeds 80% for sync logic with mocked Hyperliquid API
5. **Given** new developer reads documentation, **When** attempting to set up local environment, **Then** can complete setup in under 30 minutes using README instructions alone

---

### User Story 7 - Production Infrastructure & Monitoring (Priority: P3)

As a **system operator**, I need comprehensive monitoring, logging, backups, and deployment automation so that I can operate the system confidently in production with visibility into health and ability to recover from failures.

**Why this priority**: Enables production operations at scale. While system can run without this, professional production deployment requires monitoring and recovery capabilities.

**Independent Test**: Can be fully tested by deploying to production-like environment, simulating failures (database crash, service restart), and verifying automated alerts fire, logs capture root cause, and recovery procedures work within defined SLAs.

**Acceptance Scenarios**:

1. **Given** production system runs for 24 hours, **When** sync fails 3 times consecutively, **Then** alert fires via configured channel (email/Slack) within 2 minutes of third failure
2. **Given** Grafana dashboard displays system metrics, **When** operator reviews dashboard, **Then** can see current balance, recent AI decisions, order success rate, and sync status at a glance
3. **Given** database corruption occurs, **When** recovery procedure executes, **Then** system restores from most recent automated backup (max 24 hours old) and resumes operations within 15 minutes
4. **Given** code push to main branch occurs, **When** CI/CD pipeline runs, **Then** tests execute, Docker images build, and deployment to staging happens automatically if tests pass
5. **Given** structured logging collects events, **When** investigating sync failure, **Then** logs provide complete trace with request IDs, timestamps, error details, and context for debugging

---

### Edge Cases

- What happens when Hyperliquid API is temporarily unavailable (503, timeout, rate limit)?
  - System should: log at WARNING level with error context, retry with exponential backoff (FR-008), increment failure counter, serve stale cached data to frontend with staleness indicator showing time since last successful sync, resume normal operations when API recovers

- What happens when database migration fails midway?
  - System should: wrap migrations in database transactions where supported (PostgreSQL transactional DDL), detect failures, and provide manual rollback scripts if needed; preserve original data via automated pre-migration backup; log failure reason with full context; prevent application start until migration succeeds or is manually rolled back

- What happens when AI API call exceeds timeout or returns malformed response?
  - System should: fall back to hold strategy (maintain current positions, execute no new trades), log failure for analysis, retry on next cycle, continue operating in degraded mode until AI recovers

- What happens when sync detects irreconcilable conflict (e.g., order exists locally but not on Hyperliquid)?
  - System should: apply Hyperliquid-wins policy, archive conflicting local records, create audit log entry, notify operator of data inconsistency

- What happens when multiple environment instances (dev, staging, prod) sync from same Hyperliquid account?
  - System should: use read-only sync for non-prod environments, prevent trading operations on non-prod, clearly distinguish environments in logs

- What happens when PostgreSQL connection pool exhausts all connections?
  - System should: queue requests with 30-second timeout, return 503 Service Unavailable with Retry-After: 10 header, log pool exhaustion event at WARNING level, automatically scale pool if configured

- What happens during Docker container update with active trading operations?
  - System should: gracefully shut down (complete in-flight operations), persist state, restart with minimal downtime (<30s), resume operations

## Requirements *(mandatory)*

### Functional Requirements

#### Database & Data Synchronization

- **FR-001**: System MUST use PostgreSQL as primary database for production deployments with configurable connection pooling using SQLAlchemy parameters (pool_size: configurable 3-15, default 10; max_overflow: 5; pool_timeout: 30s - sized for single-user load with 5-10 typical concurrent operations)
- **FR-002**: System MUST support SQLite as alternative database for local development with functional equivalence for single-threaded operations (note: concurrent writes, JSONB features, and some advanced constraints are PostgreSQL-only)
- **FR-003**: System MUST implement Alembic migrations for schema changes with rollback capability
- **FR-004**: System MUST synchronize with Hyperliquid API every 30 seconds (configurable) fetching balances, positions, orders, and fills
- **FR-005**: System MUST treat Hyperliquid as single source of truth, always overwriting local database state during sync conflicts
- **FR-006**: System MUST implement idempotent sync operations preventing duplicate records even if sync runs multiple times
- **FR-007**: System MUST execute sync operations in atomic transactions (all-or-nothing) to maintain data consistency
- **FR-008**: System MUST retry failed sync operations with exponential backoff using 5 retry attempts with delays: 1s, 2s, 4s, 8s, 16s (backoff capped at 16s for any additional retries if logic changes)
- **FR-009**: System MUST clear and recreate position records on each sync to match Hyperliquid state exactly
- **FR-010**: System MUST fetch and store last 100 fills from Hyperliquid, creating corresponding order and trade records
- **FR-011**: System MUST maintain AI decision logs, OHLCV kline data, and price history locally (not synced from Hyperliquid)

#### Async Architecture

- **FR-012**: System MUST use async SQLAlchemy (AsyncEngine, AsyncSession) for all database operations
- **FR-013**: System MUST implement all FastAPI endpoints as async def functions for non-blocking I/O
- **FR-014**: System MUST wrap all synchronous Hyperliquid SDK calls with asyncio.to_thread() or run_in_executor() to prevent blocking the FastAPI async event loop (SDK confirmed synchronous-only per research phase)
- **FR-015**: System MUST implement dependency injection pattern using FastAPI Depends for service instances
- **FR-016**: System MUST configure database connection pool with async support (asyncpg driver for PostgreSQL with 3-15 connection range), 30-second queue timeout, and return 503 with Retry-After: 10 header on pool exhaustion
- **FR-017**: System MUST handle concurrent requests without database lock contention using appropriate isolation levels

#### Service Layer Organization

- **FR-018**: System MUST organize services into logical modules: trading/, market_data/, infrastructure/
- **FR-019**: Trading module MUST separate: order_service (placement/tracking), sync_service (Hyperliquid sync), ai_service (AI decisions)
- **FR-020**: Market data module MUST separate: price_service (price fetching/caching), kline_service (OHLCV data), news_service (news aggregation)
- **FR-021**: Infrastructure module MUST separate: scheduler_service (APScheduler wrapper), cache_service (optional Redis integration)

#### Error Handling & Resilience

- **FR-022**: System MUST implement custom exception hierarchy (TradingException, SyncException, AIException, etc.)
- **FR-023**: System MUST register global FastAPI exception handlers for consistent error responses
- **FR-024**: System MUST implement comprehensive rate limiting and resilience for Hyperliquid API calls including: (a) Circuit breaker pattern (open after 5 consecutive failures, half-open after 60s allowing 1 probe request, close on probe success or reopen on failure), (b) Proactive rate limit tracking (1200 weighted requests/minute limit, track request weights, implement backoff when approaching 80% of limit)
- **FR-025**: System MUST implement rate limiting for DeepSeek API calls preventing API quota exhaustion
- **FR-026**: System MUST serve cached data when sync fails, increment failure counter, and display staleness indicator to users showing time since last successful sync
- **FR-027**: System MUST log all exceptions with structured format including: timestamp, level, service, operation, error details, stack trace, and relevant context (request params, account ID, etc.)

#### Logging & Observability

- **FR-028**: System MUST implement structured logging in JSON format with fields: timestamp, level, service, request_id, message, context
- **FR-029**: System MUST use appropriate log levels: DEBUG for dev, INFO for prod normal operations, WARNING for degraded mode (sync failures, API timeouts), ERROR for critical failures requiring intervention
- **FR-030**: System MUST assign unique request IDs to all API requests and include in all related log entries
- **FR-031**: System MUST expose /health endpoint returning: status (ok/degraded/down), uptime, last_sync_time, sync_status
- **FR-032**: System MUST expose /ready endpoint checking: database connectivity, Hyperliquid API reachability, required environment variables
- **FR-033**: System MUST track and log performance metrics: sync duration, API response times, database query times

#### AI Cost Optimization

- **FR-034**: System MUST cache news feed data for configurable TTL (default: 1 hour) to reduce redundant fetches
- **FR-035**: System MUST optimize AI prompts by summarizing news instead of including full text (target: 40% token reduction measured against 30-day pre-optimization baseline average tokens per call)
- **FR-036**: System MUST deduplicate AI decisions for identical market states within configurable window (default: 10 minutes) using MD5 hash of (rounded_price, position_quantity, top_10_news_ids) as identity key
- **FR-037**: System MUST track AI API usage metrics: calls per day, tokens per call, estimated monthly cost calculated as (input_tokens × $0.14/1M) + (output_tokens × $0.14/1M) based on DeepSeek pricing as of 2025-10
- **FR-038**: System MUST implement fallback to hold strategy (maintain current positions without new trades) when AI API fails or times out
- **FR-039**: System SHOULD support evaluation of self-hosted DeepSeek option with cost/performance comparison reporting (DEFERRED: Analysis only in this phase, implementation pending cost analysis results - see Out of Scope section)

#### News Feed Enhancement

- **FR-040**: System MUST fetch news from CoinJournal RSS feed as primary source
- **FR-041**: System MUST cache fetched news articles with 1-hour TTL to prevent redundant API calls
- **FR-042**: System MUST filter news by relevance to actively traded symbols if configured

#### Docker & Deployment

- **FR-043**: System MUST provide multi-stage Dockerfile with 3 stages: Stage 1 frontend build (Node), Stage 2 backend deps (Python+uv), Stage 3 final runtime (slim) with final runtime stage image size target <500MB
- **FR-044**: System MUST run application as non-root user (UID 1000) in container for security
- **FR-045**: System MUST implement health check in Dockerfile probing /api/health endpoint every 30 seconds
- **FR-046**: System MUST provide docker-compose.yml including: postgres, redis (optional), app services
- **FR-047**: System MUST configure service dependencies ensuring postgres starts before app using health-based conditions
- **FR-048**: System MUST support environment-based configuration via .env file with .env.example template
- **FR-049**: System MUST validate required environment variables on startup using Pydantic Settings
- **FR-050**: System MUST configure container restart policy (unless-stopped) for automatic recovery
- **FR-051**: System MUST expose Prometheus metrics endpoint for monitoring integration
- **FR-052**: System MUST integrate with reverse proxy (Traefik) for SSL/TLS termination

#### Code Quality

- **FR-053**: System MUST organize utility scripts into: scripts/debug/, scripts/maintenance/, scripts/deployment/
- **FR-054**: System MUST remove or archive unused files: force_trade.py, close_xrp.py if not actively used
- **FR-055**: System MUST annotate all public functions and methods with type hints for mypy validation
- **FR-056**: System MUST include docstrings for all public functions describing purpose, parameters, return values
- **FR-057**: System MUST use consistent naming: snake_case for functions/variables, PascalCase for classes
- **FR-058**: System MUST eliminate code duplication extracting common patterns into shared utilities

#### Testing

- **FR-059**: System MUST provide unit tests for critical services: sync, trading, AI decision logic
- **FR-060**: System MUST provide integration tests for complete sync flow: fetch → process → persist → verify
- **FR-061**: System MUST provide end-to-end test for trading flow: AI decision → order placement → sync → verification
- **FR-062**: System MUST mock Hyperliquid API in tests using fixtures or test doubles
- **FR-063**: System MUST achieve minimum 70% code coverage for core services (trading, sync, AI)

#### Monitoring & Infrastructure

- **FR-064**: System MUST export logs in structured format suitable for aggregation (e.g., JSON to stdout)
- **FR-065**: System MUST provide Grafana dashboard templates showing: balance trends, AI decisions, order success rate, sync status
- **FR-066**: System MUST configure automated database backups daily with 7-day retention, stored locally in ./backups/ directory with gzip compression (encryption optional for production)
- **FR-067**: System MUST implement alerting for critical events: sync failure (3 consecutive), balance mismatch, API errors
- **FR-068**: System MUST provide deployment guide documenting: server requirements, setup steps, environment configuration
- **FR-069**: System MUST update ARCHITECTURE.md with current state: async patterns, sync strategy, service organization
- **FR-070**: System MUST provide manual rollback scripts for Alembic migrations that involve data transformations or schema changes requiring manual intervention if automatic downgrade fails

### Key Entities

- **User**: Represents system user (scaffolding for future multi-user support)
  - Attributes: id, username, email, password_hash, is_active, created_at
  - Current deployment: Single user only - authentication/authorization deferred to future phase
  - Purpose: Database schema foundation for potential multi-tenant expansion

- **Account**: Represents trading account with DeepSeek AI model configuration
  - Attributes: name, account_type (AI/Manual), model name, API credentials, balances (current_cash, frozen_cash, initial_capital), active status
  - Synced from Hyperliquid: current_cash (available balance), frozen_cash (margin used), initial_capital (total equity)
  - Local only: AI model config, account metadata

- **Position**: Represents current open trading position
  - Attributes: symbol, quantity, available_quantity, average_cost, creation/update timestamps
  - Synced from Hyperliquid: ENTIRE position list (cleared and recreated each sync)
  - Sync strategy: Delete all local positions for account → Recreate from Hyperliquid assetPositions

- **Order**: Represents trading order (historical and active)
  - Attributes: order_no (unique ID), symbol, side (buy/sell), order_type, price, quantity, filled_quantity, status, timestamps
  - Synced from Hyperliquid: Historical fills converted to FILLED orders
  - Status values: PENDING, FILLED, CANCELLED, REJECTED
  - Note: Hyperliquid is source of truth - local orders are historical records

- **Trade**: Represents executed trade (fill)
  - Attributes: symbol, side, price, quantity, commission, trade_time
  - Linked to: Order (many trades per order possible)
  - Synced from Hyperliquid: Last 100 fills fetched each sync
  - Deduplication: Use timestamp+symbol+side+quantity as unique identifier

- **AIDecisionLog**: Tracks AI trading decisions and reasoning (LOCAL ONLY)
  - Attributes: decision_time, reason (AI explanation), operation (buy/sell/hold), symbol, prev_portion, target_portion, total_balance, executed flag, linked order_id
  - NOT synced from Hyperliquid - purely local audit trail
  - Purpose: Track what AI decided, why, and whether order was successfully executed

- **CryptoKline**: OHLCV candlestick data cache (LOCAL ONLY)
  - Attributes: symbol, period (1m/5m/15m/1h/1d), timestamp, open/high/low/close prices, volume, amount
  - Purpose: Historical price data for chart display and technical analysis
  - Source: Market data APIs (CCXT or similar), not Hyperliquid

- **CryptoPrice**: Daily price snapshot cache (LOCAL ONLY)
  - Attributes: symbol, price, price_date
  - Purpose: Simplified historical pricing for portfolio valuation

## Success Criteria *(mandatory)*

### Measurable Outcomes

#### Reliability & Data Accuracy
- **SC-001**: System maintains 99.5% uptime over 30-day period when deployed to production server
- **SC-002**: Sync success rate exceeds 99% with automatic recovery from transient failures within 5 minutes
- **SC-003**: Frontend data staleness does not exceed 60 seconds in 99% of cases (p99) during normal operations, measured server-side as `max(current_time - last_successful_sync_time)` on data fetch, accounting for sync interval (30s) + processing (5s) + potential single retry (up to 25s)
- **SC-004**: Zero data inconsistencies detected between local database and Hyperliquid over 7-day continuous operation, validated using automated data integrity script comparing DB snapshots with Hyperliquid API responses (daily automated checks)
- **SC-005**: System recovers automatically from database connection loss within 2 minutes without manual intervention

#### Performance
- **SC-006**: API endpoints respond within 200ms for 95% of requests (p95 latency) under normal load (defined as 5-10 concurrent requests representing single-user typical usage pattern)
- **SC-007**: Sync operation completes within 5 seconds for typical account (10 positions, 100 recent fills)
- **SC-008**: System handles 10+ concurrent API requests without degradation or timeout errors (single-user load pattern)
- **SC-009**: Frontend initial page load completes within 2 seconds on standard broadband connection
- **SC-010**: Database connection pool maintains < 70% utilization under peak load

#### Cost Optimization
- **SC-011**: AI API costs reduce by minimum 30% compared to baseline while maintaining trading performance
- **SC-012**: News feed API calls reduce by 80% through effective caching (measured over 24-hour period)
- **SC-013**: AI token usage per decision reduces by 40% through prompt optimization
- **SC-014**: Monthly operational costs remain below $100 USD budget threshold (target: $50-100 range including hosting, AI API, and services)

#### Code Quality & Maintainability
- **SC-015**: Mypy type checking passes with zero errors on core services in strict mode
- **SC-016**: Test coverage reaches 70% or higher for critical services (sync, trading, AI)
- **SC-017**: New developer successfully sets up local environment in under 30 minutes using documentation alone
- **SC-018**: Zero high-severity security vulnerabilities detected by automated security scanning tools

#### Deployment & Operations
- **SC-019**: Docker deployment completes successfully on clean server within 15 minutes from clone to running
- **SC-020**: System restarts automatically after crash with full service restoration within 30 seconds
- **SC-021**: Database migration from SQLite to PostgreSQL preserves 100% of data with zero corruption
- **SC-022**: Monitoring dashboards provide visibility into system health with 5-minute refresh granularity

#### Business Impact
- **SC-023**: Trading operations continue uninterrupted for 7+ days without manual intervention
- **SC-024**: Operator can identify and diagnose system issues within 10 minutes using logs and monitoring
- **SC-025**: System handles account balance changes (deposits, withdrawals, trading activity) with immediate reflection in UI after next sync cycle

## Constraints *(include if applicable)*

### Technical Constraints
- Must maintain compatibility with Hyperliquid Python SDK (current version in use)
- Must respect Hyperliquid API rate limits: 1200 weighted requests/minute per IP (most sync endpoints have weight=1, some info endpoints weight=2-40)
- Must work with existing DeepSeek API contract (OpenAI-compatible chat completions endpoint)
- PostgreSQL version 12+ required for deployment (for certain features like JSONB improvements)
- Python 3.11+ required for typing features and performance improvements
- Node.js 20+ required for frontend build process

### Operational Constraints
- **Zero downtime requirement**: Migration must be performed incrementally with ability to rollback
- **No data loss**: All historical trades, positions, and AI decisions must be preserved during migrations
- **Backward compatibility**: Existing API endpoints should maintain contracts where possible to avoid breaking frontend
- **Budget limit**: Total monthly operational costs (hosting + AI API + services) must stay within $100 USD budget (target: $50-100 range)

### Deployment Constraints
- **Single Scheduler Instance**: System MUST run only ONE APScheduler instance to prevent duplicate job execution (sync tasks, AI decisions). If future horizontal scaling is required, MUST migrate to distributed event broker (Redis/PostgreSQL) with job locking mechanism before scaling to multiple replicas.
- **Current design**: Docker Compose single replica configuration, APScheduler with MemoryDataStore sufficient for single-instance deployment
- **Monitoring requirement**: Alert if multiple scheduler heartbeats detected (indicates misconfiguration)

### Security Constraints
- Private keys and API secrets must NEVER be committed to version control
- Database credentials must be passed via environment variables only
- Production environment must use separate credentials from development/staging
- System must run with least privilege (non-root user in containers)

### Business Constraints
- **Personal use only**: System designed for single-user personal trading (no multi-user, multi-tenant, or commercial use)
- Focus exclusively on Hyperliquid exchange (no multi-exchange support in this phase)
- Use DeepSeek as sole AI provider (no multi-model support in this phase)
- Real trading only - no paper trading mode (already removed from codebase)

## Assumptions *(include if applicable)*

### Technical Assumptions
- Hyperliquid Python SDK supports async operations OR can be wrapped with asyncio without significant performance penalty
- Hyperliquid API provides sufficient data granularity for full sync (balances, positions, fills)
- DeepSeek API has adequate rate limits for current trading frequency (decision every 3 minutes)
- PostgreSQL connection pooling with 15 connections max is sufficient for single-user load (typically 5-10 concurrent operations max, with 1.5-2x safety margin)
- Existing frontend React application can consume updated backend API without major changes

### Business Assumptions
- Trading operations are automated and do not require real-time human intervention during normal conditions
- System is for personal use only (single user, single account) - no multi-user features required
- News feed from CoinJournal provides adequate market context for AI decisions
- Current capital limits ($52 USD max) are appropriate for this deployment phase
- Trading frequency (AI decision every 3 minutes) is optimal balance of opportunity vs. cost
- Expected load is very low (single user accessing frontend occasionally) - infrastructure can be sized conservatively

### Operational Assumptions
- Deployment target is single VPS or cloud VM (not distributed/multi-region initially)
- Server has reliable internet connectivity with minimal downtime
- Operator has basic Docker and Linux system administration skills
- Monitoring and alerting infrastructure (Grafana, Prometheus) can be self-hosted or uses managed service
- Hyperliquid maintains API stability and backward compatibility

### Data Assumptions
- Hyperliquid API remains accessible as authoritative source for all trading data
- Historical AI decisions older than 90 days have minimal operational value (retention policy TBD)
- OHLCV kline data cached locally supplements but does not replace Hyperliquid as source of truth
- Database size remains manageable (< 10GB) for typical SQLite → PostgreSQL migration

## Out of Scope *(include if applicable)*

### Features Explicitly Excluded (This Phase)
- **Multi-user support**: No user registration, authentication, authorization, or multi-tenancy (personal single-user system only)
- Multi-exchange support (only Hyperliquid in scope)
- Multiple AI model providers (only DeepSeek in scope)
- Advanced machine learning model training or backtesting framework
- Mobile application development
- Social features (public rankings, user interactions, sharing)
- Advanced trading strategies beyond current AI-driven approach
- Historical data analysis or reporting beyond basic dashboard metrics

### Features Deferred (Future Phases)
- Self-hosted DeepSeek deployment (currently evaluation only - decision pending cost analysis)
- Multiple news source aggregation (CoinDesk, CoinTelegraph, Twitter/X - currently only CoinJournal)
- Advanced sentiment analysis (currently basic filtering only)
- CI/CD pipeline automation (optional nice-to-have, not required for initial deployment)
- Advanced monitoring (Sentry error tracking, New Relic APM - currently basic Prometheus/Grafana only)
- Geographic distribution or multi-region deployment
- Horizontal scaling beyond single server instance

### Non-Functional Exclusions
- Performance optimization beyond stated success criteria (p95 < 200ms is sufficient)
- Load testing beyond 100 concurrent requests (current expected load much lower)
- Security penetration testing or compliance certifications (basic security practices only)
- Formal disaster recovery procedures beyond daily backups
- SLA guarantees beyond 99.5% uptime goal

## Dependencies & Integrations *(include if applicable)*

### External Service Dependencies
- **Hyperliquid DEX API**: Authoritative source for all trading data (balances, positions, orders, fills)
  - Criticality: High - system cannot operate without Hyperliquid connectivity
  - Fallback: Serve stale cached data with staleness indicator if API temporarily unavailable

- **DeepSeek API**: AI model for trading decisions
  - Criticality: Medium - system can fall back to rule-based decisions if AI unavailable
  - Fallback: Hold strategy - maintain current positions without executing new trades

- **CoinJournal RSS Feed**: News aggregation for AI context
  - Criticality: Low - AI can make decisions without news if feed unavailable
  - Fallback: Operate with market data only

### Infrastructure Dependencies
- **PostgreSQL Database**: Primary data store for production
  - Version: 12+ (recommended: 14+ for better performance)
  - Alternatives: SQLite for local development only

- **Redis Cache** (optional): Session caching and rate limiting
  - Version: 6+ or 7+
  - Optional but recommended for production to reduce database load

- **Docker & Docker Compose**: Container orchestration
  - Version: Docker 20+, Compose V2
  - Required for production deployment strategy

### Library/SDK Dependencies
- **hyperliquid-python-sdk**: Hyperliquid API client (version >= 0.20.0)
- **FastAPI**: Web framework for backend API (version per pyproject.toml)
- **SQLAlchemy**: Database ORM (version 2.0+ for async support)
- **asyncpg**: Async PostgreSQL driver (for production)
- **aiosqlite**: Async SQLite driver (for development)
- **APScheduler**: Task scheduling for periodic sync
- **requests**: HTTP client for DeepSeek API and news fetching

### Integration Points
- **Frontend ↔ Backend API**: RESTful JSON API + WebSocket for real-time updates
  - Contract: Existing API endpoints maintained for backward compatibility
  - New endpoints: Health/readiness checks, sync status

- **Backend ↔ Hyperliquid**: RESTful API via official Python SDK
  - Authentication: Private key + wallet address from environment
  - Rate limits: 1200 weighted requests/minute per IP (standard sync endpoints weight=1, with 30s interval using ~10 requests/min leaving ample margin)

- **Backend ↔ DeepSeek**: OpenAI-compatible chat completions API
  - Authentication: Bearer token from environment
  - Rate limits: Implement backoff and fallback on 429 errors

- **Backend ↔ Database**: Async SQLAlchemy ORM
  - Connection: Pooled connections via AsyncEngine
  - Transactions: Atomic sync operations with rollback capability

## Open Questions / Clarifications Needed *(optional)*

1. **Hyperliquid SDK Async Support**: Does the hyperliquid-python-sdk natively support async operations, or will we need to wrap synchronous calls with asyncio.to_thread()? This affects performance of P1 and P2 stories.

2. **Deployment Timeline**: Is there a target date for production deployment that might affect prioritization or scope of included features?

## Notes *(optional)*

### Risks & Mitigations

**Risk**: Hyperliquid Python SDK is synchronous-only, blocking FastAPI async event loop
- **Likelihood**: High (confirmed by SDK documentation showing no async methods in examples)
- **Impact**: Medium (performance degradation under concurrent load, violates FR-014)
- **Mitigation**:
  1. Wrap all SDK calls with `asyncio.to_thread()` or `run_in_executor()` to prevent event loop blocking
  2. Create thin async wrapper layer: `AsyncHyperliquidClient` class providing async interface
  3. Load test wrapped implementation to verify <200ms p95 latency maintained (SC-006)
  4. If performance insufficient after optimization, evaluate alternative: direct REST API calls with `httpx.AsyncClient`
  5. Document wrapper pattern in ARCHITECTURE.md for future maintainers

**Risk**: Hyperliquid API changes or deprecates endpoints during migration
- **Mitigation**: Use official Python SDK (abstracts API changes), implement integration tests, monitor SDK release notes

**Risk**: PostgreSQL migration encounters data type mismatches from SQLite
- **Mitigation**: Extensive testing of migration scripts against copy of production data, have rollback plan

**Risk**: Async refactoring introduces subtle race conditions or deadlocks
- **Mitigation**: Comprehensive integration tests, load testing, gradual rollout starting with read-only operations

**Risk**: Self-hosted DeepSeek analysis shows it's not cost-effective (requires expensive GPU)
- **Mitigation**: Continue with API usage, implement aggressive prompt optimization and caching (FR-034 through FR-038)

**Risk**: Multi-stage Docker build produces large images exceeding deployment constraints
- **Mitigation**: Optimize layer caching, use slim base images, measure and enforce image size limits

### Implementation Recommendations

- **Phase 1 (Foundation)**: P1 stories - Database migration + Async architecture - Enables reliable H24 operations
- **Phase 2 (Deployment)**: P2 stories - Docker + AI optimization - Enables actual production deployment
- **Phase 3 (Polish)**: P3 stories - Code quality + Infrastructure - Improves maintainability and operations

- Consider feature flags for new sync strategy to allow gradual rollout and easy rollback
- Implement comprehensive logging from day one - troubleshooting async issues is much harder without good logs
- Use database migrations as opportunity to add missing indexes and constraints identified in current schema
- Docker multi-stage build should use layer caching effectively - dependencies rarely change, leverage this

### Reference Materials

- Hyperliquid Python SDK documentation: [verify URL in project]
- FastAPI async best practices: https://fastapi.tiangolo.com/async/
- SQLAlchemy async ORM guide: https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html
- Docker multi-stage builds: https://docs.docker.com/build/building/multi-stage/
- Current ARCHITECTURE.md: ./backend/ARCHITECTURE.md (needs update post-refactoring)
