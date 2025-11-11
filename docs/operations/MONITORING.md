# Monitoring & Debugging Guide

Complete guide for monitoring system health and debugging issues.

## Health Check Endpoints

### 1. System Health (`/api/health`)

**Purpose**: Monitor overall system status

```bash
curl http://localhost:5611/api/health
```

**Response**:
```json
{
  "status": "ok",  // "ok", "degraded", or "down"
  "uptime": 3600,
  "last_sync_time": "2025-11-10T14:30:00Z",
  "sync_status": "ok",  // "ok", "stale", or "failing"
  "message": "All systems operational"
}
```

**Status Meanings**:
- `ok`: System fully operational, sync working
- `degraded`: Sync stale (2-5 minutes since last sync)
- `down`: Sync failing (3+ consecutive failures or 5+ min)

**HTTP Status Codes**:
- `200`: Healthy or degraded
- `503`: System down (critical failure)

### 2. Readiness Check (`/api/readiness`)

**Purpose**: Check if system ready to accept traffic (used by load balancers)

```bash
curl http://localhost:5611/api/readiness
```

**Response**:
```json
{
  "ready": true,
  "checks": {
    "database": "ok",
    "hyperliquid_api": "ok",
    "environment": "ok"
  },
  "message": "System ready"
}
```

**Use Cases**:
- Kubernetes readiness probes
- Zero-downtime deployments
- Load balancer health checks

---

## Log Monitoring

### Structured Logging Format

**File**: `backend/config/logging.py:13-72`

**Production** (JSON format):
```json
{
  "timestamp": "2025-11-10T14:30:00.123Z",
  "level": "INFO",
  "logger": "services.auto_trader",
  "message": "AI Trading Cycle Started",
  "context": {
    "account_id": 1,
    "max_ratio": 0.2
  }
}
```

**Development** (simple format):
```
2025-11-10 14:30:00 - services.auto_trader - INFO - AI Trading Cycle Started
```

### Key Log Patterns

**Job Execution**:
```bash
# AI Trading cycles
grep "AI Trading Cycle Started\|AI Trading Cycle Completed" logs

# Hyperliquid sync
grep "Sync completed for account\|Sync failed" logs

# Portfolio snapshots
grep "Portfolio snapshot captured" logs
```

**Errors & Warnings**:
```bash
# All errors
grep "ERROR" logs | grep -v "DEBUG"

# Rate limiting (429)
grep "429\|RateLimitExceeded" logs

# Circuit breaker
grep "Circuit breaker\|Circuit opened" logs

# WebSocket errors
grep "WebSocket.*error\|WebSocket disconnected" logs
```

**Performance**:
```bash
# Long-running operations
grep "duration_ms.*[0-9]{4,}" logs  # >1000ms

# API call counts
grep "API call" logs | wc -l

# Database pool exhaustion
grep "PoolExhaustedException\|pool timeout" logs
```

### Log Filtering by Request ID

Every HTTP request has unique `X-Request-ID` header:

```bash
# Find all logs for specific request
grep "request_id.*abc-123" logs.json | jq '.'

# Trace error across services
grep "request_id.*abc-123" logs.json | jq '.exception.stack_trace'
```

---

## Debug Workflows

### Issue: Balance Shows $0

**Diagnosis**:
1. Check Hyperliquid API:
   ```bash
   cd backend/
   python3 -c "
   import asyncio
   from services.trading.hyperliquid_trading_service import hyperliquid_trading_service
   async def check():
       state = await hyperliquid_trading_service.get_user_state_async()
       print(f'Real balance: \${state[\"marginSummary\"][\"accountValue\"]}')
   asyncio.run(check())
   "
   ```

2. Check database sync:
   ```bash
   sqlite3 backend/data.db "
   SELECT datetime(snapshot_time), total_assets
   FROM portfolio_snapshots
   ORDER BY snapshot_time DESC LIMIT 5;
   "
   ```

3. Force manual sync:
   ```bash
   curl -X POST http://localhost:5611/api/sync/account/1
   ```

**Common Causes**:
- Fallback value used (`or '0'` pattern) → Remove fallbacks
- Sync job not running → Check scheduler status
- API credentials invalid → Check `.env` file

---

### Issue: Positions Missing

**Diagnosis**:
```bash
# Check sync status
curl http://localhost:5611/api/health

# Check last sync time
docker compose logs app | grep "Sync completed" | tail -5

# Manually trigger sync
curl -X POST http://localhost:5611/api/sync/account/1
```

**Common Causes**:
- Not synced yet (wait 30s for next cycle)
- Sync job failing (check error logs)
- Positions closed on Hyperliquid (check exchange directly)

---

### Issue: AI Not Trading

**Diagnosis**:
1. Check AI trading job:
   ```bash
   # Verify job registered
   curl http://localhost:5611/api/config/scheduler-status | jq '.jobs[] | select(.id=="ai_crypto_trade")'

   # Check execution logs
   docker compose logs app | grep "AI Trading Cycle" | tail -10
   ```

2. Check AI API key:
   ```bash
   # Test DeepSeek API
   curl https://api.deepseek.com/v1/models \
     -H "Authorization: Bearer $DEEPSEEK_API_KEY"
   ```

3. Check decision cache:
   ```bash
   # Recent decisions
   sqlite3 backend/data.db "
   SELECT datetime(decision_time), operation, symbol, executed
   FROM ai_decision_logs
   ORDER BY decision_time DESC LIMIT 10;
   "
   ```

**Common Causes**:
- Invalid API key → Update in settings
- Decision cache preventing trades → Wait 10 minutes
- Market conditions trigger HOLD → Check AI reasoning in logs
- Validation failing → Check portfolio balance

---

### Issue: High API Call Rate (429 Errors)

**Diagnosis**:
```bash
# Count 429 errors
docker compose logs app | grep "429" | wc -l

# Check which endpoint
docker compose logs app | grep "429" | grep -oP "POST \K.*" | sort | uniq -c

# Monitor job execution timing
docker compose logs app | grep "job started\|job completed" | tail -20
```

**Solutions**:
1. Increase job intervals:
   ```bash
   # Edit .env
   AI_DECISION_INTERVAL=240  # 4 min instead of 3
   SYNC_INTERVAL_SECONDS=60  # 60s instead of 30
   ```

2. Reduce symbol count in technical analysis:
   ```python
   # Edit technical_analysis_service.py
   MAX_SYMBOLS = 50  # Reduce from 100+
   ```

3. Add delays between API calls (already implemented)

---

### Issue: Orders Not Executing

**Diagnosis**:
1. Check order validation:
   ```bash
   docker compose logs app | grep "Decision validation failed"
   ```

2. Check Hyperliquid response:
   ```bash
   docker compose logs app | grep "Order placed\|Order rejected"
   ```

3. Check available balance:
   ```bash
   curl http://localhost:5611/api/accounts/1 | jq '.portfolio'
   ```

**Common Causes**:
- Insufficient balance → Check `withdrawable` funds
- Order size too small → Min $10 (Hyperliquid limit)
- Max ratio exceeded → Reduce `max_ratio` in settings
- API rate limited → Wait for rate limit to clear

---

## Performance Monitoring

### Database Connection Pool

```bash
# Check pool status (if metrics enabled)
curl http://localhost:5611/metrics | grep db_pool_
```

**Healthy values**:
- `db_pool_available`: 5-10 (out of 10 total)
- `db_pool_timeout_errors`: 0
- `db_pool_checkout_time`: <100ms

**Warning signs**:
- `db_pool_available` = 0 → Pool exhausted
- `db_pool_timeout_errors` > 0 → Increase pool size
- `db_pool_checkout_time` > 500ms → Query optimization needed

### WebSocket Connections

```bash
# Count active connections
docker compose logs app | grep "WebSocket connected" | wc -l

# Check for disconnections
docker compose logs app | grep "WebSocket disconnected" | tail -10
```

### AI Cost Tracking

```bash
# Get current usage
curl http://localhost:5611/api/ai/usage
```

**Response**:
```json
{
  "today": {
    "calls": 120,
    "tokens": 150000,
    "cost": 0.75,
    "cost_per_call": 0.00625
  },
  "projections": {
    "monthly": 22.50,
    "yearly": 273.75
  }
}
```

---

## Alert Configuration (Optional)

**File**: `backend/config/settings.py:97-109`

**Enable alerts**:
```bash
# Edit .env
ALERT_ENABLED=true
ALERT_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK
ALERT_EMAIL_RECIPIENTS=admin@example.com,ops@example.com
```

**Alert triggers**:
- Sync failures (3+ consecutive)
- Circuit breaker opens
- Pool exhaustion
- Stop-loss triggered

---

## Production Monitoring Stack (Optional)

### Prometheus + Grafana

**Setup**:
```bash
# Add to docker-compose.yml
services:
  prometheus:
    image: prom/prometheus
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml

  grafana:
    image: grafana/grafana
    ports:
      - "3000:3000"
```

**Metrics exposed** (`/metrics` endpoint):
- `trading_system_sync_success_total`
- `trading_system_sync_failure_total`
- `trading_system_sync_duration_seconds`
- `trading_system_db_pool_available`
- `trading_system_ai_cache_hits_total`

### Grafana Dashboard

**Panels**:
1. Sync success rate (last hour)
2. API call rate (calls/min)
3. Order execution success rate
4. Database pool utilization
5. AI cost per hour

---

## Troubleshooting Checklist

When system has issues, check in this order:

1. ✅ **Health check**: `curl /api/health`
2. ✅ **Scheduler status**: `curl /api/config/scheduler-status`
3. ✅ **Error logs**: `grep "ERROR" logs`
4. ✅ **Rate limiting**: `grep "429" logs`
5. ✅ **Database**: Test connection with `/api/readiness`
6. ✅ **Hyperliquid API**: Test with direct API call
7. ✅ **WebSocket**: Check browser console for disconnect
8. ✅ **Environment**: Verify `.env` values

---

## Related Documentation

- **[SCHEDULED_JOBS.md](SCHEDULED_JOBS.md)** - Job details and troubleshooting
- **[SYSTEM_ORCHESTRATION.md](../architecture/SYSTEM_ORCHESTRATION.md)** - Operational flow
- **[OVERVIEW.md](../architecture/OVERVIEW.md)** - System architecture
