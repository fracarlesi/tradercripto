# Quickstart Guide: Production-Ready Bitcoin Trading System

**Feature**: 001-production-refactor
**Date**: 2025-10-31
**Purpose**: Step-by-step guide for local development setup and production deployment

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Local Development Setup](#local-development-setup)
3. [Running the Application](#running-the-application)
4. [Production Deployment](#production-deployment)
5. [Configuration Reference](#configuration-reference)
6. [Troubleshooting](#troubleshooting)

---

## Prerequisites

### Required Software

| Tool | Version | Purpose | Installation |
|------|---------|---------|--------------|
| **Python** | 3.11+ | Backend runtime | https://www.python.org/downloads/ |
| **uv** | Latest | Python package manager | `pip install uv` |
| **Node.js** | 20+ | Frontend build | https://nodejs.org/ |
| **pnpm** | Latest | Frontend package manager | `npm install -g pnpm` |
| **Docker** | 20+ | Containerization | https://docs.docker.com/get-docker/ |
| **Docker Compose** | V2 | Orchestration | Included with Docker Desktop |
| **PostgreSQL** | 12+ (production) | Database | https://www.postgresql.org/download/ |

### Optional Tools

- **SQLite Browser**: View local SQLite database (dev mode)
- **pgAdmin**: Manage PostgreSQL database (production)
- **Postman/Insomnia**: Test API endpoints

### API Credentials Required

1. **Hyperliquid Account**:
   - Private key (hex format with 0x prefix)
   - Wallet address (optional - can be derived from private key)
   - Sign up: https://app.hyperliquid.xyz/

2. **DeepSeek API Key**:
   - API key for AI trading decisions
   - Sign up: https://www.deepseek.com/

---

## Local Development Setup

### 1. Clone Repository

```bash
git clone <repository-url>
cd trader_bitcoin
git checkout 001-production-refactor
```

### 2. Backend Setup (Python)

```bash
cd backend

# Install dependencies using uv
uv sync

# Activate virtual environment
source .venv/bin/activate  # Linux/Mac
# or
.venv\Scripts\activate  # Windows
```

### 3. Frontend Setup (Node.js)

```bash
cd ../frontend

# Install dependencies
pnpm install
```

### 4. Configure Environment Variables

Create `.env` file in project root:

```bash
# Copy example template
cp .env.example .env

# Edit .env with your credentials
nano .env  # or use your preferred editor
```

**Required `.env` Contents** (see [Configuration Reference](#configuration-reference) for full list):

```bash
# Database (SQLite for local dev)
DATABASE_URL=sqlite+aiosqlite:///./data.db

# Hyperliquid API (REQUIRED)
HYPERLIQUID_PRIVATE_KEY=0x1234567890abcdef...  # Your private key
HYPERLIQUID_WALLET_ADDRESS=0xabcdef...          # Optional (auto-derived if omitted)
MAX_CAPITAL_USD=53.0                            # Maximum trading capital

# DeepSeek API (REQUIRED for AI trading)
DEEPSEEK_API_KEY=sk-...                         # Your DeepSeek API key
DEEPSEEK_BASE_URL=https://api.deepseek.com      # API endpoint

# Application Settings
DEBUG=false                                      # Set to true for verbose logging
SQL_DEBUG=false                                  # Set to true to log SQL queries
```

**Security**: Never commit `.env` file to version control!

### 5. Initialize Database

```bash
cd backend

# Run database migrations (creates tables)
alembic upgrade head

# Verify database created
ls -la data.db  # Should exist now
```

### 6. Verify Setup

```bash
# Test backend startup (should start without errors)
cd backend
uv run uvicorn main:app --reload --port 5611

# Open browser: http://localhost:5611/api/health
# Should return: {"status": "healthy", ...}
```

**Expected Output**:
```
INFO:     Uvicorn running on http://0.0.0.0:5611 (Press CTRL+C to quit)
INFO:     Started reloader process [12345] using StatReload
INFO:     Started server process [12346]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
```

---

## Running the Application

### Development Mode (Recommended for Local Work)

#### Option 1: Separate Backend + Frontend

**Terminal 1 - Backend**:
```bash
cd backend
source .venv/bin/activate
uv run uvicorn main:app --reload --host 0.0.0.0 --port 5611
```

**Terminal 2 - Frontend**:
```bash
cd frontend
pnpm run dev
```

**Access**:
- Frontend: http://localhost:5173 (Vite dev server with hot reload)
- Backend API: http://localhost:5611/api
- API Docs: http://localhost:5611/docs (Swagger UI)

**Benefits**:
- Hot reload for both frontend and backend
- Faster iteration cycle
- Separate logs for debugging

#### Option 2: Integrated (Frontend Built)

```bash
# Build frontend once
cd frontend
pnpm run build

# Copy build to backend static folder
cp -r dist ../backend/static

# Run backend (serves frontend at root)
cd ../backend
uv run uvicorn main:app --reload --host 0.0.0.0 --port 5611
```

**Access**: http://localhost:5611 (single server for everything)

### Production Mode (Docker)

```bash
# Build and start containers
docker-compose up --build

# Run in background (detached)
docker-compose up -d

# View logs
docker-compose logs -f

# Stop containers
docker-compose down
```

**Access**: http://localhost:5611 (Docker container)

---

## Production Deployment

### Prerequisites

1. **Server**: Linux VPS or cloud VM (Ubuntu 22.04 recommended)
   - Minimum: 1 vCPU, 2GB RAM, 20GB disk
   - Recommended: 2 vCPU, 4GB RAM, 50GB disk

2. **Domain**: Optional but recommended (e.g., `oaa.finan.club`)

3. **Reverse Proxy**: Traefik, Nginx, or similar for SSL/TLS

### Step-by-Step Deployment

#### 1. Prepare Server

```bash
# SSH into server
ssh user@your-server.com

# Update system
sudo apt update && sudo apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Install Docker Compose
sudo apt install docker-compose-plugin

# Verify installations
docker --version
docker compose version
```

#### 2. Setup PostgreSQL Database

**Option A: Docker PostgreSQL** (Recommended for simple setup)

```yaml
# Add to docker-compose.yml
services:
  postgres:
    image: postgres:14-alpine
    container_name: trader_db
    restart: unless-stopped
    environment:
      POSTGRES_USER: trader
      POSTGRES_PASSWORD: ${DB_PASSWORD}  # Set in .env
      POSTGRES_DB: trader_db
    volumes:
      - postgres_data:/var/lib/postgresql/data
    networks:
      - trader_network
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U trader"]
      interval: 10s
      timeout: 5s
      retries: 5

  app:
    # ... existing app config ...
    environment:
      DATABASE_URL: postgresql+asyncpg://trader:${DB_PASSWORD}@postgres:5432/trader_db
    depends_on:
      postgres:
        condition: service_healthy

volumes:
  postgres_data:

networks:
  trader_network:
```

**Option B: Managed PostgreSQL** (AWS RDS, DigitalOcean, etc.)

- Create managed PostgreSQL instance
- Note connection string: `postgresql+asyncpg://user:pass@host:port/dbname`
- Update `DATABASE_URL` in `.env`

#### 3. Deploy Application

```bash
# Clone repository on server
git clone <repository-url> /opt/trader_bitcoin
cd /opt/trader_bitcoin
git checkout 001-production-refactor

# Create production .env file
cat > .env << EOF
# Database (PostgreSQL)
DATABASE_URL=postgresql+asyncpg://trader:${DB_PASSWORD}@postgres:5432/trader_db
DB_PASSWORD=your-secure-password-here  # Generate strong password

# Hyperliquid API
HYPERLIQUID_PRIVATE_KEY=0x...
HYPERLIQUID_WALLET_ADDRESS=0x...
MAX_CAPITAL_USD=53.0

# DeepSeek API
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_BASE_URL=https://api.deepseek.com

# Production Settings
DEBUG=false
SQL_DEBUG=false
DB_POOL_SIZE=10
DB_MAX_OVERFLOW=5
EOF

# Set secure permissions
chmod 600 .env

# Run database migrations
docker-compose run --rm app alembic upgrade head

# Start application
docker-compose up -d

# Verify health
curl http://localhost:5611/api/health
```

#### 4. Configure Reverse Proxy (Traefik Example)

**Traefik Configuration** (already in `docker-compose.yml`):

```yaml
services:
  app:
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.trader.rule=Host(`oaa.finan.club`)"
      - "traefik.http.routers.trader.entrypoints=websecure"
      - "traefik.http.routers.trader.tls.certresolver=letsencrypt"
      - "traefik.http.services.trader.loadbalancer.server.port=5611"
    networks:
      - traefik

networks:
  traefik:
    external: true
```

**Setup Traefik** (if not already running):

```bash
# Create traefik network
docker network create traefik

# Deploy Traefik (separate compose file)
# See: https://doc.traefik.io/traefik/getting-started/quick-start/
```

#### 5. Setup Monitoring (Optional but Recommended)

**Prometheus + Grafana**:

```bash
# Add to docker-compose.yml
services:
  prometheus:
    image: prom/prometheus:latest
    container_name: prometheus
    volumes:
      - ./monitoring/prometheus.yml:/etc/prometheus/prometheus.yml
      - prometheus_data:/prometheus
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
    ports:
      - "9090:9090"
    networks:
      - trader_network

  grafana:
    image: grafana/grafana:latest
    container_name: grafana
    volumes:
      - grafana_data:/var/lib/grafana
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_PASSWORD}
    ports:
      - "3000:3000"
    networks:
      - trader_network

volumes:
  prometheus_data:
  grafana_data:
```

**Prometheus Configuration** (`monitoring/prometheus.yml`):

```yaml
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: 'trading_system'
    static_configs:
      - targets: ['app:5611']
    metrics_path: '/api/metrics'
```

#### 6. Setup Automated Backups (T088, Enhanced T138-T141)

The system includes production-grade backup scripts with:
- Pre-backup disk space checks
- Post-backup integrity verification
- Alerting on success/failure
- Retention policy (7 daily + 4 weekly backups)

**Backup Scripts**:
- `backend/scripts/maintenance/backup_db.sh` - Main backup script
- `backend/scripts/maintenance/cleanup_backups.sh` - Retention policy enforcement
- `backend/scripts/maintenance/restore_db.sh` - Database restore script

**Cron Job Setup** (T140):

```bash
# Edit crontab
crontab -e

# Add the following lines:

# Daily backup at 2 AM with alerts
0 2 * * * cd /opt/trader_bitcoin && ./backend/scripts/maintenance/backup_db.sh --alert >> /var/log/trader_backup.log 2>&1

# Weekly cleanup on Sundays at 3 AM
0 3 * * 0 cd /opt/trader_bitcoin && ./backend/scripts/maintenance/cleanup_backups.sh >> /var/log/trader_cleanup.log 2>&1

# Optional: Monthly full backup to external storage
0 4 1 * * cd /opt/trader_bitcoin && ./backend/scripts/maintenance/backup_db.sh --alert --keep-days 90 --output-dir /mnt/external/backups >> /var/log/trader_external_backup.log 2>&1
```

**Backup Script Options**:

```bash
# Full backup with all checks
./backend/scripts/maintenance/backup_db.sh --alert --min-space-gb 2

# Test backup (dry run)
./backend/scripts/maintenance/backup_db.sh --dry-run

# Cleanup with custom retention
./backend/scripts/maintenance/cleanup_backups.sh --daily-keep 14 --weekly-keep 8

# Restore backup
./backend/scripts/maintenance/restore_db.sh data/backups/postgres_backup_20250131_020000.sql.gz
```

**Backup Rotation Policy**:
- **Daily backups**: Last 7 days (most recent)
- **Weekly backups**: Last 4 weeks (Sundays only)
- **Compression**: Automatic gzip compression
- **Integrity**: Verified after creation
- **Alerts**: Sent on failure (requires `--alert` flag)

**Monitoring Backups**:

```bash
# View backup logs
tail -f /var/log/trader_backup.log
tail -f /var/log/trader_cleanup.log

# Check backup status
ls -lh /opt/trader_bitcoin/data/backups/

# Verify backup integrity manually
gzip -t /opt/trader_bitcoin/data/backups/postgres_backup_*.sql.gz

# Test restore to staging (recovery time target: <15 minutes)
./backend/scripts/maintenance/restore_db.sh --db-name trader_db_staging backups/latest.sql.gz
```

---

## Configuration Reference

### Environment Variables (Complete List)

#### Database Configuration
```bash
DATABASE_URL=sqlite+aiosqlite:///./data.db          # Dev: SQLite
DATABASE_URL=postgresql+asyncpg://user:pass@host/db # Prod: PostgreSQL
DB_POOL_SIZE=10                                      # PostgreSQL pool size (default: 10)
DB_MAX_OVERFLOW=5                                    # Max overflow connections (default: 5)
```

#### Hyperliquid API
```bash
HYPERLIQUID_PRIVATE_KEY=0x...     # Required: Your Hyperliquid private key
HYPERLIQUID_WALLET_ADDRESS=0x...  # Optional: Auto-derived if omitted
MAX_CAPITAL_USD=53.0               # Required: Maximum trading capital
```

#### DeepSeek AI
```bash
DEEPSEEK_API_KEY=sk-...                      # Required: DeepSeek API key
DEEPSEEK_BASE_URL=https://api.deepseek.com   # Optional: API endpoint
```

#### Application Settings
```bash
DEBUG=false                # Enable debug logging (true/false)
SQL_DEBUG=false            # Log SQL queries (true/false)
LOG_LEVEL=INFO             # Logging level (DEBUG/INFO/WARNING/ERROR)
```

#### Scheduler Settings
```bash
SYNC_INTERVAL_SECONDS=30   # Hyperliquid sync interval (default: 30)
AI_DECISION_INTERVAL=180   # AI decision interval in seconds (default: 180 = 3 minutes)
```

### Default Values

If environment variables are not set, the following defaults apply:

- `DATABASE_URL`: `sqlite+aiosqlite:///./data.db`
- `DB_POOL_SIZE`: `10`
- `DB_MAX_OVERFLOW`: `5`
- `DEBUG`: `false`
- `SQL_DEBUG`: `false`
- `LOG_LEVEL`: `INFO`
- `SYNC_INTERVAL_SECONDS`: `30`
- `AI_DECISION_INTERVAL`: `180`

---

## Troubleshooting

### Common Issues

#### 1. Database Connection Errors

**Symptom**: `sqlalchemy.exc.OperationalError: unable to open database file`

**Solution**:
```bash
# Ensure data directory exists
mkdir -p data

# Check file permissions
chmod 755 data
chmod 644 data/data.db  # If file exists

# Verify DATABASE_URL in .env
cat .env | grep DATABASE_URL
```

#### 2. Hyperliquid API Errors

**Symptom**: `Sync failed: Hyperliquid API timeout` or `Invalid signature`

**Solution**:
```bash
# Verify private key format (must start with 0x)
echo $HYPERLIQUID_PRIVATE_KEY  # Should be 0x followed by 64 hex chars

# Test API connectivity
curl -X POST https://api.hyperliquid.xyz/info -d '{"type":"meta"}'

# Check account balance on Hyperliquid website
# Ensure account has some balance (even $1 is enough for testing)
```

#### 3. Frontend Not Loading

**Symptom**: `404 Not Found` when accessing http://localhost:5611

**Solution**:
```bash
# Rebuild frontend
cd frontend
pnpm run build

# Copy to backend static folder
rm -rf ../backend/static
cp -r dist ../backend/static

# Restart backend
cd ../backend
uv run uvicorn main:app --reload --port 5611
```

#### 4. Docker Container Crashes

**Symptom**: `docker-compose up` exits immediately

**Solution**:
```bash
# Check logs
docker-compose logs app

# Common issues:
# - Missing .env file: Create .env with required variables
# - Port conflict: Change port in docker-compose.yml
# - Database not ready: Add healthcheck dependency

# Rebuild containers
docker-compose down -v  # Remove volumes
docker-compose build --no-cache
docker-compose up
```

#### 5. Sync Showing "Stale Data"

**Symptom**: Frontend shows data freshness > 2 minutes

**Solution**:
```bash
# Check scheduler is running
curl http://localhost:5611/api/sync/status

# Manual sync
curl -X POST http://localhost:5611/api/sync/all

# Check logs for sync errors
docker-compose logs app | grep -i sync

# Verify Hyperliquid API is reachable
curl -X POST https://api.hyperliquid.xyz/info -d '{"type":"meta"}'
```

#### 6. High Memory Usage

**Symptom**: Server runs out of memory, OOM killer terminates container

**Solution**:
```bash
# Check memory usage
docker stats

# Reduce database pool size in .env
DB_POOL_SIZE=5
DB_MAX_OVERFLOW=2

# Add memory limits to docker-compose.yml
services:
  app:
    mem_limit: 1g
    mem_reservation: 512m
```

### Logs and Debugging

#### View Application Logs

```bash
# Docker logs (last 100 lines)
docker-compose logs --tail=100 app

# Follow logs in real-time
docker-compose logs -f app

# Filter for errors
docker-compose logs app | grep -i error

# Export logs to file
docker-compose logs app > app_logs.txt
```

#### Enable Debug Logging

```bash
# Set in .env
DEBUG=true
SQL_DEBUG=true  # Also log SQL queries

# Restart application
docker-compose restart app
```

#### Check Database Contents

**SQLite** (dev):
```bash
# Install sqlite3
sudo apt install sqlite3

# Open database
sqlite3 backend/data.db

# List tables
.tables

# Query accounts
SELECT * FROM accounts;

# Exit
.quit
```

**PostgreSQL** (prod):
```bash
# Connect to database
docker exec -it trader_db psql -U trader trader_db

# List tables
\dt

# Query accounts
SELECT * FROM accounts;

# Exit
\q
```

### Performance Tuning

#### Database Query Optimization

```bash
# Enable query logging
SQL_DEBUG=true

# Analyze slow queries
docker-compose logs app | grep -i "SELECT" | grep -o "[0-9]\+ms" | sort -n

# Add indexes (if needed)
# See data-model.md for recommended indexes
```

#### API Response Time Monitoring

```bash
# Check /api/metrics endpoint
curl http://localhost:5611/api/metrics | grep api_request_duration

# Analyze p95 latency
# Should be < 200ms for most endpoints
```

---

## Next Steps

After successful deployment:

1. **Monitor System Health**:
   - Check `/api/health` endpoint regularly
   - Set up alerts for sync failures
   - Review logs daily for errors

2. **Verify Trading**:
   - Watch AI decision logs in database
   - Verify orders appear on Hyperliquid
   - Check balance updates after trades

3. **Optimize Costs**:
   - Monitor DeepSeek API usage (`/api/metrics`)
   - Adjust AI decision interval if needed
   - Review caching effectiveness

4. **Backup Strategy**:
   - Verify automated backups are running
   - Test restore procedure once
   - Store backups off-server (S3, Backblaze, etc.)

5. **Security Hardening**:
   - Rotate API keys every 90 days
   - Enable firewall (UFW) on server
   - Use strong passwords for database
   - Keep Docker images updated

---

## Support and Resources

- **Project README**: `../README.md`
- **Architecture Documentation**: `../backend/ARCHITECTURE.md`
- **API Documentation**: http://localhost:5611/docs (when running)
- **OpenAPI Specs**: `./contracts/*.yaml`
- **Data Model**: `./data-model.md`

**Need Help?**
- Check Troubleshooting section above
- Review application logs for error messages
- Consult Hyperliquid API docs: https://hyperliquid.gitbook.io/
- Check DeepSeek API status: https://www.deepseek.com/
