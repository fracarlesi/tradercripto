# Testing Guide - Market Data Orchestrator

Guida completa per testare e verificare il nuovo sistema JSON-based prima del deployment in produzione.

## 📋 Test Overview

Abbiamo 3 test da eseguire in sequenza:

1. **Quick Cache Verification (3 minuti)** - Verifica che i cache si aggiornino correttamente
2. **10-Minute Orchestrator Test (10-15 minuti)** - Verifica che tutto il sistema funzioni per più cicli
3. **Rate Limit Monitoring (continuo)** - Monitora l'uso delle API per evitare 429 errors

## ✅ Test 1: Cache Refresh Verification (3 minuti)

**Obiettivo**: Verificare che i dati cached si aggiornino secondo i TTL previsti:
- Technical Analysis: 180s (3 min)
- Pivot Points: 3600s (1 ora)
- Prophet Forecasts: 86400s (24 ore)

### Quick Mode (3 minuti) - Testa Technical Analysis

```bash
cd backend/
python3 scripts/testing/verify_cache_refresh.py --mode quick
```

**Cosa fa:**
1. Esegue orchestrator (ciclo 1 - cache fredda)
2. Ri-esegue subito (ciclo 2 - dovrebbe usare cache)
3. Aspetta 3 minuti e 20 secondi (oltre il TTL di 180s)
4. Ri-esegue (ciclo 3 - dovrebbe re-fetchare i dati)
5. Verifica che l'età del cache sia diminuita (= refresh avvenuto)

**Output atteso:**
```
╔══════════════════════════════════════════════════════════════╗
║                CACHE REFRESH VERIFICATION TEST                ║
╚══════════════════════════════════════════════════════════════╝

CYCLE 1: Initial Run (Cold Cache)
Cache Stats:
  Hits: 0
  Misses: 142
  Hit Rate: 0.0%

CYCLE 2: Immediate Run (Should Use Cache)
Cache Stats:
  Hits: 142 (+142)
  Misses: 142 (+0)
  Hit Rate: 50.0%

✅ Cache Used         PASS        Hits: 0 → 142

WAITING 200s for Cache Expiration...
  Progress:  50.0% | Elapsed:   100s | Remaining:   100s

CYCLE 3: After TTL Expiration (Should Re-fetch)
Cache Ages:
  technical_analysis       : 5.2s    ← Dovrebbe essere vicino a 0!
  pivot_points            : 205.8s
  prophet_forecasts       : 205.8s

VERIFICATION RESULTS
✅ technical_analysis    PASS        Age: 205s → 5s (refreshed)
⏭️ pivot_points          SKIP        Not tested in quick mode
⏭️ prophet_forecasts     SKIP        Not tested in quick mode

FINAL VERDICT
✅ ALL TESTS PASSED - Cache refresh is working correctly!
```

### Medium Mode (1 ora) - Testa Technical + Pivot (OPTIONAL)

```bash
python3 scripts/testing/verify_cache_refresh.py --mode medium
```

Aspetta 1 ora e 1 minuto per verificare anche Pivot Points refresh.

### Full Mode (24 ore) - Testa TUTTO (NON NECESSARIO per ora)

```bash
python3 scripts/testing/verify_cache_refresh.py --mode full
```

**⚠️ NOTA**: Non serve eseguire il test completo ora. Il quick mode è sufficiente per verificare che la logica funzioni.

---

## ✅ Test 2: 10-Minute Orchestrator Test

**Obiettivo**: Verificare che l'orchestrator funzioni correttamente per più cicli consecutivi, mostrando:
- JSON generation completa
- Top 5 technical signals
- Portfolio state
- Global indicators (sentiment, whale, news)
- Performance metrics (durata, cache hit rate)

### Modalità A: Senza Prophet (VELOCE - raccomandato per primo test)

```bash
cd backend/
python3 scripts/testing/test_orchestrator_10min.py --cycles 3
```

**Durata**: ~3-5 minuti per 3 cicli (senza Prophet)

**Output atteso** (esempio ciclo 1):
```
╔══════════════════════════════════════════════════════════════╗
║             ORCHESTRATOR 10-MINUTE PRODUCTION TEST            ║
╚══════════════════════════════════════════════════════════════╝
Configuration:
  Account ID: 1
  Cycles: 3
  Prophet: DISABLED
  Save JSON: No
  Show Full JSON: No
  Estimated duration: 4-7 minutes

════════════════════════════════════════════════════════════════
  CYCLE #1 - 2025-11-11 14:30:15
════════════════════════════════════════════════════════════════

────────────────────────────────────────────────────────────────
  CYCLE SUMMARY
────────────────────────────────────────────────────────────────
✅ Status: SUCCESS
⏱️  Duration: 45.3 seconds
📊 Symbols analyzed: 142
💾 Cache hit rate: 0.0%
🔮 Prophet enabled: No

────────────────────────────────────────────────────────────────
  TOP 5 TECHNICAL SIGNALS
────────────────────────────────────────────────────────────────
  1. BTC    $101,234.50 - Score: 0.782 (STRONG_LONG  ) - Pivot: above_r1
  2. ETH    $  3,456.78 - Score: 0.654 (LONG         ) - Pivot: between
  3. SOL    $    234.56 - Score: 0.612 (LONG         ) - Pivot: above_pp
  4. AVAX   $     45.67 - Score: 0.587 (LONG         ) - Pivot: between
  5. MATIC  $      0.89 - Score: 0.523 (NEUTRAL      ) - Pivot: between

────────────────────────────────────────────────────────────────
  PORTFOLIO STATE
────────────────────────────────────────────────────────────────
  Total Assets:    $ 10,234.50
  Available Cash:  $  8,456.78
  Positions Value: $  1,777.72
  Unrealized P&L:  $    123.45
  Active Positions: 2

  Positions:
    • BTC    LONG : $  1,234.56 (P&L:  +12.34%)
    • ETH    LONG : $    543.16 (P&L:   -2.15%)

────────────────────────────────────────────────────────────────
  GLOBAL INDICATORS
────────────────────────────────────────────────────────────────
  Sentiment: Fear            (42/100)
  Signal:    neutral
  Whale Alerts: 3 recent transactions
  News Headlines: 5 articles

  Latest Headlines:
    1. Bitcoin reaches new all-time high as institutional demand surges...
    2. Ethereum network upgrade scheduled for next week...
    3. SEC approves spot Bitcoin ETF applications...

────────────────────────────────────────────────────────────────
  JSON STRUCTURE (Sample)
────────────────────────────────────────────────────────────────
{
  "metadata": {
    "timestamp": "2025-11-11T14:30:15Z",
    "symbols_analyzed": 142,
    "cache_hit_rate": 0.0,
    "generation_time_seconds": 45.3
  },
  "symbols": [
    {
      "symbol": "BTC",
      "price": 101234.5,
      "technical_analysis": {
        "score": 0.782,
        "signal": "STRONG_LONG",
        "rsi": 67.5,
        "macd_histogram": 234.56,
        "ema_trend": "bullish"
      },
      "pivot_points": {
        "current_zone": "above_r1",
        "support_1": 100000.0,
        "resistance_1": 102000.0
      },
      "prophet_forecast": null
    }
  ],
  "global_indicators": {
    "sentiment": {
      "value": 42,
      "label": "Fear",
      "signal": "neutral"
    },
    "whale_alerts": [...],
    "news": [...]
  },
  "portfolio": {
    "total_assets": 10234.5,
    "available_cash": 8456.78,
    "positions": [...]
  }
}

  ... (showing 2/142 symbols)

⏳ Waiting 30s before next cycle...
```

**Cosa verificare**:
- ✅ Status: SUCCESS per tutti i cicli
- ✅ Duration: <90s al primo ciclo (cold cache), <60s ai successivi (cache hit)
- ✅ Cache hit rate: 0% al primo ciclo, >50% ai successivi
- ✅ Top symbols hanno score, signal, pivot_points
- ✅ Portfolio mostra balance reale (NON $0!)
- ✅ Global indicators hanno sentiment, news, whale alerts

### Modalità B: Con Prophet (PIÙ LENTO ma completo)

```bash
python3 scripts/testing/test_orchestrator_10min.py --cycles 3 --enable-prophet
```

**Durata**: ~6-10 minuti per 3 cicli (con Prophet LITE mode)

**Differenze**:
- Ogni simbolo avrà anche `prophet_forecast` con trend prediction
- Primo ciclo più lento (~90-120s invece di 45s)
- Cache hit rate ancora più alto ai cicli successivi

### Opzioni Aggiuntive

```bash
# Salva JSON completo su file (per debug)
python3 scripts/testing/test_orchestrator_10min.py --cycles 3 --save-json

# Mostra JSON completo a schermo (WARNING: molto lungo!)
python3 scripts/testing/test_orchestrator_10min.py --cycles 3 --show-full-json

# Test più lungo (5 cicli con Prophet)
python3 scripts/testing/test_orchestrator_10min.py --cycles 5 --enable-prophet
```

**File salvati** (se --save-json):
```
backend/output/market_snapshot_20251111_143015.json
backend/output/market_snapshot_20251111_143245.json
backend/output/market_snapshot_20251111_143515.json
```

---

## ✅ Test 3: Rate Limit Monitoring

**Obiettivo**: Verificare che nessuna API superi i rate limits documentati.

### Modalità Locale (durante i test)

```bash
./monitor_rate_limits.sh
```

**Output atteso:**
```
╔══════════════════════════════════════════════════════════════╗
║        API RATE LIMIT MONITORING - 2025-11-11                 ║
╚══════════════════════════════════════════════════════════════╝

Mode: local
Time window: Last 24 hours

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1️⃣  HYPERLIQUID API (Trading Platform)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   429 Rate Limit Errors: 0 ✅ (Expected: 0)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
2️⃣  DEEPSEEK API (AI Decision Engine)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   Rate Limit Warnings: 0 ✅ (Expected: 0)
   API Calls (24h): 480 ✅ (Expected: ~480)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
3️⃣  COINMARKETCAP API (Sentiment Index)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   API Calls (24h): 24 ✅ (Expected: ~24, Limit: 333/day)
   Limit Usage: 7% of 333 calls/day

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
4️⃣  WHALE ALERT API (Large Transactions)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   API Calls (24h): 35 ✅ (Expected: <50, Monthly limit: 1000)
   Projected Monthly: 1050 calls (Limit: 1,000)
   ⚠️  WARNING: Will exceed monthly limit!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
5️⃣  NEWS API (CoinJournal)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   API Calls (24h): 24 ✅ (Expected: ~24)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 SUMMARY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ ALL CLEAR - No rate limit issues detected

Last updated: 2025-11-11 14:35:42

For detailed analysis, see:
  backend/docs/API_RATE_LIMITS_ANALYSIS.md
```

**Cosa verificare**:
- ✅ Hyperliquid 429 errors: 0 (CRITICAL)
- ✅ DeepSeek calls: ~480/day (20/hour = 1 ogni 3 min)
- ✅ CoinMarketCap calls: ~24/day (1/hour) - dopo il fix!
- ⚠️ Whale Alert: <35/day (projected 1050/month - needs optimization)

### Modalità Produzione (su VPS)

```bash
./monitor_rate_limits.sh production
```

Monitora i logs su VPS remoto (46.224.45.196).

---

## 📊 Test Results Summary

Dopo aver eseguito tutti i test, dovresti avere:

### ✅ Cache Verification Results
```
Test Mode: quick (3 min)
Technical Analysis cache: ✅ PASS (refreshed dopo 180s)
```

### ✅ Orchestrator Test Results
```
Total Cycles: 3
Successful: 3 ✅
Failed: 0
Total Duration: 180.5s (3.0 minutes)

Cycle Performance:
  Average: 60.2s
  Fastest: 45.3s (cycle 1 - cold cache)
  Slowest: 67.8s (cycle 3)
  Speedup (first → last): 1.5x

✅ ALL TESTS PASSED
```

### ✅ Rate Limit Check
```
Hyperliquid: 0 errors ✅
DeepSeek: 480 calls/day ✅
CoinMarketCap: 24 calls/day ✅ (7% of limit)
Whale Alert: 35 calls/day ⚠️ (needs monitoring)
News API: 24 calls/day ✅
```

---

## 🚀 Next Steps After Testing

Se tutti i test passano:

1. **✅ Cache refresh verificato** - I dati si aggiornano correttamente secondo TTL
2. **✅ Orchestrator funzionante** - JSON generation completa ogni 3 minuti
3. **✅ Rate limits sotto controllo** - Nessun rischio di 429 errors

**Pronto per deployment in produzione!**

Vedi `DEPLOYMENT_ROADMAP.md` per deployment su VPS.

---

## 🐛 Troubleshooting

### Test fallisce con "No module named 'services'"

**Soluzione**: Assicurati di essere in `backend/` directory:
```bash
cd backend/
python3 scripts/testing/verify_cache_refresh.py --mode quick
```

### Test fallisce con "Database not found"

**Soluzione**: Esegui migrazioni prima:
```bash
cd backend/
python3 -m alembic upgrade head
```

### Cache verification FAIL - Age non diminuisce

**Possibile causa**: Cache manager non sta invalidando dati scaduti.

**Debug**:
```bash
# Controlla cache_manager.py implementazione
grep -n "is_expired" backend/services/orchestrator/cache_manager.py
```

### Orchestrator lentissimo (>180s per ciclo)

**Possibile causa**: Prophet in FULL mode invece di LITE.

**Soluzione**: Verifica configurazione Prophet:
```python
# In market_data_orchestrator.py dovrebbe essere:
prophet_mode="lite"  # NOT "full"
```

### Rate limit 429 errors durante test

**Causa**: Troppi test eseguiti consecutivamente.

**Soluzione**: Aspetta 2-5 minuti tra test multipli per permettere rate limit reset.

---

## 📝 Testing Checklist

Prima del deployment finale, verifica:

- [ ] ✅ Cache verification quick mode PASSED
- [ ] ✅ Orchestrator test 3 cicli PASSED (senza Prophet)
- [ ] ✅ Orchestrator test 3 cicli PASSED (con Prophet) - OPTIONAL
- [ ] ✅ Rate limit monitoring: NO 429 errors
- [ ] ✅ CoinMarketCap calls: ~24/day (non più 288!)
- [ ] ✅ JSON structure completa con tutti i campi
- [ ] ✅ Portfolio balance NON è $0 (dati reali da Hyperliquid)
- [ ] ✅ Top symbols hanno technical_analysis, pivot_points
- [ ] ✅ Global indicators hanno sentiment, news, whale alerts
- [ ] ⚠️ Whale Alert projected usage <1000/month (verificare)

**Se tutti i check sono ✅ → Sistema pronto per produzione!**
