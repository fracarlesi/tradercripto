# Implementation Plan: Production-Ready Bitcoin Trading System Refactoring

**Branch**: `001-production-refactor` | **Date**: 2025-10-31 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/001-production-refactor/spec.md`

**Note**: This template is filled in by the `/speckit.plan` command. See `.specify/templates/commands/plan.md` for the execution workflow.

## Summary

This feature refactors the existing Bitcoin trading system into a production-ready application with async architecture, PostgreSQL database support, reliable Hyperliquid synchronization, Docker deployment, and AI cost optimization. The primary technical approach involves migrating from synchronous SQLite-based operations to async FastAPI + async SQLAlchemy + PostgreSQL while maintaining backward compatibility and implementing comprehensive error handling, monitoring, and deployment infrastructure.

## Technical Context

**Language/Version**: Python 3.11+ (currently 3.13 in Dockerfile)
**Primary Dependencies**: FastAPI, SQLAlchemy 2.0+ (async), hyperliquid-python-sdk >=0.20.0, APScheduler, uvicorn
**Storage**: PostgreSQL 12+ (production), SQLite (local dev) - requires dual database support with Alembic migrations
**Testing**: pytest with async support, httpx AsyncClient for API testing, mocking for Hyperliquid SDK
**Target Platform**: Linux server (Docker container on VPS/cloud VM), single-instance deployment with reverse proxy (Traefik)
**Project Type**: Web application (React frontend + FastAPI backend)
**Performance Goals**: p95 API latency <200ms, sync operations <5s, 10+ concurrent requests without degradation, 99.5% uptime
**Constraints**: $50-100/month budget, single-user personal use, 1200 weighted requests/min Hyperliquid rate limit, <500MB Docker image
**Scale/Scope**: Single user, ~40 Python files, 7 user stories (3 P1, 3 P2, 1 P3), 69 functional requirements, minimal frontend changes needed

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

**Note**: Project constitution file is template-only. Applying standard software engineering gates:

### Gate 1: Data Integrity ✅ PASS
- **Requirement**: Hyperliquid must remain single source of truth for all trading data
- **Status**: PASS - Spec explicitly requires Hyperliquid-wins policy (FR-005), sync operations are idempotent (FR-006), and atomic transactions (FR-007)
- **Justification**: This is non-negotiable for production trading system to prevent data inconsistencies that could lead to financial loss

### Gate 2: Backward Compatibility ✅ PASS
- **Requirement**: Existing API contracts maintained to avoid breaking frontend
- **Status**: PASS - Spec constrains API endpoints to maintain contracts (Operational Constraints section), frontend changes minimal
- **Justification**: Single-user system can tolerate incremental migration without full API versioning

### Gate 3: Security ✅ PASS
- **Requirement**: No secrets in version control, least privilege execution
- **Status**: PASS - Spec requires environment variable-only credentials (Security Constraints), non-root container user (FR-044)
- **Justification**: Standard security practices for production deployment

### Gate 4: Testing Strategy ⚠️ CONDITIONAL PASS
- **Requirement**: 70%+ code coverage for critical services before production deployment
- **Status**: CONDITIONAL - Deferred to Phase 3 (P3 priority), but acceptance criteria require testing (SC-016)
- **Justification**: Can deploy with integration testing only if comprehensive monitoring and rollback capability in place. Full unit test coverage is P3 enhancement, not blocker.
- **Mitigation**: Extensive integration testing in Phase 1, comprehensive logging (FR-027-033), health checks (FR-031-032), easy rollback via Docker

### Gate 5: Single Instance Architecture ✅ PASS
- **Requirement**: Acknowledge single APScheduler instance limitation before horizontal scaling
- **Status**: PASS - Spec explicitly documents constraint (Deployment Constraints), states current design sufficient for single-user load
- **Justification**: Appropriate for personal use case. Alert mechanism required if scaling needed in future (monitoring for multiple scheduler heartbeats)

**Constitution Check Result**: ✅ PASS with conditional testing (mitigated by monitoring + rollback)

## Project Structure

### Documentation (this feature)

```text
specs/001-production-refactor/
├── plan.md              # This file (/speckit.plan command output)
├── research.md          # Phase 0 output (async SDK, PostgreSQL patterns, Docker optimization)
├── data-model.md        # Phase 1 output (entity relationships, sync strategy)
├── quickstart.md        # Phase 1 output (local dev setup, deployment guide)
├── contracts/           # Phase 1 output (OpenAPI specs for health/sync endpoints)
│   ├── health-api.yaml
│   └── sync-api.yaml
└── tasks.md             # Phase 2 output (/speckit.tasks command - NOT created by /speckit.plan)
```

### Source Code (repository root)

**Selected Structure**: Web application (React frontend + FastAPI backend)

```text
trader_bitcoin/
├── backend/
│   ├── api/                      # FastAPI route handlers (TO REFACTOR: async endpoints)
│   │   ├── account_routes.py
│   │   ├── market_data_routes.py
│   │   ├── order_routes.py
│   │   └── ws.py                 # WebSocket for real-time updates
│   ├── config/                   # Application configuration
│   │   └── settings.py           # (TO ADD: Pydantic Settings for env validation)
│   ├── database/                 # Database layer (TO REFACTOR: async SQLAlchemy)
│   │   ├── connection.py         # (TO REFACTOR: AsyncEngine, async session)
│   │   └── models.py             # SQLAlchemy ORM models
│   ├── repositories/             # Data access layer (TO REFACTOR: async methods)
│   │   ├── account_repo.py
│   │   ├── order_repo.py
│   │   └── position_repo.py
│   ├── services/                 # Business logic (TO REFACTOR: async services)
│   │   ├── hyperliquid_sync_service.py    # Periodic sync orchestration
│   │   ├── hyperliquid_trading_service.py # Hyperliquid API wrapper
│   │   ├── ai_decision_service.py         # DeepSeek AI integration
│   │   ├── news_feed.py                   # (TO OPTIMIZE: caching layer)
│   │   ├── scheduler.py                   # APScheduler wrapper
│   │   └── startup.py                     # Service initialization
│   ├── schemas/                  # Pydantic request/response models
│   │   ├── account.py
│   │   └── order.py
│   ├── alembic/                  # (TO CREATE: Database migrations)
│   │   ├── versions/
│   │   └── env.py
│   ├── tests/                    # (TO CREATE: Test suite)
│   │   ├── unit/
│   │   ├── integration/
│   │   └── conftest.py
│   ├── scripts/                  # (TO ORGANIZE: Move debug scripts here)
│   │   ├── debug/
│   │   ├── maintenance/
│   │   └── deployment/
│   ├── main.py                   # FastAPI application entry (TO REFACTOR: async lifespan)
│   └── pyproject.toml            # Dependencies (TO UPDATE: add asyncpg, psycopg2, alembic)
├── frontend/
│   ├── src/
│   │   ├── components/           # React components
│   │   ├── pages/                # Page views
│   │   └── services/             # API client services
│   ├── package.json
│   └── vite.config.ts
├── docker-compose.yml            # (TO ENHANCE: add PostgreSQL, Redis, health checks)
├── Dockerfile                    # (TO OPTIMIZE: multi-stage build, non-root user)
├── .env.example                  # (TO CREATE: Template for environment variables)
└── README.md                     # (TO UPDATE: new setup instructions)
```

**Structure Decision**: Web application structure selected because project has separate frontend (React/Vite) and backend (FastAPI) codebases. Backend uses layered architecture: API routes → Services → Repositories → Database. This structure supports the refactoring goals by separating concerns and enabling incremental async migration per layer.

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

No violations requiring justification. All gates passed or conditionally passed with appropriate mitigations documented above.
