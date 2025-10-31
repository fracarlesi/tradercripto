# Trading Bot Architecture

## Data Source Strategy

### Hyperliquid as Single Source of Truth ✅

**Principle**: Hyperliquid is the authoritative source for all trading data. The local database acts as a **read-only cache** synchronized from Hyperliquid.

**Trading Mode**: Real trading only - paper trading has been removed from the codebase.

## Data Flow

### AI-Driven Real Trading Flow

```
AI Decision → Place Order on Hyperliquid → Immediate Sync → Database Updated
                                                ↓
                                        Periodic Sync (60s)
```

**Steps**:
1. AI makes trading decision (saved in `ai_decision_logs` table - local only)
2. Order placed directly on Hyperliquid via `hyperliquid_trading_service.place_market_order()`
3. **Immediate sync** after successful trade via `sync_account_to_database()`
4. Database gets populated from Hyperliquid fills
5. **Periodic sync** (every 60s) ensures consistency

**Key Points**:
- ✅ NO local order creation - orders go directly to Hyperliquid
- ✅ Database is populated ONLY from Hyperliquid sync
- ✅ Hyperliquid is source of truth for: balance, positions, orders, fills
- ✅ All trading is real - no simulation mode

## Database Tables

### Tables Synced from Hyperliquid:
- `accounts.current_cash` - Available balance
- `accounts.frozen_cash` - Margin used
- `positions` - Open positions (CLEARED and RECREATED on each sync)
- `orders` - Historical orders (synced from fills)
- `trades` - Historical trades (synced from fills)

### Local-Only Tables:
- `ai_decision_logs` - AI trading decisions and reasoning
- `crypto_prices` - Price history cache
- `crypto_klines` - OHLCV data cache

## Synchronization Services

### 1. Periodic Sync (Every 60s)
**Service**: `hyperliquid_sync_service.sync_all_active_accounts()`

**What it does**:
- Fetches account state from Hyperliquid
- Updates balance (current_cash, frozen_cash)
- **CLEARS** all crypto positions
- **RECREATES** positions from Hyperliquid
- Syncs recent fills (last 100)

**Location**: `services/hyperliquid_sync_service.py`

### 2. Post-Trade Sync (Immediate)
**Service**: `_sync_balance_from_hyperliquid()`

**What it does**:
- Same as periodic sync but triggered immediately after real trade
- Ensures database reflects latest state after order execution

**Location**: `services/trading_commands.py:65`

## Order Status Mapping

### Hyperliquid → Database:
- Hyperliquid Fill → Database Order with status `FILLED` (⚠️ was incorrectly set to `EXECUTED` before fix)
- `filled_quantity` = `quantity` for filled orders

### Important:
- Orders synced from Hyperliquid are **already executed fills**
- They should ALWAYS have status = `FILLED`, not `EXECUTED`
- Open orders on Hyperliquid are NOT synced to database (they're live on exchange)

## Configuration

### Environment Variables:
```bash
HYPERLIQUID_PRIVATE_KEY=0x...        # Wallet private key (required)
HYPERLIQUID_WALLET_ADDRESS=0x...     # Wallet address (optional)
MAX_CAPITAL_USD=53.0                 # Maximum capital to use for trading
```

**Note**: `ENABLE_REAL_TRADING` has been removed - all trading is real.

## Best Practices

### ✅ DO:
1. Always read trading data from Hyperliquid when possible
2. Trust Hyperliquid balance over local database
3. Use database as display cache for frontend
4. Let periodic sync maintain database consistency
5. Store only AI decisions and analysis locally

### ❌ DON'T:
1. Create orders in database - they must go to Hyperliquid
2. Modify positions directly in database
3. Trust local balance if it differs from Hyperliquid
4. Use database as primary state - Hyperliquid is the source of truth

## Removed Features

### Paper Trading
- **Status**: Completely removed from codebase
- **Reason**: Simplified architecture, all trading is real
- **Removed files**: `services/order_matching.py`
- **Removed functions**: `place_random_crypto_order()`, `create_order()`, `check_and_execute_order()`

## Troubleshooting

### Balance Mismatch:
```bash
# Check sync status
python check_sync_status.py

# Force sync
python sync_balance.py
```

### Order Status Issues:
- All filled orders from Hyperliquid should have status `FILLED`
- If orders show as `EXECUTED`, run `python fix_order_status.py`

## Files Reference

### Core Services:
- `services/hyperliquid_trading_service.py` - Hyperliquid API wrapper
- `services/hyperliquid_sync_service.py` - Periodic sync orchestration
- `services/trading_commands.py` - AI trading commands
- `services/scheduler.py` - Task scheduling (60s sync interval)

### Utilities:
- `check_sync_status.py` - Verify sync status
- `fix_order_status.py` - Fix incorrect order statuses
- `sync_balance.py` - Manual balance sync
