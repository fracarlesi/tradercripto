# Data Model: Production-Ready Bitcoin Trading System

**Feature**: 001-production-refactor
**Date**: 2025-10-31
**Purpose**: Define entity relationships, validation rules, and synchronization strategy

## Overview

This document defines the data model for the production trading system. The model is split into two categories:
1. **Synced Entities**: Data synchronized from Hyperliquid (authoritative source)
2. **Local Entities**: Data stored only in local database (audit trails, cache)

**Key Principle**: Hyperliquid is the single source of truth for all trading data (balances, positions, orders, trades).

---

## Entity Relationship Diagram

```
┌─────────────┐         ┌─────────────┐
│    User     │────────<│   Account   │
│  (Local)    │   1:N   │  (Synced)   │
└─────────────┘         └──────┬──────┘
                               │ 1:N
                               │
           ┌───────────────────┼───────────────────┐
           │                   │                   │
           ▼                   ▼                   ▼
    ┌──────────┐        ┌──────────┐      ┌──────────────┐
    │ Position │        │  Order   │      │AIDecisionLog │
    │ (Synced) │        │ (Synced) │      │   (Local)    │
    └──────────┘        └────┬─────┘      └──────────────┘
                             │ 1:N
                             ▼
                        ┌──────────┐
                        │  Trade   │
                        │ (Synced) │
                        └──────────┘

Cache Entities (Local):
┌──────────────┐    ┌──────────────┐
│ CryptoKline  │    │ CryptoPrice  │
│   (Local)    │    │   (Local)    │
└──────────────┘    └──────────────┘
```

---

## Core Entities

### 1. User (Local Only)

**Purpose**: Represents system user (currently single-user system).

**Table**: `users`

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `id` | INTEGER | PRIMARY KEY, AUTO_INCREMENT | Unique user identifier |
| `username` | VARCHAR(50) | NOT NULL, UNIQUE | Username (default: "default") |
| `email` | VARCHAR(255) | NULLABLE, UNIQUE | Email address (optional for single-user) |
| `password_hash` | VARCHAR(255) | NULLABLE | Password hash (not used in single-user mode) |
| `is_active` | VARCHAR(10) | NOT NULL, DEFAULT 'true' | Active status ('true'/'false' string) |
| `created_at` | DATETIME | NOT NULL, DEFAULT NOW() | Account creation timestamp |

**Validation Rules**:
- Username must be unique and non-empty
- Only one user with `username='default'` should exist in single-user mode
- Email must be valid format if provided

**Sync Strategy**: NOT SYNCED - local entity only

---

### 2. Account (Synced + Local Config)

**Purpose**: Represents trading account with AI model configuration and balance information.

**Table**: `accounts`

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `id` | INTEGER | PRIMARY KEY, AUTO_INCREMENT | Unique account identifier |
| `user_id` | INTEGER | FOREIGN KEY → users(id), NOT NULL | Owner user |
| `version` | VARCHAR(10) | NOT NULL | Configuration version (e.g., "v1") |
| `name` | VARCHAR(100) | NOT NULL | Account display name (e.g., "DeepSeek") |
| `account_type` | VARCHAR(20) | NOT NULL | Account type: "AI" or "Manual" |
| `model` | VARCHAR(50) | NULLABLE | AI model name (e.g., "deepseek-chat") |
| `base_url` | VARCHAR(255) | NULLABLE | AI API base URL |
| `api_key` | VARCHAR(255) | NULLABLE | AI API key (encrypted in production) |
| `initial_capital` | DECIMAL(20,8) | NOT NULL, DEFAULT 0.0 | Starting capital (USD) |
| `current_cash` | DECIMAL(20,8) | NOT NULL, DEFAULT 0.0 | **SYNCED** Available balance (USD) |
| `frozen_cash` | DECIMAL(20,8) | NOT NULL, DEFAULT 0.0 | **SYNCED** Margin used / frozen (USD) |
| `is_active` | VARCHAR(10) | NOT NULL, DEFAULT 'true' | Active status ('true'/'false') |
| `created_at` | DATETIME | NOT NULL, DEFAULT NOW() | Account creation timestamp |
| `updated_at` | DATETIME | NOT NULL, DEFAULT NOW() | Last update timestamp |

**Validation Rules**:
- `account_type` must be one of: ['AI', 'Manual']
- `current_cash` >= 0 (cannot go negative)
- `frozen_cash` >= 0 (cannot go negative)
- `current_cash + frozen_cash` should approximately equal total equity
- If `account_type='AI'`, then `model`, `base_url`, `api_key` are required

**Sync Strategy**:
- **Synced Fields**: `current_cash`, `frozen_cash` (from Hyperliquid `assetPositions` and `marginSummary`)
- **Local Fields**: `name`, `model`, `base_url`, `api_key`, `initial_capital`, `is_active`
- **Sync Frequency**: Every 30 seconds (FR-004)
- **Sync Method**: Fetch from Hyperliquid API, overwrite local values (FR-005: Hyperliquid-wins policy)

**State Transitions**:
```
CREATED → (sync) → ACTIVE (has balance) → (trading) → ACTIVE (balance changes)
                                       ↓
                                   INACTIVE (is_active='false')
```

---

### 3. Position (Fully Synced)

**Purpose**: Represents current open trading position for a symbol.

**Table**: `positions`

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `id` | INTEGER | PRIMARY KEY, AUTO_INCREMENT | Unique position identifier |
| `account_id` | INTEGER | FOREIGN KEY → accounts(id), NOT NULL | Account owning position |
| `symbol` | VARCHAR(20) | NOT NULL | Trading symbol (e.g., "BTC", "ETH") |
| `quantity` | DECIMAL(20,8) | NOT NULL | Total position size (positive=long, negative=short) |
| `available_quantity` | DECIMAL(20,8) | NOT NULL | Available quantity (not in pending orders) |
| `average_cost` | DECIMAL(20,8) | NOT NULL | Average entry price (USD) |
| `created_at` | DATETIME | NOT NULL, DEFAULT NOW() | Position opened timestamp |
| `updated_at` | DATETIME | NOT NULL, DEFAULT NOW() | Last sync timestamp |

**Validation Rules**:
- `quantity` can be positive (long) or negative (short)
- `available_quantity` <= `abs(quantity)` (can't have more available than total)
- `average_cost` > 0 (must have valid entry price)
- Unique constraint: (account_id, symbol) - one position per symbol per account

**Sync Strategy** (FR-009):
- **Complete replacement on each sync**: DELETE all positions for account → INSERT from Hyperliquid
- **Sync Source**: Hyperliquid `assetPositions` array
- **Rationale**: Hyperliquid tracks positions, we don't create them locally
- **Idempotency**: Same API response always produces same database state

**Sync Algorithm**:
```python
async def sync_positions(db: AsyncSession, account: Account, hyperliquid_positions: List[dict]):
    # Step 1: Delete all existing positions for account
    await db.execute(delete(Position).where(Position.account_id == account.id))

    # Step 2: Insert fresh positions from Hyperliquid
    for pos in hyperliquid_positions:
        new_position = Position(
            account_id=account.id,
            symbol=pos['coin'],
            quantity=Decimal(pos['szi']),  # Size with sign (+long, -short)
            available_quantity=Decimal(pos['szi']),
            average_cost=Decimal(pos['entryPx']),
        )
        db.add(new_position)

    await db.commit()
```

**State Transitions**:
```
[No Position] → (trade executed on Hyperliquid) → [Position Opened]
              ← (sync deletes)                   ← (position closed on Hyperliquid)
```

---

### 4. Order (Synced from Fills)

**Purpose**: Historical record of trading orders (filled orders from Hyperliquid).

**Table**: `orders`

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `id` | INTEGER | PRIMARY KEY, AUTO_INCREMENT | Unique order identifier |
| `account_id` | INTEGER | FOREIGN KEY → accounts(id), NOT NULL | Account that placed order |
| `order_no` | VARCHAR(50) | NOT NULL, UNIQUE | Hyperliquid order ID |
| `symbol` | VARCHAR(20) | NOT NULL | Trading symbol |
| `side` | VARCHAR(10) | NOT NULL | "BUY" or "SELL" |
| `order_type` | VARCHAR(20) | NOT NULL | Order type (e.g., "MARKET", "LIMIT") |
| `price` | DECIMAL(20,8) | NOT NULL | Order price (execution price for fills) |
| `quantity` | DECIMAL(20,8) | NOT NULL | Order quantity (total) |
| `filled_quantity` | DECIMAL(20,8) | NOT NULL | Filled quantity (= quantity for synced fills) |
| `status` | VARCHAR(20) | NOT NULL | Order status (always "FILLED" for synced orders) |
| `created_at` | DATETIME | NOT NULL | Order creation time (fill time from Hyperliquid) |
| `updated_at` | DATETIME | NOT NULL, DEFAULT NOW() | Last update timestamp |

**Validation Rules**:
- `side` must be one of: ['BUY', 'SELL']
- `order_type` must be one of: ['MARKET', 'LIMIT', 'STOP', 'STOP_LIMIT']
- `status` must be one of: ['PENDING', 'FILLED', 'CANCELLED', 'REJECTED']
- `price` > 0
- `quantity` > 0
- `filled_quantity` >= 0 and `filled_quantity` <= `quantity`
- For synced orders: `status = 'FILLED'` and `filled_quantity = quantity`

**Sync Strategy** (FR-010):
- **Sync Source**: Hyperliquid `userFills` endpoint (last 100 fills)
- **Deduplication**: Use `order_no` (Hyperliquid order ID) as unique identifier
- **Idempotency**: If order with same `order_no` exists, skip (don't update)
- **Status Mapping**: All synced fills map to `status='FILLED'` (not 'EXECUTED')

**Sync Algorithm**:
```python
async def sync_orders_from_fills(db: AsyncSession, account: Account, fills: List[dict]):
    for fill in fills:
        # Check if order already exists
        existing = await db.execute(
            select(Order).where(Order.order_no == fill['oid'])
        )
        if existing.scalar_one_or_none():
            continue  # Skip duplicates

        # Create order from fill
        order = Order(
            account_id=account.id,
            order_no=fill['oid'],
            symbol=fill['coin'],
            side='BUY' if fill['side'] == 'B' else 'SELL',
            order_type='MARKET',  # Hyperliquid fills don't specify type
            price=Decimal(fill['px']),
            quantity=Decimal(fill['sz']),
            filled_quantity=Decimal(fill['sz']),  # Always fully filled
            status='FILLED',
            created_at=datetime.fromtimestamp(fill['time'] / 1000),  # Convert ms to datetime
        )
        db.add(order)

    await db.commit()
```

**State Transitions**:
```
[Fill on Hyperliquid] → (sync) → [Order FILLED] → (never changes)
```

---

### 5. Trade (Synced from Fills)

**Purpose**: Individual trade execution record (multiple trades can belong to one order).

**Table**: `trades`

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `id` | INTEGER | PRIMARY KEY, AUTO_INCREMENT | Unique trade identifier |
| `account_id` | INTEGER | FOREIGN KEY → accounts(id), NOT NULL | Account that executed trade |
| `order_id` | INTEGER | FOREIGN KEY → orders(id), NULLABLE | Linked order (if known) |
| `symbol` | VARCHAR(20) | NOT NULL | Trading symbol |
| `side` | VARCHAR(10) | NOT NULL | "BUY" or "SELL" |
| `price` | DECIMAL(20,8) | NOT NULL | Execution price |
| `quantity` | DECIMAL(20,8) | NOT NULL | Quantity traded |
| `commission` | DECIMAL(20,8) | NOT NULL, DEFAULT 0.0 | Trading commission (USD) |
| `trade_time` | DATETIME | NOT NULL | Trade execution time |

**Validation Rules**:
- `side` must be one of: ['BUY', 'SELL']
- `price` > 0
- `quantity` > 0
- `commission` >= 0
- `trade_time` <= NOW() (can't be in future)

**Sync Strategy**:
- **Sync Source**: Hyperliquid `userFills` endpoint (last 100 fills)
- **Deduplication**: Use composite key (trade_time, symbol, side, quantity, price) to detect duplicates
- **Link to Order**: Match by `order_no` from fill data to link `order_id`

**Sync Algorithm**:
```python
async def sync_trades_from_fills(db: AsyncSession, account: Account, fills: List[dict]):
    for fill in fills:
        # Calculate unique identifier
        trade_time = datetime.fromtimestamp(fill['time'] / 1000)
        trade_key = (
            trade_time,
            fill['coin'],
            fill['side'],
            Decimal(fill['sz']),
            Decimal(fill['px']),
        )

        # Check if trade exists (approximate matching)
        existing = await db.execute(
            select(Trade).where(
                Trade.trade_time == trade_time,
                Trade.symbol == fill['coin'],
                Trade.quantity == Decimal(fill['sz']),
                Trade.price == Decimal(fill['px']),
            )
        )
        if existing.scalar_one_or_none():
            continue  # Skip duplicate

        # Find linked order
        order = await db.execute(
            select(Order).where(Order.order_no == fill['oid'])
        )
        order_obj = order.scalar_one_or_none()

        # Create trade
        trade = Trade(
            account_id=account.id,
            order_id=order_obj.id if order_obj else None,
            symbol=fill['coin'],
            side='BUY' if fill['side'] == 'B' else 'SELL',
            price=Decimal(fill['px']),
            quantity=Decimal(fill['sz']),
            commission=Decimal(fill['fee']) if 'fee' in fill else Decimal(0),
            trade_time=trade_time,
        )
        db.add(trade)

    await db.commit()
```

---

## Local-Only Entities

### 6. AIDecisionLog (Local Only)

**Purpose**: Audit trail of AI trading decisions and reasoning.

**Table**: `ai_decision_logs`

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `id` | INTEGER | PRIMARY KEY, AUTO_INCREMENT | Unique log identifier |
| `account_id` | INTEGER | FOREIGN KEY → accounts(id), NOT NULL | Account that made decision |
| `decision_time` | DATETIME | NOT NULL | When decision was made |
| `reason` | TEXT | NOT NULL | AI explanation for decision |
| `operation` | VARCHAR(10) | NOT NULL | Decision: "BUY", "SELL", "HOLD" |
| `symbol` | VARCHAR(20) | NOT NULL | Symbol to trade |
| `prev_portion` | DECIMAL(5,4) | NOT NULL | Previous portfolio % allocation |
| `target_portion` | DECIMAL(5,4) | NOT NULL | Target portfolio % allocation |
| `total_balance` | DECIMAL(20,8) | NOT NULL | Total account balance at decision time |
| `executed` | BOOLEAN | NOT NULL, DEFAULT FALSE | Whether decision was executed |
| `order_id` | INTEGER | FOREIGN KEY → orders(id), NULLABLE | Linked order (if executed) |

**Validation Rules**:
- `operation` must be one of: ['BUY', 'SELL', 'HOLD']
- `prev_portion` >= 0 and `prev_portion` <= 1.0 (0-100%)
- `target_portion` >= 0 and `target_portion` <= 1.0 (0-100%)
- `total_balance` >= 0
- `decision_time` <= NOW()

**Sync Strategy**: NOT SYNCED - local audit trail only

**Retention Policy** (Future): Archive logs older than 90 days to reduce database size

---

### 7. CryptoKline (Local Cache)

**Purpose**: OHLCV candlestick data for chart display and technical analysis.

**Table**: `crypto_klines`

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `id` | INTEGER | PRIMARY KEY, AUTO_INCREMENT | Unique kline identifier |
| `symbol` | VARCHAR(20) | NOT NULL | Trading symbol |
| `period` | VARCHAR(10) | NOT NULL | Timeframe (e.g., "1m", "5m", "1h", "1d") |
| `timestamp` | DATETIME | NOT NULL | Kline start time |
| `open` | DECIMAL(20,8) | NOT NULL | Opening price |
| `high` | DECIMAL(20,8) | NOT NULL | High price |
| `low` | DECIMAL(20,8) | NOT NULL | Low price |
| `close` | DECIMAL(20,8) | NOT NULL | Closing price |
| `volume` | DECIMAL(20,8) | NOT NULL | Trading volume |
| `amount` | DECIMAL(20,8) | NOT NULL | Trading amount (USD) |

**Validation Rules**:
- `period` must be one of: ['1m', '5m', '15m', '30m', '1h', '4h', '1d', '1w']
- `high` >= `low` (high cannot be lower than low)
- `high` >= `open` and `high` >= `close`
- `low` <= `open` and `low` <= `close`
- `volume` >= 0, `amount` >= 0
- Unique constraint: (symbol, period, timestamp)

**Sync Strategy**: NOT SYNCED - fetched from market data APIs (CCXT or similar)

---

### 8. CryptoPrice (Local Cache)

**Purpose**: Daily price snapshot for simplified portfolio valuation.

**Table**: `crypto_prices`

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `id` | INTEGER | PRIMARY KEY, AUTO_INCREMENT | Unique price identifier |
| `symbol` | VARCHAR(20) | NOT NULL | Trading symbol |
| `price` | DECIMAL(20,8) | NOT NULL | Daily closing price (USD) |
| `price_date` | DATE | NOT NULL | Price date |

**Validation Rules**:
- `price` > 0
- `price_date` <= TODAY()
- Unique constraint: (symbol, price_date)

**Sync Strategy**: NOT SYNCED - fetched from market data APIs

---

## Synchronization Strategy

### Sync Flow Overview

```
┌─────────────────┐
│  Hyperliquid    │
│  (Source of     │
│   Truth)        │
└────────┬────────┘
         │
         │ Periodic Sync (30s interval)
         │ or Immediate Sync (post-trade)
         │
         ▼
┌─────────────────┐
│ Sync Service    │
│ (Orchestrator)  │
└────────┬────────┘
         │
         │ 1. Fetch account state
         │ 2. Fetch positions
         │ 3. Fetch last 100 fills
         │
         ▼
┌─────────────────┐
│ Database        │
│ (Read-Only      │
│  Cache)         │
└─────────────────┘
```

### Sync Operations (FR-004 to FR-011)

#### 1. Account Balance Sync
- **Frequency**: Every 30 seconds + immediate post-trade
- **API**: Hyperliquid `user_state` endpoint
- **Fields Updated**: `current_cash`, `frozen_cash`
- **Atomicity**: Single transaction per account

#### 2. Position Sync (Clear + Recreate)
- **Frequency**: Every 30 seconds
- **API**: Hyperliquid `assetPositions` in user_state
- **Strategy**: DELETE all positions for account → INSERT fresh from API
- **Rationale**: Hyperliquid tracks positions, not us. Always trust API state.

#### 3. Order + Trade Sync (Historical Fills)
- **Frequency**: Every 30 seconds
- **API**: Hyperliquid `userFills` endpoint (last 100 fills)
- **Deduplication**: Skip if `order_no` already exists (orders), or composite key match (trades)
- **Idempotency**: Running sync multiple times produces same database state

### Error Handling (FR-008, FR-022 to FR-027)

#### Sync Failures
1. **Network timeout / API 503**:
   - Log at WARNING level with context
   - Retry with exponential backoff: 1s, 2s, 4s, 8s, 16s (max 5 attempts)
   - Serve stale cached data to frontend with staleness indicator
   - Increment failure counter for monitoring

2. **Data conflict** (order exists locally but not on Hyperliquid):
   - Apply Hyperliquid-wins policy: Archive conflicting local record
   - Create audit log entry with details
   - Notify operator via logging (WARNING level)

3. **Database transaction failure**:
   - Rollback entire sync operation (atomic transaction)
   - Log at ERROR level with stack trace
   - Retry sync on next cycle (30s later)

#### Circuit Breaker (FR-024)
- **Trigger**: 5 consecutive sync failures
- **Action**: Open circuit, stop sync attempts for 60 seconds
- **Half-Open**: After 60s, attempt single sync. If success, close circuit. If fail, re-open.
- **Monitoring**: Alert if circuit opens (indicates Hyperliquid API issues or network problems)

### Data Consistency Guarantees

1. **Atomicity (FR-007)**: Each sync operation runs in single database transaction - all or nothing
2. **Idempotency (FR-006)**: Same API response always produces same database state (deduplication)
3. **Hyperliquid-Wins (FR-005)**: In any conflict, Hyperliquid data overwrites local data
4. **Eventual Consistency**: Database eventually reflects Hyperliquid state within 30s + retry window

---

## Database Indexes

### Performance Indexes

```sql
-- Account lookups
CREATE INDEX idx_accounts_user_active ON accounts(user_id, is_active);
CREATE INDEX idx_accounts_type ON accounts(account_type);

-- Position lookups
CREATE INDEX idx_positions_account ON positions(account_id);
CREATE UNIQUE INDEX idx_positions_account_symbol ON positions(account_id, symbol);

-- Order queries
CREATE INDEX idx_orders_account ON orders(account_id);
CREATE INDEX idx_orders_status ON orders(status);
CREATE INDEX idx_orders_created ON orders(created_at DESC);
CREATE UNIQUE INDEX idx_orders_order_no ON orders(order_no);

-- Trade queries
CREATE INDEX idx_trades_account ON trades(account_id);
CREATE INDEX idx_trades_order ON trades(order_id);
CREATE INDEX idx_trades_time ON trades(trade_time DESC);
CREATE INDEX idx_trades_dedup ON trades(trade_time, symbol, quantity, price);

-- AI decision logs
CREATE INDEX idx_ai_logs_account ON ai_decision_logs(account_id);
CREATE INDEX idx_ai_logs_time ON ai_decision_logs(decision_time DESC);

-- Kline cache
CREATE UNIQUE INDEX idx_klines_unique ON crypto_klines(symbol, period, timestamp);
CREATE INDEX idx_klines_lookup ON crypto_klines(symbol, period, timestamp DESC);

-- Price cache
CREATE UNIQUE INDEX idx_prices_unique ON crypto_prices(symbol, price_date);
CREATE INDEX idx_prices_lookup ON crypto_prices(symbol, price_date DESC);
```

---

## Migration Notes

### SQLite → PostgreSQL Differences

| Feature | SQLite Behavior | PostgreSQL Behavior | Migration Action |
|---------|----------------|---------------------|------------------|
| `DECIMAL` | Stored as TEXT or REAL | Native DECIMAL(20,8) | Verify precision during migration |
| `DATETIME` | Stored as TEXT/INTEGER | Native TIMESTAMP | Convert format if needed |
| Boolean | INTEGER (0/1) | Native BOOLEAN | Convert `is_active` string → boolean |
| Auto-increment | `AUTOINCREMENT` | `SERIAL` or `IDENTITY` | Alembic handles automatically |
| Foreign Keys | Disabled by default | Enforced by default | Test cascade deletes |

### Data Validation Post-Migration

```sql
-- Verify row counts match
SELECT 'accounts', COUNT(*) FROM accounts UNION
SELECT 'positions', COUNT(*) FROM positions UNION
SELECT 'orders', COUNT(*) FROM orders UNION
SELECT 'trades', COUNT(*) FROM trades;

-- Check for orphaned records (broken foreign keys)
SELECT * FROM positions WHERE account_id NOT IN (SELECT id FROM accounts);
SELECT * FROM orders WHERE account_id NOT IN (SELECT id FROM accounts);
SELECT * FROM trades WHERE order_id IS NOT NULL AND order_id NOT IN (SELECT id FROM orders);

-- Verify balance integrity
SELECT id, name, current_cash, frozen_cash, (current_cash + frozen_cash) AS total_equity
FROM accounts WHERE is_active = 'true';
```

---

## Summary

**Total Entities**: 8 (5 synced, 3 local)

**Sync Strategy**:
- **Hyperliquid-Wins**: Always trust API data over local data
- **Positions**: Complete replacement (clear + recreate)
- **Orders/Trades**: Incremental sync with deduplication
- **Frequency**: 30 seconds periodic + immediate post-trade

**Data Integrity**:
- Atomic transactions (all-or-nothing sync)
- Idempotent operations (safe to retry)
- Foreign key constraints enforced (PostgreSQL)
- Comprehensive indexes for performance

**Next Steps**: Proceed to API contract generation (Phase 1 continued)
