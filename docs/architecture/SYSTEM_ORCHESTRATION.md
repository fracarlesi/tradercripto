# System Orchestration - Operational Flow

This document explains how the Bitcoin Trading System operates in production, from startup to trading cycles to data synchronization.

## Table of Contents

- [System Startup](#system-startup)
- [Scheduled Jobs](#scheduled-jobs)
- [AI Trading Cycle](#ai-trading-cycle)
- [Data Flow](#data-flow)
- [Order Execution Flow](#order-execution-flow)

---

## System Startup

### Startup Sequence (main.py:45-185)

The system follows a strict initialization order to prevent race conditions:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Phase 1: Database Setup
    Base.metadata.create_all(bind=sync_engine)

    # Phase 2: Service Initialization
    initialize_services()  # from services/startup.py

    # Phase 3: Metrics Service
    metrics_service.start()

    # Phase 4: APScheduler Jobs
    scheduler_service.start()
    scheduler_service.add_sync_job(...)

    yield  # App running

    # Phase 5: Graceful Shutdown
    scheduler_service.stop()
    shutdown_services()
```

### Service Initialization Order (services/startup.py:12-89)

**CRITICAL**: This order MUST be preserved - dependencies exist between services.

```python
def initialize_services():
    # 1. Task Scheduler (MUST be first)
    start_scheduler()

    # 2. Market Data Tasks
    setup_market_tasks()

    # 3. Auto Trading (depends on market data)
    schedule_auto_trading()

    # 4-6. Independent Interval Tasks (order doesn't matter)
    task_scheduler.add_interval_task(clear_expired_prices, 120)
    task_scheduler.add_interval_task(sync_all_active_accounts, 60)
    task_scheduler.add_interval_task(capture_snapshots_wrapper, 300)
```

**Why this order matters**:
- Scheduler MUST start first (all jobs register to it)
- Auto trading needs market data → `setup_market_tasks()` first
- Interval tasks are independent → can be in any order

---

## Scheduled Jobs

All background jobs with their intervals and purpose. See [SCHEDULED_JOBS.md](../operations/SCHEDULED_JOBS.md) for complete details.

### Job Overview

| Job | Interval | File | API Calls | Purpose |
|-----|----------|------|-----------|---------|
| AI Trading | 180s (3min) | `main.py:161` | 2-3/cycle | DeepSeek AI decisions + order execution |
| Hyperliquid Sync | 30s | `main.py:131` | 1/cycle | Sync balance/positions/orders from exchange |
| Stop Loss Check | 60s | `main.py:147` | 1/cycle | Check -5% loss threshold, close if hit |
| Price Cache Cleanup | 120s (2min) | `startup.py:27` | 0 (local) | Clear expired price cache entries |
| Portfolio Snapshot | 300s (5min) | `startup.py:51` | 1/cycle | Historical chart data capture |
| Counterfactual Learning | 3600s (1h) | `startup.py:85` | 0.1/cycle | Calculate P&L for past decisions |
| AI Usage Reset | Daily 00:00 | `main.py:140` | 0 (local) | Reset AI usage counters |

**Total API Load**: ~3.5 calls/min = ~210 calls/hour

---

## AI Trading Cycle

### Complete Trading Cycle (180 seconds / 3 minutes)

File: `backend/services/auto_trader.py:32-233`

#### Phase 0: Initialization (0:00 - 0:01, ~1 second)

```python
logger.info("=== AI Trading Cycle Started ===")

# Get active AI trading account from database
account = db.query(Account).filter(
    Account.account_type == "AI",
    Account.is_active == True
).first()
```

**Data structures**:
```python
account = {
    "id": 1,
    "name": "DeepSeek",
    "model": "deepseek-chat",
    "api_key": "sk-..."
}
```

#### Phase 1: Fetch Market Prices (0:01 - 0:02, ~1 second)

```python
# Single API call to Hyperliquid all_mids() endpoint
prices = hyperliquid_trading_service.get_all_mids_async()
```

**API Call**: `info.all_mids()` → Returns 468+ symbols in ONE call

**Data structure**:
```python
prices = {
    "BTC": 50000.0,
    "ETH": 3000.0,
    "SOL": 100.0,
    # ... 468+ symbols
}
```

#### Phase 2: Build Portfolio (0:02 - 0:03, ~1 second)

```python
# Fetch real-time data from Hyperliquid
user_state = await hyperliquid_trading_service.get_user_state_async()

portfolio = {
    "cash": float(user_state['marginSummary']['accountValue']),
    "available_cash": float(user_state['marginSummary']['withdrawable']),
    "frozen_cash": float(user_state['marginSummary']['totalMarginUsed']),
    "positions": [...],
    "total_assets": cash + positions_value
}
```

**Data structure**:
```python
portfolio = {
    "cash": 10000.0,
    "available_cash": 8000.0,
    "frozen_cash": 2000.0,
    "positions": [
        {"symbol": "BTC", "quantity": 0.1, "avg_cost": 50000.0, "value": 5000.0},
        {"symbol": "ETH", "quantity": 1.0, "avg_cost": 3000.0, "value": 3000.0}
    ],
    "total_assets": 18000.0
}
```

#### Phase 3.5: Calculate Technical Factors (0:03 - 1:30, ~90 seconds)

```python
# Analyze 100+ symbols with 6 technical indicators
technical_factors = calculate_technical_factors(available_symbols)
```

**Implementation** (`technical_analysis_service.py:80-260`):
- Fetches 70-day historical klines for EACH symbol
- **Sequential execution**: `MAX_WORKERS=1`, `150ms` delay between requests
- **Why sequential**: Hyperliquid rate limits ~10-20 req/sec, prevent 429 errors
- **Indicators calculated**:
  1. Pivot Points (R1, S1, PP)
  2. Prophet 7-day forecast
  3. RSI + MACD momentum
  4. Whale alerts (large transactions)
  5. Sentiment index (social + news)
  6. News feed analysis

**Data structure**:
```python
technical_factors = {
    "recommendations": [
        {
            "symbol": "BTC",
            "pivot_points": {"PP": 50000, "R1": 51000, "S1": 49000, "signal": "BULLISH"},
            "prophet": {"trend": "UP", "forecast_7d": 52000, "confidence": 0.85},
            "rsi_macd": {"rsi": 45, "macd_signal": "BUY", "strength": "MODERATE"},
            "whale_alerts": {"count_24h": 3, "net_flow": "+$2.5M", "sentiment": "BULLISH"},
            "sentiment": {"score": 72, "trend": "POSITIVE"},
            "news": {"relevance": 0.8, "sentiment": 0.6, "summary": "..."}
        },
        # ... 100+ symbols analyzed
    ]
}
```

#### Phase 4: AI Decision (1:30 - 1:32, ~2 seconds)

```python
# Call DeepSeek API with mega-prompt
decision = await call_ai_for_decision(account, portfolio, prices)
```

**AI Prompt Structure** (`ai_decision_service.py:150-350`):
```
Current Portfolio: {...}
Market Prices: {...}
Technical Analysis (6 indicators with weights):
  - Pivot Points (0.8 weight - HIGHEST PRIORITY)
  - Prophet Forecast (0.5 weight)
  - RSI/MACD (0.5 weight)
  - Whale Alerts (0.4 weight)
  - Sentiment (0.3 weight)
  - News (0.2 weight)

Decide: BUY/SHORT/HOLD
Target: 20% of portfolio max per trade
Reasoning: Explain decision based on weighted factors
```

**Data structure**:
```python
decision = {
    "operation": "buy",  # or "short", "hold"
    "symbol": "BTC",
    "target_portion_of_balance": 0.15,  # 15% of portfolio
    "reason": "Strong bullish pivot (R1 break), Prophet 7d +4%, RSI oversold recovery..."
}
```

**Decision Caching** (10-minute window):
- Prevents duplicate trades on identical market conditions
- MD5 hash of: `(BTC_price, portfolio_value, news_summary)`
- ~30% reduction in API calls

#### Phase 5: Snapshot Saving (1:32 - 1:33, ~1 second)

```python
# Save decision for counterfactual learning
save_decision_snapshot(
    account_id=account.id,
    decision=actual_decision,  # "LONG", "SHORT", or "HOLD"
    symbol=symbol,
    entry_price=entry_price,
    indicators_snapshot=technical_factors,
    deepseek_reasoning=decision["reason"]
)
```

**Purpose**: Enable self-learning system to analyze decision quality 24h later.

#### Phase 6: Validation (1:33 - 1:34, ~1 second)

```python
validation_result = _validate_decision(decision, portfolio, prices, max_ratio)
```

**Validation checks**:
1. **Minimum order size**: $10 (Hyperliquid requirement)
2. **Maximum ratio per trade**: 20% of portfolio (configurable)
3. **Available balance**: Check `withdrawable` funds
4. **Symbol validity**: Exists in `prices` dict
5. **Leverage limits**: 1-10x only

**Data structure**:
```python
validation_result = {
    "valid": True,
    "order_size": 0.05,  # BTC quantity
    "order_value": 2500.0,  # USD value
    "leverage": 2,
    "reason": "Valid - within limits"
}
```

#### Phase 7: Order Execution (1:34 - 1:36, ~2 seconds)

```python
# Execute on Hyperliquid
execution_result = await hyperliquid_trading_service.place_market_order_async(
    symbol=symbol,
    is_buy=True,
    size=order_size,
    leverage=leverage
)
```

**API Calls**:
1. `exchange.update_leverage()` - Set leverage before order
2. `exchange.market_open()` - Place market order

**Data structure**:
```python
execution_result = {
    "status": "ok",
    "response": {
        "type": "order",
        "data": {
            "statuses": [
                {"filled": {"totalSz": "0.05", "avgPx": "50000.0"}}
            ]
        }
    }
}
```

#### Phase 8: Post-Trade Sync (1:36 - 1:38, ~2 seconds)

```python
# Immediately sync from Hyperliquid to database
await hyperliquid_sync_service.sync_account(db, account.id)
```

**Syncs**:
- Account balance (accountValue, withdrawable, marginUsed)
- Positions (updated with new trade)
- Orders (created order record)
- Trades (fills from order execution)

#### Phase 9: Completion (1:38, <1 second)

```python
# Save decision to database for analysis
save_ai_decision(db, account, decision, portfolio, executed=True)

logger.info("=== AI Trading Cycle Completed ===")
```

**Total cycle time**: ~98 seconds (mostly technical analysis)
**Next cycle**: In 82 seconds (180s interval)

---

## Data Flow

Complete flow from external data sources to frontend display.

### Data Sources Hierarchy

```
┌─────────────────────────────────────────────────────────┐
│         HYPERLIQUID DEX (Single Source of Truth)        │
│  • Account balance (accountValue, withdrawable)         │
│  • Positions (size, entry price, PNL, leverage)         │
│  • Order history, fills, trades                         │
│  • Market prices (all_mids endpoint - 468+ symbols)     │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
        ┌──────────────────────────────┐
        │   Hyperliquid Sync Service   │
        │    (Every 30 seconds)        │
        └──────────────┬───────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│              PostgreSQL Database (Cache)                 │
│  • Account metadata (AI config, name, type)             │
│  • Positions (snapshot for display - CLEARED each sync) │
│  • Orders (deduplicated by order_id)                    │
│  • Trades (deduplicated by trade_id)                    │
│  • PortfolioSnapshot (5-min intervals for charts)       │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
        ┌──────────────────────────────┐
        │      WebSocket Server        │
        │   (Real-time push to UI)     │
        └──────────────┬───────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│                   React Frontend                         │
│  • Portfolio overview                                    │
│  • Positions table                                       │
│  • Order history                                         │
│  • Asset curves (PortfolioSnapshot)                     │
└─────────────────────────────────────────────────────────┘
```

### Data Sync Algorithm (30-second cycle)

File: `backend/services/trading/hyperliquid_sync_service.py:80-200`

```python
async def sync_account(db: AsyncSession, account_id: int):
    """Sync account data from Hyperliquid to database (30s interval)."""

    # 1. Fetch from Hyperliquid
    user_state = await hyperliquid_trading_service.get_user_state_async()
    fills = await hyperliquid_trading_service.get_user_fills_async(limit=100)

    # 2. BEGIN TRANSACTION
    async with db.begin_nested():

        # 3. Update account balance (real-time source of truth)
        account.withdrawable = user_state['marginSummary']['withdrawable']
        account.account_value = user_state['marginSummary']['accountValue']

        # 4. Clear & recreate positions (snapshot approach)
        await PositionRepository.clear_positions(db, account_id)
        await PositionRepository.bulk_create_positions(
            db, account_id, user_state['assetPositions']
        )

        # 5. Upsert orders (deduplicate by order_id)
        for fill in fills:
            existing_order = await OrderRepository.get_by_order_no(db, fill['order_id'])
            if not existing_order:
                await OrderRepository.create_order(db, account_id, fill)

        # 6. Upsert trades (deduplicate by trade_id + timestamp)
        for fill in fills:
            existing_trade = await TradeRepository.find_duplicate(db, fill)
            if not existing_trade:
                await TradeRepository.create_trade(db, account_id, fill)

        # 7. COMMIT TRANSACTION
        await db.commit()
```

**Key Points**:
- **Positions**: Cleared & recreated EVERY sync (snapshot model)
- **Orders/Trades**: Deduplicated, never deleted (append-only log)
- **Balance**: ALWAYS from Hyperliquid (never stored in DB)

---

## Order Execution Flow

Complete flow when AI decides to execute a trade.

### Step-by-Step Execution

```
┌─────────────────────────────────────────────────────────┐
│ 1. AI Decision Generated                                 │
│    • DeepSeek API call with technical analysis          │
│    • Returns: {operation: "buy", symbol: "BTC", ...}    │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│ 2. Validation                                            │
│    • Check minimum order size ($10)                     │
│    • Check max ratio per trade (20%)                    │
│    • Check available balance (withdrawable funds)       │
│    • Validate symbol exists in prices                   │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼ (if valid)
┌─────────────────────────────────────────────────────────┐
│ 3. Update Leverage (if opening position)                │
│    • API: exchange.update_leverage(symbol, leverage)    │
│    • Sets 1-10x leverage BEFORE opening position        │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│ 4. Place Market Order                                    │
│    • API: exchange.market_open(symbol, is_buy, size)    │
│    • Hyperliquid executes immediately (market order)    │
│    • Response includes fill details                     │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│ 5. Check Execution Status                                │
│    • Parse response for errors                          │
│    • If errors: Log rejection, save decision as failed  │
│    • If success: Continue to sync                       │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼ (if successful)
┌─────────────────────────────────────────────────────────┐
│ 6. Immediate Post-Trade Sync                            │
│    • Sync balance (reduced by order value + margin)     │
│    • Sync positions (new position added)                │
│    • Sync orders (order record created)                 │
│    • Sync trades (fill record created)                  │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│ 7. Save AI Decision Log                                  │
│    • Store decision in ai_decision_logs table           │
│    • Mark as executed=True with order_id                │
│    • Used for performance analysis                      │
└─────────────────────────────────────────────────────────┘
```

### Error Handling

**Common errors and responses**:

1. **Rate Limit (429)**:
   - Response: `RateLimitExceeded: hyperliquid POST 429 Too Many Requests`
   - Action: Automatic retry with exponential backoff (1s, 2s, 4s)
   - Max retries: 3 attempts

2. **Insufficient Balance**:
   - Response: `{"error": "Insufficient margin"}`
   - Action: Log warning, save decision as failed, skip cycle

3. **Invalid Symbol**:
   - Response: `{"error": "Invalid asset"}`
   - Action: Remove from analysis list, continue with other symbols

4. **Order Rejected**:
   - Response: `{"error": "Order size below minimum"}`
   - Action: Increase size calculation, retry next cycle

---

## Performance Metrics

### Typical Cycle Times

| Operation | Time | File Reference |
|-----------|------|----------------|
| Market price fetch | ~1s | `hyperliquid_trading_service.py:157` |
| Portfolio build | ~1s | `auto_trader.py:70-85` |
| Technical analysis (100+ symbols) | ~90s | `technical_analysis_service.py:80-260` |
| AI decision (DeepSeek API) | ~2s | `ai_decision_service.py:150` |
| Order validation | ~1s | `auto_trader.py:188-205` |
| Order execution | ~2s | `hyperliquid_trading_service.py:236-301` |
| Post-trade sync | ~2s | `hyperliquid_sync_service.py:80-200` |
| **Total AI Trading Cycle** | **~98s** | **Complete cycle** |

### API Call Breakdown (per minute)

| Source | Calls/min | Purpose |
|--------|-----------|---------|
| AI trading cycle | 1.67 | Every 3 minutes |
| Hyperliquid sync | 2.0 | Every 30 seconds |
| Stop-loss check | 1.0 | Every 60 seconds |
| Portfolio snapshot | 0.2 | Every 5 minutes |
| **Total** | **~4.87** | **~292 calls/hour** |

---

## Related Documentation

- **[SCHEDULED_JOBS.md](../operations/SCHEDULED_JOBS.md)** - Complete job details and rate limiting
- **[OVERVIEW.md](OVERVIEW.md)** - High-level architecture and design patterns
- **[DATA_FLOW.md](DATA_FLOW.md)** - Data flow diagrams and source hierarchy
- **[MONITORING.md](../operations/MONITORING.md)** - Debug workflows and troubleshooting

---

**Last Updated**: 2025-11-10
**File References**: Valid for codebase as of commit `74be4a2`
