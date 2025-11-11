# Microservices Architecture - JSON Market Data Pipeline

**Status**: Design Phase
**Last Updated**: 2025-11-10
**Goal**: Refactor from narrative prompt to structured JSON with complete data coverage

---

## 📊 Executive Summary

**Current Problem**:
- Technical Analysis analyzes 142 symbols (~468 API calls, 90s)
- DeepSeek receives only TOP 5 symbols (96.5% data wasted!)
- Narrative prompt format (inefficient, hard to parse)
- Missing indicators for most symbols

**Target Solution**:
- Structured JSON with ALL 142 symbols
- Each microservice contributes its data
- Coordinated pipeline running every 3 minutes
- Complete indicator coverage per symbol

**Key Metrics**:
- **Data Utilization**: 3.5% → 100% (use all analyzed symbols)
- **Token Efficiency**: Narrative → JSON saves ~40% tokens
- **Decision Quality**: 5 options → 142 options for AI
- **Cost**: +$7/month for complete coverage

---

## 🏗️ Current System Architecture

### Microservices Inventory

| Service | Location | Purpose | API Calls | Cache | Duration |
|---------|----------|---------|-----------|-------|----------|
| **Technical Analysis** | `technical_analysis_service.py` | Momentum + Support scoring | 468/cycle | None | ~90s |
| **Pivot Calculator** | `pivot_calculator.py` | Support/Resistance levels | 3/symbol | None | ~0.5s/symbol |
| **Prophet Forecaster** | `prophet_forecaster.py` | ML price prediction (24h) | 90/symbol | 24h | ~3s/symbol |
| **Sentiment Tracker** | `sentiment_tracker.py` | Fear & Greed Index (global) | 1 | 5min | ~0.3s |
| **Whale Tracker** | `whale_tracker.py` | Large transactions >$10M | 1 | Real-time | ~0.5s |
| **News Feed** | `news_cache.py` | CoinJournal headlines | 1 | 1h | ~0.3s |
| **Price Cache** | `price_cache.py` | All market prices | 1 | 30s | ~0.3s |

### Current Data Flow (AI Trading Cycle)

```
┌─────────────────────────────────────────────────────────────┐
│  TRIGGER: APScheduler Job (every 3 minutes)                 │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  1. Fetch Market Prices (all_mids)                          │
│     - API calls: 1                                          │
│     - Duration: ~0.3s                                       │
│     - Output: {symbol: price} for ~468 symbols             │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  2. Calculate Technical Analysis (ALL 468 symbols)          │
│     - API calls: 468 (one per symbol for klines)           │
│     - Duration: ~90s (sequential with 150ms delays)        │
│     - Success: ~142 symbols (have 70+ days data)           │
│     - Output: {symbol, score, momentum, support}           │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  3. Build Portfolio Data                                    │
│     - API calls: 1 (Hyperliquid user_state)                │
│     - Duration: ~0.5s                                       │
│     - Output: {cash, positions, total_assets}              │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  4. Call AI for Decision (DeepSeek)                         │
│     ├─ Fetch News (cache 1h)                               │
│     ├─ Format Technical (TOP 5 ONLY!) ❌                   │
│     ├─ Calculate Pivot Points (3-8 symbols)                │
│     ├─ Prophet Forecast (BTC, ETH only)                    │
│     ├─ Sentiment Index (global)                            │
│     ├─ Whale Alerts (global)                               │
│     └─ Build narrative prompt (~3,117 tokens)              │
│     - API calls: 2-3 (DeepSeek + pivot data)               │
│     - Duration: ~3-5s                                       │
│     - Output: {operation, symbol, size, reason}            │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  5. Execute Order on Hyperliquid                            │
│     - API calls: 2 (set leverage + place order)            │
│     - Duration: ~1s                                         │
└─────────────────────────────────────────────────────────────┘

TOTAL: ~470 API calls, ~95 seconds per cycle
```

### Critical Issues

1. **DATA WASTE**: 142 symbols analyzed, only 5 used (96.5% wasted!)
2. **INCOMPLETE COVERAGE**: Most symbols missing pivot/prophet data
3. **NARRATIVE FORMAT**: Inefficient, hard to parse, wastes tokens
4. **NO LEARNING FEEDBACK**: Suggested weights not automatically applied

---

## 🎯 Target Architecture: Microservices with JSON Aggregation

### Design Principles

1. **Separation of Concerns**: Each microservice is independent and cacheable
2. **Parallel Execution**: Run independent services concurrently
3. **Lazy Computation**: Only calculate expensive indicators (Prophet) for top symbols
4. **Structured Output**: Every service returns typed JSON
5. **Cache Hierarchy**: Different TTLs based on data volatility

### Microservices Structure

```
backend/services/
├── orchestrator/
│   ├── market_data_orchestrator.py    ← NEW: Coordinates all services
│   ├── json_builder.py                ← NEW: Aggregates all data into JSON
│   └── cache_manager.py               ← NEW: Unified caching layer
├── market_data/
│   ├── technical_analysis_service.py  ← REFACTOR: Return structured dict
│   ├── pivot_calculator.py            ← REFACTOR: Batch calculation for 142 symbols
│   ├── prophet_forecaster.py          ← REFACTOR: Configurable symbol list
│   ├── sentiment_tracker.py           ← KEEP: Already structured
│   ├── whale_tracker.py               ← KEEP: Already structured
│   └── news_cache.py                  ← KEEP: Already structured
└── ai/
    ├── deepseek_client.py             ← NEW: JSON prompt builder
    └── decision_parser.py             ← NEW: Parse DeepSeek JSON response
```

---

## 📐 JSON Schema Design

### Complete Market Data Structure

```typescript
interface MarketDataSnapshot {
  metadata: {
    timestamp: string;           // ISO 8601
    version: string;             // "2.0.0"
    symbols_analyzed: number;    // 142
    cycle_duration_ms: number;   // ~95000
  };

  symbols: SymbolData[];         // Array of 142 symbols
  global_indicators: GlobalIndicators;
  portfolio: PortfolioState;
}

interface SymbolData {
  symbol: string;                // "BTC"
  price: number;                 // 102450.0

  technical_analysis: {
    score: number;               // 0.0-1.0
    momentum: number;            // 0.0-1.0
    support: number;             // 0.0-1.0
    signal: "STRONG_BUY" | "BUY" | "HOLD" | "SELL" | "STRONG_SELL";
    rank: number;                // 1-142 (1 = best)
  };

  pivot_points: {
    PP: number;
    R1: number;
    R2: number;
    R3: number;
    S1: number;
    S2: number;
    S3: number;
    current_zone: "above_R1" | "bullish" | "neutral" | "bearish" | "below_S1";
    signal: "long_opportunity" | "short_opportunity" | "bullish_zone" | "bearish_zone" | "neutral";
    distance_to_support_pct: number;  // Negative if below support
    distance_to_resistance_pct: number;
  };

  prophet_forecast: {
    current_price: number;
    forecast_6h: number;
    forecast_24h: number;
    change_pct_6h: number;
    change_pct_24h: number;
    trend: "up" | "down" | "neutral";
    confidence: number;          // 0.0-1.0
    confidence_interval_24h: [number, number];
  } | null;  // null if not in top 20

  market_data: {
    volume_24h: number;
    market_cap: number | null;
    rank_by_market_cap: number | null;
  };
}

interface GlobalIndicators {
  sentiment: {
    value: number;               // 0-100
    label: "EXTREME_FEAR" | "FEAR" | "NEUTRAL" | "GREED" | "EXTREME_GREED";
    signal: "contrarian_long" | "contrarian_short" | "neutral";
    last_updated: string;
  };

  whale_alerts: Array<{
    symbol: string;
    amount_usd: number;
    transaction_type: "transfer" | "exchange_inflow" | "exchange_outflow";
    from: string;
    to: string;
    timestamp: string;
    signal: "sell_pressure" | "buy_pressure" | "neutral";
  }>;

  news: Array<{
    headline: string;
    summary: string | null;
    url: string;
    published_at: string;
    sentiment: "positive" | "neutral" | "negative" | null;
    mentioned_symbols: string[];
  }>;
}

interface PortfolioState {
  total_assets: number;
  available_cash: number;
  positions_value: number;
  unrealized_pnl: number;

  positions: Array<{
    symbol: string;
    quantity: number;
    side: "LONG" | "SHORT";
    entry_price: number;
    current_price: number;
    unrealized_pnl: number;
    unrealized_pnl_pct: number;
    market_value: number;
  }>;

  strategy_weights: {
    prophet: number;
    pivot_points: number;
    technical_analysis: number;
    whale_alerts: number;
    sentiment: number;
    news: number;
  };
}
```

---

## ⚙️ Orchestration Pipeline

### Execution Strategy: Sequential + Parallel Hybrid

```python
# Pseudocode for orchestrator

async def build_market_data_snapshot(account_id: int) -> MarketDataSnapshot:
    """
    Orchestrate all microservices to build complete JSON snapshot.

    Execution order:
    1. SEQUENTIAL: Fetch prices (needed by everyone)
    2. PARALLEL: Run all per-symbol analyses concurrently
    3. PARALLEL: Fetch global indicators concurrently
    4. SEQUENTIAL: Aggregate into final JSON
    """

    start_time = time.time()

    # ============================================
    # STAGE 1: Fetch Market Prices (MUST be first)
    # ============================================
    prices = await fetch_all_prices()  # 1 API call, ~0.3s
    available_symbols = list(prices.keys())  # ~468 symbols

    # ============================================
    # STAGE 2: Per-Symbol Analysis (PARALLEL)
    # ============================================
    # These can run concurrently as they're independent

    technical_task = asyncio.create_task(
        calculate_technical_analysis(available_symbols)
        # 468 API calls, ~90s (sequential internally due to rate limiting)
        # Returns: {symbol: {score, momentum, support}}
    )

    pivot_task = asyncio.create_task(
        calculate_pivot_points_batch(available_symbols)
        # 142 * 3 = 426 API calls (only for symbols with technical data)
        # Can be batched or cached aggressively (pivot data stable)
        # Returns: {symbol: {PP, R1-R3, S1-S3, signal}}
    )

    prophet_task = asyncio.create_task(
        calculate_prophet_forecasts(
            symbols=get_top_symbols_by_market_cap(20),  # Top 20 only
            cache_ttl_hours=24  # Aggressive caching
        )
        # 20 symbols * 7 API calls (7 days of 1h candles) = 140 API calls
        # But cached for 24h! So only runs once per day
        # Returns: {symbol: {forecast_6h, forecast_24h, confidence}}
    )

    # Wait for all per-symbol analyses
    technical_results, pivot_results, prophet_results = await asyncio.gather(
        technical_task,
        pivot_task,
        prophet_task,
        return_exceptions=True  # Don't fail entire pipeline if one service fails
    )

    # ============================================
    # STAGE 3: Global Indicators (PARALLEL)
    # ============================================
    # These are independent and fast

    sentiment_task = asyncio.create_task(get_sentiment_index())
    whale_task = asyncio.create_task(get_whale_alerts())
    news_task = asyncio.create_task(fetch_latest_news())
    portfolio_task = asyncio.create_task(get_portfolio_state(account_id))

    sentiment, whale_alerts, news, portfolio = await asyncio.gather(
        sentiment_task,
        whale_task,
        news_task,
        portfolio_task,
        return_exceptions=True
    )

    # ============================================
    # STAGE 4: Build Unified JSON
    # ============================================
    snapshot = {
        "metadata": {
            "timestamp": datetime.utcnow().isoformat(),
            "version": "2.0.0",
            "symbols_analyzed": len(technical_results),
            "cycle_duration_ms": int((time.time() - start_time) * 1000)
        },
        "symbols": _merge_symbol_data(
            prices=prices,
            technical=technical_results,
            pivots=pivot_results,
            prophet=prophet_results
        ),
        "global_indicators": {
            "sentiment": sentiment,
            "whale_alerts": whale_alerts,
            "news": news
        },
        "portfolio": portfolio
    }

    return snapshot
```

---

## 🚀 Performance Optimization Strategies

### 1. Intelligent Caching

```python
# Cache layers with different TTLs

class CacheLayer:
    PRICES = 30          # 30 seconds (volatile)
    TECHNICAL = 180      # 3 minutes (recalculate every cycle)
    PIVOT = 3600         # 1 hour (stable, recalculate hourly)
    PROPHET = 86400      # 24 hours (expensive, daily update)
    SENTIMENT = 300      # 5 minutes (moderate update)
    WHALE = 60           # 1 minute (near real-time)
    NEWS = 3600          # 1 hour (headlines don't change fast)
```

**Impact**:
- Prophet: 140 API calls/day instead of 67,200/day (99.8% reduction!)
- Pivot: 426 API calls/hour instead of 8,520/hour (95% reduction!)
- Total: ~600 API calls/cycle → ~470 API calls/cycle (maintain current load)

### 2. Batch Processing for Pivot Points

```python
async def calculate_pivot_points_batch(symbols: List[str]) -> Dict[str, dict]:
    """
    Calculate pivot points for multiple symbols in batch.

    Optimization: Instead of sequential calls, batch fetch OHLC data.
    """

    # Check cache first
    cached = cache_manager.get_batch("pivot_points", symbols)

    # Only fetch missing symbols
    missing_symbols = [s for s in symbols if s not in cached]

    if not missing_symbols:
        return cached

    # Batch fetch OHLC data (one call for multiple symbols if API supports)
    ohlc_data = await hyperliquid_client.fetch_ohlc_batch(
        symbols=missing_symbols,
        timeframe="1d",
        limit=3  # Last 3 days for pivot calculation
    )

    # Calculate pivots in parallel (CPU-bound, use ThreadPool)
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(_calculate_pivot, symbol, ohlc_data[symbol]): symbol
            for symbol in missing_symbols
        }

        results = {}
        for future in as_completed(futures):
            symbol = futures[future]
            results[symbol] = future.result()

    # Cache for 1 hour
    cache_manager.set_batch("pivot_points", results, ttl=3600)

    # Merge with cached results
    return {**cached, **results}
```

### 3. Prophet Lite Mode

```python
class ProphetForecaster:
    def __init__(self, mode: str = "full"):
        self.mode = mode  # "full" or "lite"

    def forecast_price(self, symbol: str) -> dict:
        if self.mode == "lite":
            # Use 7 days of 1h candles instead of 90 days
            # 7 API calls instead of 90 (13x faster!)
            # Accuracy: ~85% of full model (acceptable trade-off)
            return self._forecast_lite(symbol)
        else:
            return self._forecast_full(symbol)
```

**Trade-off**:
- Full mode: 90 API calls/symbol, ~3s, 95% accuracy
- Lite mode: 7 API calls/symbol, ~0.5s, 85% accuracy
- For 20 symbols: 1,800 calls → 140 calls (13x reduction!)

---

## 📊 Performance Comparison

### Current System

| Metric | Value |
|--------|-------|
| API calls per cycle | ~470 |
| Cycle duration | ~95s |
| Symbols with complete data | 2 (BTC, ETH) |
| Data utilization | 3.5% (5/142) |
| DeepSeek prompt tokens | ~3,117 |
| Cost per cycle | $0.000436 |
| Cost per day | $0.21 |
| Cost per month | $6.30 |

### Target System (with optimizations)

| Metric | Value | Change |
|--------|-------|--------|
| API calls per cycle | ~470 (cached Prophet/Pivot) | **+0%** ✅ |
| API calls per day (Prophet) | +140 (once daily) | Negligible |
| API calls per day (Pivot) | +426 (once hourly) | Small increase |
| Cycle duration | ~95s (same) | **+0s** ✅ |
| Symbols with complete data | 142 (all) | **+70x** 🚀 |
| Data utilization | 100% (142/142) | **+96.5%** 🚀 |
| DeepSeek prompt tokens | ~10,665 (JSON) | **+242%** |
| Cost per cycle | $0.001493 | **+243%** |
| Cost per day | $0.72 | **+243%** |
| Cost per month | $21.60 | **+243%** |

**Cost Analysis**:
- **Extra cost**: +$15.30/month
- **Benefit**: 142 symbols with complete indicators (vs 5 currently)
- **ROI**: If better decisions make >$15.30/month extra profit → pays for itself

**Alternative: Top 20 Symbols with Complete Data**

| Metric | Value | Change |
|--------|-------|--------|
| Symbols with complete data | 20 (top market cap) | **+10x** 🚀 |
| DeepSeek prompt tokens | ~6,600 | **+112%** |
| Cost per month | $13.00 | **+106%** |

**Sweet Spot**: Top 20 with complete data = best cost/benefit ratio

---

## 🔄 Migration Plan

### Phase 1: JSON Schema & Builder (Week 1)

**Tasks**:
1. Define TypeScript interfaces → Generate Python TypedDict
2. Create `json_builder.py` with schema validation
3. Unit tests for JSON structure

**Files to Create**:
- `backend/services/orchestrator/json_builder.py`
- `backend/services/orchestrator/schemas.py`
- `backend/tests/unit/test_json_builder.py`

**Deliverable**: Valid JSON can be generated (with mock data)

### Phase 2: Refactor Microservices (Week 2)

**Tasks**:
1. Refactor Technical Analysis to return structured dict
2. Extend Pivot Calculator for batch processing
3. Configure Prophet for top 20 symbols
4. Add caching layer to all services

**Files to Modify**:
- `backend/services/market_data/technical_analysis_service.py`
- `backend/services/market_data/pivot_calculator.py`
- `backend/services/market_data/prophet_forecaster.py`

**Deliverable**: Each service returns typed dict matching schema

### Phase 3: Orchestrator Implementation (Week 3)

**Tasks**:
1. Create `market_data_orchestrator.py`
2. Implement parallel execution with asyncio.gather()
3. Add error handling and fallbacks
4. Integration tests with real Hyperliquid data

**Files to Create**:
- `backend/services/orchestrator/market_data_orchestrator.py`
- `backend/services/orchestrator/cache_manager.py`

**Deliverable**: Full JSON snapshot generated every 3 minutes

### Phase 4: DeepSeek Integration (Week 4)

**Tasks**:
1. Create new JSON prompt builder
2. Modify DeepSeek prompt to parse JSON instead of narrative
3. Update decision parsing logic
4. A/B testing: compare decisions (old vs new prompt)

**Files to Create**:
- `backend/services/ai/deepseek_client.py`
- `backend/services/ai/json_prompt_builder.py`

**Deliverable**: DeepSeek makes decisions using JSON input

### Phase 5: Feedback Loop (Week 5)

**Tasks**:
1. Auto-apply suggested weights from self-analysis
2. Log JSON snapshots for debugging
3. Dashboard to visualize indicator performance
4. Gradual rollout (10% → 50% → 100% of decisions)

**Deliverable**: Closed feedback loop for continuous improvement

---

## 🎯 Success Metrics

### Technical Metrics

- ✅ JSON generation time: <95 seconds (maintain current speed)
- ✅ API calls per cycle: ~470 (maintain current load)
- ✅ JSON schema validation: 100% compliance
- ✅ Cache hit rate: >80% for Prophet/Pivot

### Business Metrics

- ✅ Symbols with complete indicators: 20+ (vs 2 currently)
- ✅ Decision quality: Measure P&L improvement after 1 month
- ✅ Cost efficiency: Extra cost <$20/month for 10x more data
- ✅ Learning speed: Suggested weights auto-applied within 1 week

---

## 🚨 Risk Mitigation

### Risk 1: Increased API Load

**Mitigation**:
- Aggressive caching for Prophet (24h) and Pivot (1h)
- Prophet Lite mode (7 days instead of 90)
- Rate limiting with exponential backoff

### Risk 2: JSON Too Large for DeepSeek

**Mitigation**:
- Top 20 symbols with complete data (10,665 tokens < 32k limit)
- Compress global indicators (only last 10 news headlines)
- Remove redundant fields

### Risk 3: Slower Cycle Times

**Mitigation**:
- Parallel execution with asyncio.gather()
- Batch processing for Pivot Points
- Pre-computed Prophet forecasts (24h cache)

### Risk 4: Breaking Changes

**Mitigation**:
- Gradual rollout (shadow mode first)
- A/B testing old vs new prompts
- Rollback plan (keep old code for 2 weeks)

---

## 📚 Next Steps

1. **Review this design** with stakeholders
2. **Approve cost increase** (+$15/month for 142 symbols OR +$7/month for 20 symbols)
3. **Start Phase 1** (JSON schema & builder)
4. **Set up monitoring** for API load and cycle times

**Decision Required**:
- Option A: 142 symbols with complete data (+$15/month)
- Option B: 20 symbols with complete data (+$7/month) ← **RECOMMENDED**

---

**Document Version**: 1.0.0
**Author**: AI Trading System
**Review Status**: Awaiting Approval
