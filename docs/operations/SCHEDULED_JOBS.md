# Scheduled Jobs - Background Task Management

Complete reference for all background jobs running in the Bitcoin Trading System.

## Table of Contents

- [Job Overview](#job-overview)
- [Job Details](#job-details)
- [Rate Limiting](#rate-limiting)
- [Job Dependencies](#job-dependencies)
- [Optimization History](#optimization-history)
- [Troubleshooting](#troubleshooting)

---

## Job Overview

### Active Scheduled Jobs

| Job Name | Interval | Scheduler | File Reference | API Calls | Status |
|----------|----------|-----------|----------------|-----------|--------|
| **AI Trading** | 180s (3min) | APScheduler | `main.py:161-170` | 2-3/cycle | ✅ Active |
| **Hyperliquid Sync** | 30s | APScheduler | `main.py:131-137` | 1/cycle | ✅ Active |
| **Stop Loss Check** | 60s | APScheduler | `main.py:147-154` | 1/cycle | ✅ Active |
| **Price Cache Cleanup** | 120s (2min) | TaskScheduler | `startup.py:27-35` | 0 (local) | ✅ Active |
| **Portfolio Snapshot** | 300s (5min) | TaskScheduler | `startup.py:51-65` | 1/cycle | ✅ Active |
| **Counterfactual Learning** | 3600s (1h) | TaskScheduler | `startup.py:72-89` | 0.1/cycle | ✅ Active |
| **Self-Analysis** | 10800s (3h) | TaskScheduler | `startup.py:91-106` | 0/cycle | ✅ Active |
| **AI Usage Reset** | Daily 00:00 | APScheduler | `main.py:140-144` | 0 (local) | ✅ Active |

### API Load Summary

**Current Load** (after 2025-11-09 optimization):
- **Calls per minute**: ~3.5
- **Calls per hour**: ~210
- **Calls per day**: ~5,040

**Previous Load** (before optimization):
- **Calls per minute**: ~5.5
- **Calls per hour**: ~330
- **Calls per day**: ~7,920

**Reduction**: **-40% API calls** → Reduced rate limiting risk

---

## Job Details

### 1. AI Trading Job

**Purpose**: Make AI-powered trading decisions and execute orders

**Interval**: 180 seconds (3 minutes)

**Scheduler**: APScheduler (non-blocking, threaded)

**File**: `main.py:161-170`, execution in `services/auto_trader.py:32-233`

**Configuration**:
```python
scheduler_service.add_sync_job(
    job_func=lambda: place_ai_driven_crypto_order(max_ratio=0.2),
    interval_seconds=180,  # 3 minutes
    job_id="ai_crypto_trade"
)
```

**Execution Flow**:
1. Fetch market prices (1 API call - `all_mids()`)
2. Build portfolio from Hyperliquid (1 API call - `user_state()`)
3. Calculate technical factors (~90s processing, multiple API calls for klines)
4. Call DeepSeek AI API (1 API call)
5. Execute order if decision is BUY/SHORT (2 API calls - leverage + order)
6. Post-trade sync (1 API call)

**API Calls**: 2-3 per cycle (depends on whether order is placed)

**Average Duration**: ~98 seconds (most time in technical analysis)

**Critical Notes**:
- APScheduler runs each job in separate thread
- Long-running technical analysis (90s) does NOT block other jobs
- Decision cache prevents duplicate trades (10-minute window)

---

### 2. Hyperliquid Sync Job

**Purpose**: Synchronize account data from Hyperliquid DEX to local database

**Interval**: 30 seconds

**Scheduler**: APScheduler

**File**: `main.py:131-137`, execution in `services/trading/sync_jobs.py:18-32`

**Configuration**:
```python
scheduler_service.add_sync_job(
    job_func=periodic_sync_job,
    interval_seconds=30,  # From settings.sync_interval_seconds
    job_id="hyperliquid_sync",
)
```

**Execution Flow**:
1. Fetch user state (`user_state()` API call)
2. Fetch recent fills (`user_fills()` API call - combined with user_state)
3. Update database:
   - Clear and recreate positions
   - Upsert orders (deduplicate by order_id)
   - Upsert trades (deduplicate by trade_id)

**API Calls**: 1 per cycle (user_state includes fills)

**Average Duration**: ~2 seconds

**Critical Notes**:
- **Single source of truth**: All balance/position data comes from Hyperliquid
- Database is a **display cache**, NOT authoritative
- Circuit breaker opens after 5 consecutive failures (60s timeout)

---

### 3. Stop Loss Check Job

**Purpose**: Monitor positions for -5% loss threshold and close if hit

**Interval**: 60 seconds (optimized from 30s on 2025-11-09)

**Scheduler**: APScheduler

**File**: `main.py:147-154`, execution in `services/auto_trader.py:340-420`

**Configuration**:
```python
scheduler_service.add_sync_job(
    job_func=check_stop_loss_async,
    interval_seconds=60,
    job_id="stop_loss_check"
)
```

**Execution Flow**:
1. Fetch current positions from Hyperliquid
2. Calculate PNL for each position
3. If any position < -5%:
   - Place market order to close position
   - Log stop-loss trigger
   - Notify via alert system (if enabled)

**API Calls**: 1 per cycle (user_state)

**Average Duration**: ~1 second (unless stop-loss triggered)

**Critical Notes**:
- Optimized from 30s to 60s interval (less critical, still catches losses quickly)
- Only triggers on OPEN positions
- Bypass AI decision - immediate market close

---

### 4. Price Cache Cleanup Job

**Purpose**: Remove expired price cache entries to prevent memory bloat

**Interval**: 120 seconds (2 minutes)

**Scheduler**: TaskScheduler (custom threading-based)

**File**: `startup.py:27-35`, execution in `services/market_data/price_cache.py:45-65`

**Configuration**:
```python
task_scheduler.add_interval_task(
    task_func=clear_expired_prices,
    interval_seconds=120,
    task_id="price_cache_cleanup",
)
```

**Execution Flow**:
1. Iterate through price cache entries
2. Check `timestamp + TTL < now`
3. Delete expired entries

**API Calls**: 0 (local memory operation)

**Average Duration**: <100ms

**Critical Notes**:
- Price cache TTL: 30 seconds
- Prevents memory leaks from stale price data
- Thread-safe with `Lock` synchronization

---

### 5. Portfolio Snapshot Job

**Purpose**: Capture portfolio state for historical charts

**Interval**: 300 seconds (5 minutes)

**Scheduler**: TaskScheduler

**File**: `startup.py:51-65`, execution in `services/portfolio_snapshot_service.py:15-80`

**Configuration**:
```python
task_scheduler.add_interval_task(
    task_func=capture_snapshots_wrapper,
    interval_seconds=300,
    task_id="portfolio_snapshot_capture",
)
```

**Execution Flow**:
1. Fetch current portfolio state from Hyperliquid
2. Calculate total assets, positions value, PNL
3. Insert snapshot into `portfolio_snapshots` table

**API Calls**: 1 per cycle (user_state)

**Average Duration**: ~1 second

**Critical Notes**:
- Data used ONLY for charts (not real-time display)
- Retention: Unlimited (historical analysis)
- Immutable once created (append-only table)

---

### 6. Counterfactual Learning Job

**Purpose**: Calculate P&L for past AI decisions (LONG, SHORT, HOLD alternatives)

**Interval**: 3600 seconds (1 hour)

**Scheduler**: TaskScheduler

**File**: `startup.py:72-89`, execution in `services/learning/decision_snapshot_service.py:150-250`

**Configuration**:
```python
task_scheduler.add_interval_task(
    task_func=calculate_counterfactuals_wrapper,
    interval_seconds=3600,  # 1 hour
    task_id="counterfactual_calculation",
)
```

**Execution Flow**:
1. Find decision snapshots older than 24h without counterfactuals
2. For each snapshot:
   - Fetch historical price 24h after decision (CCXT API)
   - Calculate P&L for LONG, SHORT, HOLD
   - Determine optimal decision
   - Calculate regret = optimal_pnl - actual_pnl
3. Update snapshot with counterfactuals

**API Calls**: ~0.1 per cycle average (only for snapshots needing price fetch)

**Average Duration**: ~2-5 seconds (depends on pending snapshots)

**Critical Notes**:
- Runs AFTER 24h delay (need exit price)
- Rate limiting: 2s delay between CCXT requests
- Enables self-learning system to analyze decision quality

---

### 7. Self-Analysis Job

**Purpose**: Analyze patterns in past decisions and suggest weight optimizations

**Interval**: 10800 seconds (3 hours)

**Scheduler**: TaskScheduler

**File**: `startup.py:91-106`, execution in `services/learning/deepseek_self_analysis_service.py:50-400`

**Configuration**:
```python
task_scheduler.add_interval_task(
    task_func=run_self_analysis_wrapper,
    interval_seconds=10800,  # 3 hours
    task_id="self_analysis",
)
```

**Execution Flow**:
1. Fetch 50+ decision snapshots with counterfactuals
2. Analyze patterns:
   - Win rate per indicator
   - Common mistakes (ignored signals)
   - Optimal weights based on performance
3. Log insights (NOT auto-applied for safety)
4. Optional: Update strategy_weights in database

**API Calls**: 0 (local database analysis)

**Average Duration**: ~5-10 seconds

**Critical Notes**:
- Requires minimum 50 decisions with counterfactuals
- Weight suggestions logged but NOT auto-applied
- Future enhancement: Automatic weight adjustment with backtesting

---

### 8. AI Usage Reset Job

**Purpose**: Reset daily AI API usage counters at midnight

**Interval**: Daily at 00:00 (cron job)

**Scheduler**: APScheduler

**File**: `main.py:140-144`, execution in `services/infrastructure/usage_tracker.py:85-95`

**Configuration**:
```python
scheduler_service.add_cron_job(
    job_func=reset_ai_usage_daily,
    hour=0,
    minute=0,
    job_id="ai_usage_daily_reset"
)
```

**Execution Flow**:
1. Reset daily call count to 0
2. Reset daily token count to 0
3. Reset daily cost to 0.0
4. Keep cumulative counters unchanged

**API Calls**: 0 (local memory operation)

**Average Duration**: <10ms

**Critical Notes**:
- Enables daily/monthly cost projections
- Thread-safe with `Lock` synchronization
- Metrics exposed via `/api/ai/usage` endpoint

---

## Rate Limiting

### Hyperliquid API Limits

**Official Limits**:
- ~10-20 requests per second
- Burst allowance: ~50 requests in 5 seconds
- Rate limit error: `429 Too Many Requests`

**Our Strategy**:
1. **Sequential API calls**: `MAX_WORKERS=1` with `150ms` delays
2. **Reduced job frequency**: Optimized intervals (e.g., stop-loss 30s → 60s)
3. **Retry with backoff**: Exponential backoff on 429 errors (1s, 2s, 4s)
4. **Job coordination**: APScheduler threads prevent job overlap

### Rate Limit Symptoms

**Error Message**:
```
ccxt.base.errors.RateLimitExceeded: hyperliquid POST https://api.hyperliquid.xyz/info 429 Too Many Requests
```

**When it happens**:
- Multiple jobs execute simultaneously
- Technical analysis processes 100+ symbols
- Burst of API calls in short time window

**Impact**:
- Orders delayed 2-5 minutes until rate limit clears
- System retries automatically (no manual intervention needed)

**Mitigation** (already implemented):
- Reduced API load by 40% (2025-11-09 optimization)
- Sequential symbol processing in technical analysis
- Job intervals staggered to prevent overlap

---

## Job Dependencies

### Dependency Graph

```
┌─────────────────────────────────────────────────────────┐
│  Scheduler (MUST start first)                           │
└────────────┬────────────────────────────────────────────┘
             │
             ├─────────────────────────────────────────────┐
             │                                             │
             ▼                                             ▼
┌────────────────────────┐                   ┌────────────────────────┐
│  Market Data Tasks     │                   │  Interval Tasks        │
│  (empty placeholder)   │                   │  (independent)         │
└────────────┬───────────┘                   └────────────────────────┘
             │
             ▼
┌────────────────────────┐
│  Auto Trading          │
│  (depends on market)   │
└────────────────────────┘
```

**Critical Dependencies**:
1. **Scheduler → All Jobs**: Scheduler MUST start before any job registration
2. **Market Data → Auto Trading**: Auto trading needs market data setup (currently empty)
3. **AI Trading → Hyperliquid Sync**: Post-trade sync depends on sync service availability

**Independent Jobs** (no dependencies):
- Price cache cleanup
- Portfolio snapshot
- Counterfactual learning
- Self-analysis
- AI usage reset

---

## Optimization History

### 2025-11-09: -40% API Call Reduction

**Changes Made**:

1. **Removed duplicate sync job**:
   - ❌ Deleted: `sync_all_active_accounts` (60s interval)
   - ✅ Kept: `periodic_sync_job` in main.py (30s interval)
   - Reason: Both did the same thing - redundant

2. **Removed empty placeholder**:
   - ❌ Deleted: `setup_market_tasks()` function call
   - Reason: Function was empty (no tasks registered)

3. **Reduced stop-loss frequency**:
   - ❌ Was: 30 seconds
   - ✅ Now: 60 seconds
   - Reason: Less critical, still catches -5% losses quickly

**Impact**:
- Before: ~5.5 calls/min = ~330/hour
- After: ~3.5 calls/min = ~210/hour
- **Reduction**: -40% API calls

**Files Modified**:
- `backend/services/startup.py` (removed duplicate job)
- `backend/main.py` (updated stop-loss interval)

---

## Troubleshooting

### Job Not Running

**Symptoms**:
- Logs show "Added task X" but no execution logs
- Expected behavior not occurring

**Diagnosis**:
```bash
# Check scheduler status
curl http://localhost:5611/api/config/scheduler-status

# Check logs for job registration
docker compose logs app | grep "Added task"

# Check logs for job execution
docker compose logs app | grep "portfolio_snapshot\|hyperliquid_sync"
```

**Common Causes**:
1. Scheduler not started (check startup logs)
2. Job registration failed silently
3. Job execution raising exception (check error logs)

**Solution**:
- Restart application
- Check `main.py` lifespan for job registration
- Review error logs with `exc_info=True`

---

### High API Call Rate

**Symptoms**:
- Frequent `429 Too Many Requests` errors
- Orders delayed by 2-5 minutes

**Diagnosis**:
```bash
# Count API errors in last hour
docker compose logs app | grep "429" | wc -l

# Check job execution frequency
docker compose logs app | grep "AI Trading Cycle Started" | tail -20
```

**Common Causes**:
1. Multiple jobs executing simultaneously
2. Technical analysis processing too many symbols
3. Burst of retries after rate limit

**Solution**:
- Increase intervals: `AI_DECISION_INTERVAL=240` (4 min instead of 3)
- Reduce symbol count in technical analysis
- Add delays between API calls (already implemented)

---

### Job Execution Delays

**Symptoms**:
- Job should run every Xs but shows gaps in logs
- Long execution times

**Diagnosis**:
```bash
# Check execution timestamps
docker compose logs app | grep "AI Trading Cycle" | tail -50

# Check for blocking operations
docker compose logs app | grep "Calculating technical analysis"
```

**Common Causes**:
1. Technical analysis taking >90 seconds (normal)
2. Previous job still running (APScheduler queues next execution)
3. Database lock contention (rare with async)

**Solution**:
- Optimize technical analysis (reduce symbols analyzed)
- Increase job intervals to prevent overlap
- Use APScheduler's `max_instances=1` (already configured)

---

## Monitoring Jobs

### Health Check Endpoints

```bash
# Overall scheduler status
curl http://localhost:5611/api/config/scheduler-status

# System health (includes sync status)
curl http://localhost:5611/api/health

# AI usage (includes cost tracking)
curl http://localhost:5611/api/ai/usage
```

### Log Monitoring

**Key log patterns to watch**:
```bash
# Job registrations (startup)
grep "Added task\|Added.*job.*to scheduler" logs

# Job executions
grep "AI Trading Cycle\|Calculated counterfactuals\|portfolio snapshot" logs

# Errors
grep "ERROR.*scheduler\|ERROR.*auto_trader" logs

# Rate limiting
grep "429\|RateLimitExceeded" logs
```

---

## Related Documentation

- **[SYSTEM_ORCHESTRATION.md](../architecture/SYSTEM_ORCHESTRATION.md)** - Complete operational flow
- **[MONITORING.md](MONITORING.md)** - Debug workflows and troubleshooting
- **[OVERVIEW.md](../architecture/OVERVIEW.md)** - System architecture

---

**Last Updated**: 2025-11-10
**Optimization Status**: ✅ Current (after 2025-11-09 -40% reduction)
