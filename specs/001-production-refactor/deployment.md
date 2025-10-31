# Deployment Guide: Bitcoin Trading System

**Feature**: 001-production-refactor
**Date**: 2025-10-31
**Purpose**: Production deployment procedures and automation

---

## Table of Contents

1. [Git Workflow](#git-workflow)
2. [Deployment Process](#deployment-process)
3. [Rollback Procedure](#rollback-procedure)
4. [Blue-Green Deployment](#blue-green-deployment)
5. [Monitoring and Verification](#monitoring-and-verification)

---

## Git Workflow

### Branch Strategy

```
main (production)
  ↑
  └── feature/001-production-refactor (development)
       ↑
       └── fix/bug-description (hotfixes)
```

### Development Workflow

1. **Feature Development**:
   ```bash
   git checkout -b feature/my-feature
   git add .
   git commit -m "feat: add new feature"
   git push origin feature/my-feature
   ```

2. **Pull Request Review**:
   - Create PR to `main` branch
   - Automated tests run via CI/CD
   - Code review required before merge
   - Squash and merge to maintain clean history

3. **Hotfix Workflow**:
   ```bash
   git checkout main
   git checkout -b fix/critical-bug
   # Make fix
   git commit -m "fix: resolve critical issue"
   git push origin fix/critical-bug
   # Create PR, merge to main, then cherry-pick to feature branch if needed
   ```

---

## Deployment Process

### Automated Deployment Script

The `deploy.sh` script automates the deployment process with health checks and rollback on failure.

#### Pre-Deployment Checklist

- [ ] Code merged to `main` branch
- [ ] All tests passing locally
- [ ] Database migrations reviewed
- [ ] `.env` file configured with production values
- [ ] Backup completed (see `backup_db.sh`)
- [ ] Maintenance window scheduled (if required)

#### Step 1: Pull Latest Code

```bash
cd /opt/trader_bitcoin
git fetch origin
git checkout main
git pull origin main
```

#### Step 2: Run Deployment Script

```bash
./backend/scripts/deployment/deploy.sh
```

The script performs:
1. Version check (tags current deployment)
2. Code pull from git
3. Dependency installation
4. Database migrations in transaction
5. Docker image build
6. Rolling restart with health checks
7. Verification of services
8. Automatic rollback on failure

#### Step 3: Manual Deployment (Alternative)

If automated script fails:

```bash
# 1. Build new images
docker-compose build

# 2. Run migrations
docker-compose run --rm app alembic upgrade head

# 3. Restart services (zero-downtime)
docker-compose up -d --no-deps --build app

# 4. Verify health
curl http://localhost:5611/api/health
curl http://localhost:5611/api/ready
```

---

## Rollback Procedure

### Automated Rollback

Use the `rollback.sh` script for quick rollback:

```bash
./backend/scripts/deployment/rollback.sh [previous_version]
```

Example:
```bash
# Rollback to previous version
./backend/scripts/deployment/rollback.sh

# Rollback to specific version
./backend/scripts/deployment/rollback.sh v1.2.3
```

### Manual Rollback Steps

#### 1. Identify Current Version

```bash
docker inspect trader_app | grep 'Image'
git describe --tags
```

#### 2. Rollback Docker Images

```bash
# Stop current containers
docker-compose down

# Checkout previous version
git checkout <previous_tag>

# Rebuild and start
docker-compose up -d --build
```

#### 3. Rollback Database (If Needed)

```bash
# Check migration history
alembic history

# Rollback one migration
alembic downgrade -1

# Rollback to specific revision
alembic downgrade <revision_id>
```

#### 4. Verify Rollback

```bash
# Check health
curl http://localhost:5611/api/health

# Check version
docker exec trader_app cat /app/VERSION

# Check logs
docker-compose logs --tail=100 app
```

---

## Blue-Green Deployment

### Overview

Blue-Green deployment provides zero-downtime updates by running two identical environments:

- **Blue**: Current production environment
- **Green**: New version being deployed

### Setup with Traefik

#### 1. Deploy Green Environment

```bash
# Create green compose file
cp docker-compose.yml docker-compose.green.yml

# Modify ports and container names
# app:5611 -> app-green:5612
# trader_app -> trader_app_green

# Deploy green environment
docker-compose -f docker-compose.green.yml up -d
```

#### 2. Run Migrations on Green

```bash
docker-compose -f docker-compose.green.yml run --rm app alembic upgrade head
```

#### 3. Verify Green Environment

```bash
# Health check
curl http://localhost:5612/api/health

# Run smoke tests
./backend/scripts/deployment/smoke_test.sh http://localhost:5612
```

#### 4. Switch Traefik Traffic

Update Traefik labels to route to green:

```yaml
# docker-compose.yml
labels:
  - "traefik.http.routers.trader.rule=Host(`oaa.finan.club`)"
  - "traefik.http.services.trader.loadbalancer.server.url=http://app-green:5612"
```

Reload Traefik:
```bash
docker-compose -f traefik-compose.yml up -d
```

#### 5. Monitor and Verify

```bash
# Monitor logs
docker-compose -f docker-compose.green.yml logs -f app

# Check metrics
curl http://oaa.finan.club/api/metrics
```

#### 6. Cleanup Blue Environment

After 24-hour soak period:
```bash
docker-compose down  # Stop blue environment
mv docker-compose.green.yml docker-compose.yml
```

### Rollback Blue-Green

If green deployment fails:
```bash
# Switch Traefik back to blue
# Update labels to point to app:5611
docker-compose up -d

# Stop green environment
docker-compose -f docker-compose.green.yml down
```

---

## Monitoring and Verification

### Post-Deployment Checks

#### 1. Health Endpoints

```bash
# Application health
curl http://oaa.finan.club/api/health
# Expected: {"status": "ok", "uptime": 123}

# Readiness check
curl http://oaa.finan.club/api/ready
# Expected: {"ready": true, "checks": {"database": "ok", "hyperliquid_api": "ok"}}
```

#### 2. Sync Status

```bash
curl http://oaa.finan.club/api/sync/status
# Verify last_sync_time is recent (< 60 seconds ago)
```

#### 3. Prometheus Metrics

```bash
curl http://oaa.finan.club/api/metrics
# Check for errors in metrics
```

#### 4. Database Connectivity

```bash
docker exec trader_postgres pg_isready -U trader
# Expected: postgres:5432 - accepting connections
```

#### 5. Log Review

```bash
# Check for errors in last 100 lines
docker-compose logs --tail=100 app | grep -i error

# Analyze logs
docker logs trader_app 2>&1 | grep '^{' | python backend/scripts/maintenance/analyze_logs.py -
```

### Continuous Monitoring

#### Grafana Dashboards

Access Grafana: `http://localhost:3000` (default: admin/admin)

Monitor:
- Account balance trends
- Sync operation success rate
- API response times (p95 < 200ms)
- Error rates by service
- Database connection pool usage

#### Alert Channels

Configure alerting in `monitoring/prometheus_alerts.yml`:
- Sync failures (>3 consecutive)
- High error rate (>5% over 5 minutes)
- Slow API responses (p95 > 500ms)
- Database pool exhaustion
- Circuit breaker open

---

## Deployment Frequency

### Recommended Schedule

- **Hotfixes**: As needed (critical bugs)
- **Minor updates**: Weekly (Friday afternoon)
- **Major updates**: Monthly (first Monday)
- **Security patches**: Within 24 hours of disclosure

### Maintenance Windows

- **Regular maintenance**: Friday 10 PM - 11 PM UTC
- **Emergency maintenance**: Announced via status page
- **Zero-downtime updates**: Any time (using blue-green)

---

## Troubleshooting Deployments

### Common Issues

#### 1. Migration Fails

```bash
# Check migration status
alembic current

# Review migration file
cat backend/alembic/versions/<latest>.py

# Manually fix and retry
alembic upgrade head
```

#### 2. Container Won't Start

```bash
# Check logs
docker-compose logs app

# Check environment variables
docker-compose config

# Verify .env file
cat .env | grep -v '^#'
```

#### 3. Health Check Fails

```bash
# Debug health endpoint
docker exec trader_app curl http://localhost:5611/api/health

# Check database connection
docker exec trader_app python -c "from backend.database.connection import engine; print(engine)"
```

#### 4. Rollback Fails

Use manual rollback procedure and restore from backup:

```bash
# Stop all services
docker-compose down

# Restore database backup
./backend/scripts/maintenance/restore_db.sh <backup_file>

# Checkout last known good version
git checkout <previous_tag>

# Redeploy
./backend/scripts/deployment/deploy.sh
```

---

## Deployment History

Track all deployments in `deployments.log`:

```
2025-10-31 17:00:00 | v1.0.0 | Initial production deployment | SUCCESS
2025-11-01 09:00:00 | v1.0.1 | Fix sync timeout bug | SUCCESS
2025-11-05 10:00:00 | v1.1.0 | Add AI cost optimization | ROLLED_BACK (migration failed)
2025-11-05 14:00:00 | v1.1.1 | Fix migration issue | SUCCESS
```

Format:
```
<timestamp> | <version> | <description> | <status>
```

---

## Security Considerations

### Secrets Management

- Never commit secrets to git
- Use `.env` file (gitignored)
- Rotate API keys every 90 days
- Use Docker secrets for sensitive data in production

### SSL/TLS

- Traefik handles SSL termination
- Automatic Let's Encrypt certificates
- Force HTTPS redirects

### Network Security

- Firewall configured (UFW)
- Only ports 80, 443, 22 exposed
- Database not exposed to public internet
- Redis not exposed to public internet

---

## Rollout Strategy

### Percentage Rollout (Canary)

For major changes, use percentage-based rollout:

1. **5%**: Deploy to 5% of users (1 server)
2. **25%**: If metrics good after 1 hour
3. **50%**: If metrics good after 4 hours
4. **100%**: If metrics good after 24 hours

Monitor error rates and rollback if >1% increase.

---

## Support and Resources

- **Runbook**: `backend/ARCHITECTURE.md`
- **API Docs**: `specs/001-production-refactor/contracts/`
- **Backup Procedures**: `specs/001-production-refactor/quickstart.md`
- **Incident Response**: See `INCIDENT_RESPONSE.md` (to be created)

**Emergency Contact**: System administrator on-call
