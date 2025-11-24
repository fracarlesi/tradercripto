# WebSocket Deployment Guide

**Date**: 2025-11-14
**Target**: Production VPS (46.224.45.196)

---

## 🎯 Pre-Deployment Checklist

### 1. Local Testing (MANDATORY)

Before deploying to production, test locally:

```bash
# Terminal 1: Start backend with WebSocket
cd backend/
python main.py

# Terminal 2: Run test script
python scripts/testing/test_websocket_momentum.py
```

**Expected output**:
```
✅ WebSocket connected and cache populated with 220 symbols
✅ Momentum calculation completed in 0.47s
✅ ALL TESTS PASSED
```

### 2. Verify Dependencies

Check `pyproject.toml` has `websockets>=12.0`:
```bash
grep websockets backend/pyproject.toml
```

### 3. Code Review

Files to review before deployment:
- `backend/services/market_data/websocket_candle_service.py` (new)
- `backend/services/market_data/hourly_momentum.py` (modified)
- `backend/services/startup.py` (modified)

---

## 🚀 Deployment Steps

### Step 1: Stop Production System

```bash
ssh root@46.224.45.196 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml stop'
```

Verify it stopped:
```bash
ssh root@46.224.45.196 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml ps'
```

### Step 2: Deploy New Code

From local machine:
```bash
./deploy_to_hetzner.sh 46.224.45.196
```

This script:
1. Copies code to VPS
2. Builds Docker image
3. Restarts container
4. Shows logs

### Step 3: Monitor Startup Logs

Watch for WebSocket initialization:
```bash
ssh root@46.224.45.196 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml logs -f app' | grep -E "(WebSocket|Subscribed to|Cache:)"
```

**Expected logs**:
```
WebSocket candle service initializing in background...
✅ WebSocket connected to wss://api.hyperliquid.xyz/ws
✅ Subscribed to 220 symbols
Cache: 220 symbols, 5280 candles, 1.05 MB
```

### Step 4: Verify Cache Population

Wait 1-2 minutes for cache to populate, then check:

```bash
ssh root@46.224.45.196 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml exec -T app python3 -c "
from services.market_data.websocket_candle_service import get_websocket_candle_service
ws = get_websocket_candle_service()
stats = ws.get_cache_stats()
print(f\"Connected: {stats[\"connected\"]}\")
print(f\"Symbols cached: {stats[\"symbols_cached\"]}\")
print(f\"Total candles: {stats[\"total_candles\"]}\")
print(f\"Memory: {stats[\"memory_mb\"]} MB\")
"'
```

**Expected output**:
```
Connected: True
Symbols cached: 220
Total candles: 5280
Memory: 1.05 MB
```

### Step 5: Verify Momentum Calculation

Check first AI trading cycle logs:
```bash
ssh root@46.224.45.196 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml logs -f app' | grep -E "(Calculating hourly momentum|Analyzed.*from cache)"
```

**Expected logs**:
```
🚀 Calculating hourly momentum from WebSocket cache
Cache: 220 symbols, 5280 candles, 1.05 MB
✅ Analyzed 180/220 coins in 0.5s (from cache)
📊 Top 20 performers by momentum:
  1. POPCAT: +7.88% (vol: $1,160,000, score: 8.12)
```

**✅ SUCCESS INDICATORS**:
- Duration: ~0.5s (NOT 15-30s)
- Message says "from cache" (NOT "fetching from API")
- No rate limiting warnings

### Step 6: Monitor for Errors

Watch for 1 hour for any issues:
```bash
ssh root@46.224.45.196 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml logs -f app' | grep -i -E "(error|warning|exception|websocket|disconnect)"
```

**Common warnings (SAFE)**:
- `Missing cache data for 40/220 coins (WebSocket warming up?)` - Normal during first 1-2 hours
- `WebSocket not connected - cache may be stale` - Temporary during reconnection

**CRITICAL errors (STOP IMMEDIATELY)**:
- `Failed to connect to WebSocket` (repeatedly)
- `No symbols in WebSocket cache - is service running?`
- `RuntimeError: Event loop closed`
- Continuous reconnection loops

---

## 🐛 Troubleshooting

### Issue: WebSocket not connecting

**Symptoms**:
```
Connecting to Hyperliquid WebSocket...
WebSocket error: ConnectionRefused
```

**Solution**:
1. Check VPS network connectivity:
   ```bash
   ssh root@46.224.45.196 'ping -c 3 api.hyperliquid.xyz'
   ```

2. Check firewall:
   ```bash
   ssh root@46.224.45.196 'iptables -L -n | grep 443'
   ```

3. Verify WebSocket URL is correct (mainnet vs testnet)

---

### Issue: Cache not populating

**Symptoms**:
```
Cache: 0 symbols, 0 candles, 0.0 MB
Missing cache data for 220/220 coins
```

**Solution**:
1. Check WebSocket subscription logs:
   ```bash
   docker compose -f docker-compose.simple.yml logs app | grep -i "subscribed"
   ```

2. Verify Hyperliquid API is reachable:
   ```bash
   curl https://api.hyperliquid.xyz/info -X POST -H "Content-Type: application/json" -d '{"type":"meta"}'
   ```

3. Check for message handling errors:
   ```bash
   docker compose -f docker-compose.simple.yml logs app | grep "Failed to handle WebSocket message"
   ```

---

### Issue: Momentum calculation still slow (15s+)

**Symptoms**:
```
✅ Analyzed 180/220 coins in 17.3s
```

**Root Cause**: Still using HTTP polling instead of WebSocket cache

**Solution**:
1. Check import in `hourly_momentum.py`:
   ```bash
   ssh root@46.224.45.196 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml exec app grep -n "from services.market_data.websocket_candle_service" backend/services/market_data/hourly_momentum.py'
   ```

2. Verify it's NOT importing `hyperliquid.info`:
   ```bash
   ssh root@46.224.45.196 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml exec app grep -n "from hyperliquid.info import Info" backend/services/market_data/hourly_momentum.py'
   ```
   - Should return NOTHING (import removed)

---

### Issue: Memory leak (cache growing indefinitely)

**Symptoms**:
```
Memory: 150.32 MB  (expected: ~1 MB)
```

**Root Cause**: Deque maxlen not working

**Solution**:
1. Check cache initialization in `websocket_candle_service.py`:
   ```python
   self.candle_cache: Dict[str, deque] = defaultdict(lambda: deque(maxlen=self.max_candles))
   ```

2. Restart container:
   ```bash
   ssh root@46.224.45.196 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml restart app'
   ```

---

## 🔄 Rollback Procedure

If WebSocket causes critical issues:

### Step 1: Revert Code

```bash
git revert HEAD  # Revert WebSocket changes
git push
```

### Step 2: Redeploy Old Version

```bash
./deploy_to_hetzner.sh 46.224.45.196
```

### Step 3: Fix Rate Limiting (Temporary)

Edit `backend/services/market_data/hourly_momentum.py`:
```python
# Increase delay from 0.1s to 1.2s
await asyncio.sleep(1.2)  # Was: 0.1

# Reduce symbols from 220 to 100
all_coins = all_coins[:100]  # Limit analysis
```

**See**: `backend/docs/RATE_LIMIT_ANALYSIS_HOURLY_MOMENTUM.md` for full fix details.

---

## 📊 Success Metrics (Monitor for 24h)

After deployment, verify these metrics:

| Metric | Target | How to Check |
|--------|--------|--------------|
| **WebSocket uptime** | >99% | Check disconnect count in logs |
| **Cache population** | 220 symbols | `ws.get_cache_stats()` |
| **Momentum calc time** | <1s | Check "Analyzed X coins in Ys" logs |
| **API rate limit usage** | 0 weight | No 429 errors in logs |
| **Trading frequency** | Every 3min | Count AI trading cycles/hour |
| **Memory usage** | <5 MB cache | Check cache stats |

### Monitoring Commands

```bash
# Check trading activity (should be every ~3 min)
ssh root@46.224.45.196 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml logs app' | grep "AI trading" | tail -20

# Count 429 errors (should be 0)
ssh root@46.224.45.196 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml logs app' | grep -c "429"

# Check WebSocket reconnections (should be rare)
ssh root@46.224.45.196 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml logs app' | grep -c "Reconnecting"

# Monitor memory usage
ssh root@46.224.45.196 'docker stats --no-stream trader_bitcoin-app-1'
```

---

## ✅ Deployment Complete Checklist

- [ ] Local tests passed (`test_websocket_momentum.py`)
- [ ] Production system stopped
- [ ] Code deployed to VPS
- [ ] WebSocket connected and subscribed to 220 symbols
- [ ] Cache populated (220 symbols, 5000+ candles)
- [ ] Momentum calculation <1s "from cache"
- [ ] No 429 rate limit errors
- [ ] Trading cycles executing every ~3 minutes
- [ ] No critical errors in logs
- [ ] Monitored for 1 hour without issues
- [ ] Performance metrics documented
