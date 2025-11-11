# Bitcoin Trading System - Documentation

Complete documentation for the Bitcoin Trading System - an AI-powered cryptocurrency trading platform on Hyperliquid DEX.

## 📚 Documentation Index

### 🏗️ Architecture

System design, data flows, and technical architecture:

- **[OVERVIEW.md](architecture/OVERVIEW.md)** - Complete system architecture, async patterns, database design, sync algorithm
- **[SYSTEM_ORCHESTRATION.md](architecture/SYSTEM_ORCHESTRATION.md)** - Detailed operational flow: startup, scheduled jobs, AI trading cycles
- **[DATA_FLOW.md](architecture/DATA_FLOW.md)** - Data flow from Hyperliquid API → Database → Frontend
- **[DATABASE.md](architecture/DATABASE.md)** - SQLite vs PostgreSQL differences, migration guide

### 🔧 Operations

Deployment, monitoring, and operational procedures:

- **[SCHEDULED_JOBS.md](operations/SCHEDULED_JOBS.md)** - All background jobs: intervals, dependencies, API rate limiting
- **[MONITORING.md](operations/MONITORING.md)** - Health checks, logging, debugging workflows
- **[MIGRATIONS.md](operations/MIGRATIONS.md)** - Database migration and rollback procedures
- **[Deployment Guide](../GUIDA_DEPLOYMENT_HETZNER.md)** - Production deployment on Hetzner VPS

### 🧠 Self-Learning System

AI counterfactual analysis and self-improvement:

- **[DESIGN.md](learning/DESIGN.md)** - Self-learning system design and architecture
- **[INTEGRATION.md](learning/INTEGRATION.md)** - Integration with main trading system
- **[IMPLEMENTATION_OPTIONS.md](learning/IMPLEMENTATION_OPTIONS.md)** - Implementation alternatives

### 🌐 API Reference

REST API and WebSocket documentation:

- **[ENDPOINTS.md](api/ENDPOINTS.md)** - Complete REST API endpoint reference
- **[WEBSOCKET.md](api/WEBSOCKET.md)** - WebSocket protocol and real-time data flow

---

## 🚀 Quick Start

New to the project? Start here:

1. **[Project README](../README.md)** - Overview, installation, quick start
2. **[Architecture Overview](architecture/OVERVIEW.md)** - Understand system design
3. **[System Orchestration](architecture/SYSTEM_ORCHESTRATION.md)** - Learn how everything works together

## 📖 For Developers

Building and extending the system:

- **[CLAUDE.md](../CLAUDE.md)** - Development guidelines, coding rules, workflows
- **[Backend README](../backend/README.md)** - Backend-specific setup and structure

## 🐛 Troubleshooting

- **[Monitoring Guide](operations/MONITORING.md)** - Debug workflows, common issues
- Health checks: `/api/health` (system status), `/api/readiness` (startup checks)

---

## 📂 Documentation Structure

```
docs/
├── README.md                    # This file - documentation index
├── architecture/                # System design and architecture
│   ├── OVERVIEW.md
│   ├── SYSTEM_ORCHESTRATION.md
│   ├── DATA_FLOW.md
│   └── DATABASE.md
├── operations/                  # Deployment and operations
│   ├── SCHEDULED_JOBS.md
│   ├── MONITORING.md
│   └── MIGRATIONS.md
├── learning/                    # Self-learning system
│   ├── DESIGN.md
│   ├── INTEGRATION.md
│   └── IMPLEMENTATION_OPTIONS.md
└── api/                         # API reference
    ├── ENDPOINTS.md
    └── WEBSOCKET.md
```

---

## 🔄 Keeping Documentation Updated

Documentation is **living code** - update it with every feature change:

1. **Architecture changes** → Update `architecture/OVERVIEW.md` or `SYSTEM_ORCHESTRATION.md`
2. **New scheduled jobs** → Update `operations/SCHEDULED_JOBS.md`
3. **New API endpoints** → Update `api/ENDPOINTS.md`
4. **Deployment changes** → Update deployment guide

**Best Practice**: Document changes in the same commit as code changes.

---

## 📝 Contributing

When adding documentation:

- Use clear, concise language
- Include code examples where relevant
- Add diagrams for complex flows (use draw.io, PlantUML, or Mermaid)
- Follow existing structure and formatting
- Test all code examples before committing

---

**Last Updated**: 2025-11-10
