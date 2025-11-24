# Orchestrator Migration Guide - JSON-Based AI Trading System

**Status**: Ready for deployment
**Date**: 2025-11-10
**Version**: 2.0.0 (New Architecture)

---

## 📋 Executive Summary

This guide documents the migration from **narrative prompts** to **structured JSON** for AI trading decisions.

### What Changed

**OLD SYSTEM** (v1.x):
- AI analyzed only **top 5 symbols** selected by backend
- Narrative text prompts (~5000 tokens)
- 96.5% of computed data was **wasted**
- Limited learning capability

**NEW SYSTEM** (v2.0):
- AI analyzes **ALL 142 symbols** with complete data
- Structured JSON format (~15000 tokens)
- **100% data utilization**
- Enables feedback loop for indicator weight learning

### Benefits

1. **Complete Market Coverage**: DeepSeek sees all 142 symbols, not just top 5
2. **Better Decisions**: Full data enables more informed choices
3. **Learning System**: Structured format enables future reinforcement learning
4. **Maintainability**: Clean separation between data aggregation (orchestrator) and AI decision (DeepSeek client)
5. **Performance**: Intelligent caching maintains ~90s cycle time

### Cost Impact

- **Old**: ~1500 tokens/cycle × $0.14/M = $0.00021 per decision
- **New**: ~15000 tokens/cycle × $0.14/M = $0.0021 per decision
- **Increase**: 10x token usage, but **complete data coverage** justifies cost
- **Monthly**: ~$45/month at 3-minute intervals (was ~$4.50/month)

---

## 🏗️ Architecture Overview

### New Components

```
┌──────────────────────────────────────────────────────────────┐
│                    AUTO TRADER (Entry Point)                  │
│  (backend/services/auto_trader.py)                           │
└────────────────┬─────────────────────────────────────────────┘
                 │
                 ▼
┌──────────────────────────────────────────────────────────────┐
│              ORCHESTRATOR (Data Aggregation)                  │
│  (backend/services/orchestrator/market_data_orchestrator.py) │
│                                                               │
│  STAGE 1: Fetch prices (all_mids) - SEQUENTIAL              │
│  STAGE 2: Per-symbol analyses - PARALLEL                     │
│    ├─ Technical Analysis (142 symbols)                       │
│    ├─ Pivot Points (142 symbols, cached 1h)                  │
│    └─ Prophet Forecasts (142 symbols, cached 24h, LITE)      │
│  STAGE 3: Global indicators - PARALLEL                       │
│    ├─ Sentiment (Fear & Greed)                               │
│    ├─ Whale Alerts (last 10 minutes)                         │
│    └─ News (CoinJournal)                                      │
│  STAGE 4: Build JSON (validation)                            │
└────────────────┬─────────────────────────────────────────────┘
                 │
                 ▼ (MarketDataSnapshot JSON)
┌──────────────────────────────────────────────────────────────┐
│              DEEPSEEK CLIENT (AI Decision)                    │
│  (backend/services/ai/deepseek_client.py)                   │
│                                                               │
│  1. Format JSON for prompt                                    │
│  2. Call DeepSeek API                                         │
│  3. Parse decision JSON                                       │
│  4. Validate decision                                         │
└────────────────┬─────────────────────────────────────────────┘
                 │
                 ▼ (Trading Decision)
┌──────────────────────────────────────────────────────────────┐
│              TRADING EXECUTION (Hyperliquid)                  │
│  (backend/services/trading_commands.py)                      │
└──────────────────────────────────────────────────────────────┘
```

### File Structure

**New Files** (created):
```
backend/
├── services/
│   ├── orchestrator/
│   │   ├── __init__.py
│   │   ├── schemas.py                  # TypedDict definitions
│   │   ├── json_builder.py             # MarketDataBuilder
│   │   ├── cache_manager.py            # Unified cache
│   │   └── market_data_orchestrator.py # Main orchestration
│   └── ai/
│       ├── __init__.py
│       └── deepseek_client.py          # New AI client
├── tests/
│   ├── unit/
│   │   └── test_json_builder.py        # Unit tests (8 tests)
│   └── integration/
│       └── test_orchestrator_deepseek_integration.py  # E2E tests
└── scripts/testing/
    └── test_full_orchestrator_pipeline.py  # Manual test script
```

**Modified Files**:
```
backend/services/
├── technical_analysis_service.py       # Added get_technical_analysis_structured()
├── market_data/
│   ├── pivot_calculator.py             # Added calculate_pivot_points_batch()
│   └── prophet_forecaster.py           # Added calculate_prophet_forecasts_batch()
```

**Deprecated** (NOT deleted yet - keep for rollback):
```
backend/services/
└── ai_decision_service.py              # OLD: call_ai_for_decision()
```

---

## 🧪 Testing Procedures

### 1. Unit Tests (JSON Builder)

**Run**:
```bash
cd backend
pytest tests/unit/test_json_builder.py -v
```

**Expected**: 8/8 tests pass
- Basic snapshot building
- Missing data validation
- Schema validation
- Method chaining

### 2. Integration Tests (E2E Pipeline)

**Run**:
```bash
cd backend
pytest tests/integration/test_orchestrator_deepseek_integration.py -v -s
```

**Expected**: 6/6 tests pass
- Complete pipeline with Prophet
- Pipeline without Prophet
- Snapshot structure completeness
- Portfolio constraint validation
- Performance metrics
- Error handling

### 3. Manual Test (Orchestrator Only - No AI Tokens)

**Run**:
```bash
cd backend
python scripts/testing/test_full_orchestrator_pipeline.py --skip-deepseek --skip-prophet
```

**Expected**:
- Duration: ~60-90 seconds (cached)
- Symbols analyzed: 142
- Top 5 signals displayed
- Portfolio summary shown
- No errors in logs

### 4. Manual Test (Full Pipeline with DeepSeek)

**Run**:
```bash
cd backend
python scripts/testing/test_full_orchestrator_pipeline.py --account-id 1
```

**Expected**:
- Duration: ~90-120 seconds
- Valid trading decision returned
- Decision references indicators (technical, pivot, prophet, etc.)
- No API errors
- Cost: ~$0.002 per test

### 5. Performance Benchmark

**Test**: Run orchestrator 3 times in a row

**Expected**:
- Run 1 (cold cache): 3-5 minutes
- Run 2 (warm cache): ~90 seconds
- Run 3 (hot cache): ~60 seconds

**Verify**: Cache hit rate increases (check logs)

---

## 🚀 Deployment Steps

### Phase 1: Pre-Deployment Testing (Local)

**1. Run all unit tests**:
```bash
cd backend
pytest tests/unit/test_json_builder.py -v
```

**2. Test orchestrator data fetching (no AI)**:
```bash
python scripts/testing/test_full_orchestrator_pipeline.py --skip-deepseek
```
- Verify 142 symbols analyzed
- Check all indicators present
- Confirm no Hyperliquid API errors

**3. Test full pipeline with DeepSeek (1 test)**:
```bash
python scripts/testing/test_full_orchestrator_pipeline.py --account-id 1
```
- Verify valid decision returned
- Check reasoning quality
- Monitor cost (~$0.002)

**4. Check Prophet LITE mode performance**:
```bash
# First run (cold cache)
time python scripts/testing/test_full_orchestrator_pipeline.py --skip-deepseek

# Second run (warm cache)
time python scripts/testing/test_full_orchestrator_pipeline.py --skip-deepseek
```
- Expected: 50-70% speedup on second run

### Phase 2: Code Integration

**1. Update auto_trader.py to use new system**:

**OLD CODE** (`backend/services/auto_trader.py`):
```python
from services.ai_decision_service import call_ai_for_decision

# Inside run_auto_trading_for_account()
decision = await call_ai_for_decision(account, portfolio, prices)
```

**NEW CODE**:
```python
from services.orchestrator import build_market_data_snapshot
from services.ai import get_trading_decision_from_snapshot

# Inside run_auto_trading_for_account()
# Build complete market data snapshot
snapshot = await build_market_data_snapshot(
    account_id=account.id,
    enable_prophet=True,
    prophet_mode="lite",  # 7 days training (fast)
)

# Get AI decision
decision = await get_trading_decision_from_snapshot(account, snapshot)
```

**2. Preserve old system for rollback**:
```python
# Keep old import commented
# from services.ai_decision_service import call_ai_for_decision
```

### Phase 3: Local Testing with Auto Trader

**1. Run auto trader manually (1 cycle)**:
```bash
cd backend
python -c "
import asyncio
from services.auto_trader import run_auto_trading_cycle
asyncio.run(run_auto_trading_cycle())
"
```

**2. Monitor logs**:
```bash
tail -f logs/auto_trader.log | grep -E "(ORCHESTRATION|DeepSeek|Trading decision)"
```

**3. Verify**:
- ✅ Orchestration completes (~90s)
- ✅ DeepSeek returns valid decision
- ✅ Decision execution works (or HOLD)
- ✅ No errors in logs
- ✅ Cost reasonable (~$0.002/cycle)

### Phase 4: Production Deployment

**1. Stop production auto trading**:
```bash
ssh root@46.224.45.196 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml stop'
```

**2. Backup database**:
```bash
ssh root@46.224.45.196 'cd /opt/trader_bitcoin && cp backend/data.db backend/data.db.backup.$(date +%Y%m%d_%H%M%S)'
```

**3. Deploy new code**:
```bash
./deploy_to_hetzner.sh 46.224.45.196
```

**4. Monitor first cycle**:
```bash
ssh root@46.224.45.196 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml logs -f app' | grep -E "(ORCHESTRATION|DeepSeek|Trading decision)"
```

**5. Verify success indicators**:
- ✅ "ORCHESTRATION COMPLETE" in logs
- ✅ "DeepSeek decision:" with valid operation
- ✅ Cycle time ~90-120 seconds (after warmup)
- ✅ No Python exceptions
- ✅ Cache hit rate increasing

### Phase 5: Monitoring (First 24 Hours)

**Monitor every 30 minutes**:

**1. Check auto trader logs**:
```bash
ssh root@46.224.45.196 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml logs --tail=100 app' | grep -E "(ERROR|WARNING|Trading decision)"
```

**2. Check for errors**:
```bash
ssh root@46.224.45.196 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml logs app' | grep -c "ERROR"
```
- Expected: 0 errors
- If >5 errors: Investigate immediately

**3. Verify decisions are being made**:
```bash
ssh root@46.224.45.196 'docker exec -it trader_bitcoin-app-1 python3 -c "
import sqlite3
conn = sqlite3.connect(\"/app/data/data.db\")
cursor = conn.cursor()
cursor.execute(\"SELECT operation, symbol, reason FROM ai_decision_logs ORDER BY decision_time DESC LIMIT 5\")
for row in cursor.fetchall():
    print(f\"{row[0]} {row[1]}: {row[2][:50]}...\")
conn.close()
"'
```

**4. Check cost**:
- Expected: ~$0.002 × 20 cycles/hour × 24 hours = ~$0.96/day
- Monitor: Check DeepSeek dashboard for API usage

**5. Performance metrics**:
```bash
# Check cycle times
ssh root@46.224.45.196 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml logs app' | grep "ORCHESTRATION COMPLETE" | tail -10
```
- Expected: ~90-120 seconds per cycle (after warmup)
- First cycle: May be 3-5 minutes (cold cache)

---

## 🔄 Rollback Procedure

**If deployment fails**, rollback to old system:

### Step 1: Revert Code Changes

**Edit** `backend/services/auto_trader.py`:

**REVERT TO**:
```python
from services.ai_decision_service import call_ai_for_decision

# Inside run_auto_trading_for_account()
decision = await call_ai_for_decision(account, portfolio, prices)
```

**COMMENT OUT**:
```python
# from services.orchestrator import build_market_data_snapshot
# from services.ai import get_trading_decision_from_snapshot
```

### Step 2: Redeploy

```bash
./deploy_to_hetzner.sh 46.224.45.196
```

### Step 3: Verify Old System Works

```bash
ssh root@46.224.45.196 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml logs -f app' | grep "AI decision for"
```

Expected: Old prompt-based decisions resume

### Step 4: Restore Database (If Needed)

**ONLY IF** data corruption occurred:
```bash
ssh root@46.224.45.196 'cd /opt/trader_bitcoin && cp backend/data.db.backup.* backend/data.db'
```

---

## 📊 Success Metrics

### Key Performance Indicators (KPIs)

**1. System Stability**:
- ✅ Target: <5 errors per day
- ✅ Target: 100% auto trader cycle completion rate
- ⚠️ Alert: >10 errors per day → Investigate

**2. Performance**:
- ✅ Target: ~90 seconds per cycle (cached)
- ✅ Target: Cache hit rate >70% after 1 hour
- ⚠️ Alert: >180 seconds per cycle → Check API rate limits

**3. Cost**:
- ✅ Target: ~$45/month ($1.50/day)
- ✅ Target: ~$0.002 per decision
- ⚠️ Alert: >$3/day → Check for token leaks

**4. Decision Quality**:
- ✅ Target: Decisions reference multiple indicators (technical, pivot, prophet)
- ✅ Target: "analysis" field populated with confidence + alternatives
- ⚠️ Alert: Generic "HOLD" for >12 hours → Check data quality

### Monitoring Commands

**Daily health check script**:
```bash
#!/bin/bash
# health_check.sh

echo "=== ORCHESTRATOR HEALTH CHECK ==="
echo ""

# 1. Error count (last 24h)
echo "Errors (last 24h):"
ssh root@46.224.45.196 'docker logs trader_bitcoin-app-1 --since 24h' | grep -c "ERROR"

# 2. Last 5 decisions
echo ""
echo "Last 5 AI decisions:"
ssh root@46.224.45.196 'docker exec trader_bitcoin-app-1 python3 -c "
import sqlite3
conn = sqlite3.connect(\"/app/data/data.db\")
cursor = conn.cursor()
cursor.execute(\"SELECT datetime(decision_time), operation, symbol FROM ai_decision_logs ORDER BY decision_time DESC LIMIT 5\")
for row in cursor.fetchall():
    print(f\"{row[0]}: {row[1]} {row[2]}\")
conn.close()
"'

# 3. Cycle times (last 10)
echo ""
echo "Cycle times (last 10):"
ssh root@46.224.45.196 'docker logs trader_bitcoin-app-1 --tail=500' | grep "ORCHESTRATION COMPLETE" | tail -10

# 4. Cache performance
echo ""
echo "Cache stats:"
ssh root@46.224.45.196 'docker logs trader_bitcoin-app-1 --tail=100' | grep "Cache stats" | tail -1

echo ""
echo "=== END HEALTH CHECK ==="
```

---

## 🐛 Troubleshooting

### Issue: "Orchestration timeout (>300s)"

**Symptoms**:
- Logs show "ORCHESTRATION FAILED after 300s"
- Cycle never completes

**Diagnosis**:
```bash
# Check which stage is hanging
ssh root@46.224.45.196 'docker logs trader_bitcoin-app-1 --tail=200' | grep -E "(STAGE [0-9]|Fetching|Calculating)"
```

**Likely causes**:
1. **Prophet hanging**: Disable Prophet temporarily
   ```python
   snapshot = await build_market_data_snapshot(account_id=1, enable_prophet=False)
   ```

2. **Hyperliquid API slow**: Check API status at https://status.hyperliquid.xyz

3. **Rate limiting**: Reduce MAX_WORKERS in technical_analysis_service.py

**Fix**: Add timeout to orchestrator stages

---

### Issue: "DeepSeek returns HOLD for every cycle"

**Symptoms**:
- AI always decides "hold"
- No trades executed for >12 hours

**Diagnosis**:
```bash
# Check if snapshot has valid data
ssh root@46.224.45.196 'docker exec trader_bitcoin-app-1 python3 scripts/testing/test_full_orchestrator_pipeline.py --skip-deepseek'
```

**Likely causes**:
1. **No strong signals**: Technical scores all <0.6
   - Solution: Normal market condition, wait for opportunities

2. **Empty snapshot**: Orchestrator returning empty symbol list
   - Check logs for "ORCHESTRATION FAILED"
   - Fix: Restart orchestrator

3. **DeepSeek prompt issue**: AI not understanding JSON format
   - Check DeepSeek logs for parsing errors
   - Fix: Verify JSON format in prompt

**Fix**: Manually test DeepSeek with snapshot
```bash
python scripts/testing/test_full_orchestrator_pipeline.py --account-id 1
```

---

### Issue: "High API costs (>$5/day)"

**Symptoms**:
- DeepSeek dashboard shows >100k tokens/day
- Monthly projection >$150

**Diagnosis**:
```bash
# Count AI API calls in logs
ssh root@46.224.45.196 'docker logs trader_bitcoin-app-1 --since 24h' | grep -c "DeepSeek API response"
```

**Expected**: ~480 calls/day (20/hour × 24h at 3-minute intervals)

**Likely causes**:
1. **Excessive calls**: Auto trader running too frequently
   - Check scheduler interval in startup.py
   - Should be: 180 seconds (3 minutes)

2. **Large prompts**: JSON too verbose
   - Check average tokens per call
   - Should be: ~15000 input tokens

3. **Failed retries**: API errors causing excessive retries
   - Check for "DeepSeek API rate limited" warnings
   - Increase retry backoff

**Fix**: Adjust auto trader frequency or reduce JSON verbosity

---

### Issue: "Cache hit rate <30%"

**Symptoms**:
- Orchestration slow (>180s per cycle)
- Cache hit rate not improving

**Diagnosis**:
```bash
# Check cache stats in logs
ssh root@46.224.45.196 'docker logs trader_bitcoin-app-1 --tail=500' | grep "Cache stats"
```

**Likely causes**:
1. **Cache expiration too short**: TTL too aggressive
   - Prophet: 24h (correct)
   - Pivot: 1h (correct)
   - Technical: 3min (might be too short for testing)

2. **Cache not persisting**: Redis/memory cache cleared
   - Check if using in-memory cache (default)
   - Memory cache clears on restart

3. **Symbol list changing**: Different symbols each cycle
   - Check if SUPPORTED_SYMBOLS list is stable

**Fix**: Increase cache TTL for technical analysis (testing only)

---

## 📚 Additional Resources

### Documentation

- **Architecture**: `backend/docs/MICROSERVICES_ARCHITECTURE.md`
- **Orchestrator Code**: `backend/services/orchestrator/market_data_orchestrator.py`
- **DeepSeek Client**: `backend/services/ai/deepseek_client.py`
- **JSON Schema**: `backend/services/orchestrator/schemas.py`

### Test Files

- **Unit Tests**: `backend/tests/unit/test_json_builder.py`
- **Integration Tests**: `backend/tests/integration/test_orchestrator_deepseek_integration.py`
- **Manual Test**: `backend/scripts/testing/test_full_orchestrator_pipeline.py`

### Key Log Messages

**Success indicators**:
```
✅ ORCHESTRATION COMPLETE: 90.3s
DeepSeek decision: buy BTC (portion: 0.25)
Cache stats: {"hit_rate": 0.75}
Trading decision executed: BUY BTC $10.00
```

**Warning indicators**:
```
⚠️ Prophet forecast unavailable for {symbol}
⚠️ DeepSeek API rate limited (attempt 2/3)
⚠️ Cache hit rate: 0.15 (expected >0.7)
```

**Error indicators**:
```
❌ ORCHESTRATION FAILED after 120.5s
❌ DeepSeek API error (status 500)
❌ Failed to fetch pivot points
❌ Invalid JSON response from DeepSeek
```

---

## ✅ Pre-Deployment Checklist

- [ ] All unit tests pass (8/8)
- [ ] Integration tests pass (6/6)
- [ ] Manual orchestrator test successful (--skip-deepseek)
- [ ] Manual full pipeline test successful (with DeepSeek)
- [ ] Performance benchmark shows cache working (speedup on second run)
- [ ] Cost estimate acceptable (~$45/month)
- [ ] auto_trader.py updated with new imports
- [ ] Old system preserved for rollback
- [ ] Production backup created
- [ ] Monitoring commands tested
- [ ] Health check script created
- [ ] Team notified of deployment

---

## 🎯 Post-Deployment Tasks (Week 1)

### Day 1-2: Intensive Monitoring

- [ ] Monitor every 30 minutes
- [ ] Check error count (<5/day)
- [ ] Verify cycle times (~90s)
- [ ] Confirm decisions being made
- [ ] Track API cost

### Day 3-7: Daily Monitoring

- [ ] Run health_check.sh daily
- [ ] Review decision quality
- [ ] Analyze cache performance
- [ ] Optimize if needed

### Week 2+: Normal Operations

- [ ] Weekly health checks
- [ ] Monthly cost review
- [ ] Quarterly performance optimization

---

## 📝 Version History

| Version | Date | Changes |
|---------|------|---------|
| 2.0.0 | 2025-11-10 | Initial JSON-based orchestrator release |
| 1.x | 2025-01-01 | Legacy narrative prompt system |

---

## 👥 Support

For issues or questions:
1. Check troubleshooting section above
2. Review logs using monitoring commands
3. Test with manual scripts
4. Rollback if critical failure

**Deployment Champion**: Claude Code (AI Assistant)
**Reviewer**: User (francescocarlesi)
