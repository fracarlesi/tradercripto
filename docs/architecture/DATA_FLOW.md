# Data Flow - From API to Frontend

Complete data flow from external sources to user interface.

## Source of Truth Hierarchy

```
┌─────────────────────────────────────────────┐
│  HYPERLIQUID DEX (Single Source of Truth)   │
│  • Balance, positions, orders, trades        │
│  • Market prices (468+ symbols)              │
│  • ALWAYS authoritative                      │
└──────────────────┬──────────────────────────┘
                   │ Sync every 30s
                   ▼
┌─────────────────────────────────────────────┐
│  PostgreSQL Database (Display Cache)        │
│  • Account metadata (AI config)             │
│  • Positions (cleared & recreated)          │
│  • Orders/Trades (append-only, deduplicated)│
│  • PortfolioSnapshot (5-min charts)         │
└──────────────────┬──────────────────────────┘
                   │ Real-time push
                   ▼
┌─────────────────────────────────────────────┐
│  WebSocket Server (ws_async.py)             │
│  • Push updates on every sync               │
│  • Broadcast to all connected clients       │
└──────────────────┬──────────────────────────┘
                   │ JSON messages
                   ▼
┌─────────────────────────────────────────────┐
│  React Frontend                              │
│  • Portfolio overview                        │
│  • Positions table                           │
│  • Order history                             │
│  • Asset curves                              │
└─────────────────────────────────────────────┘
```

## Data Types & Sources

| Data Type | Source | Update Frequency | Storage | File Reference |
|-----------|--------|------------------|---------|----------------|
| **Account Balance** | Hyperliquid API | Real-time (30s sync) | Not stored | `hyperliquid_sync_service.py:80` |
| **Positions** | Hyperliquid API | Real-time (30s sync) | Cleared & recreated | `hyperliquid_sync_service.py:120` |
| **Orders** | Hyperliquid API | Real-time (30s sync) | Deduplicated by order_id | `order_repo.py:45` |
| **Trades** | Hyperliquid API | Real-time (30s sync) | Deduplicated by trade_id | `trade_repo.py:50` |
| **Market Prices** | Hyperliquid API | Cached (30s TTL) | In-memory cache | `price_cache.py:20` |
| **Portfolio Snapshots** | Hyperliquid API | 5-minute intervals | Immutable history | `portfolio_snapshot_service.py:15` |
| **AI Decisions** | DeepSeek API + Local | Real-time | Database only | `ai_decision_service.py:150` |

## Real-Time Data Flow (30-second cycle)

**File**: `backend/services/trading/hyperliquid_sync_service.py:80-200`

```python
# 1. FETCH from Hyperliquid
user_state = await hyperliquid_trading_service.get_user_state_async()

# 2. UPDATE DATABASE (transaction)
async with db.begin_nested():
    # Update balance
    account.withdrawable = user_state['marginSummary']['withdrawable']

    # Clear & recreate positions (snapshot model)
    await PositionRepository.clear_positions(db, account_id)
    await PositionRepository.bulk_create_positions(db, account_id, positions)

    # Upsert orders/trades (deduplicate)
    for fill in fills:
        if not await OrderRepository.exists(db, fill['order_id']):
            await OrderRepository.create(db, fill)

# 3. BROADCAST via WebSocket
await websocket_manager.broadcast({
    "type": "sync_complete",
    "data": {
        "balance": account_value,
        "positions": [...],
        "orders": [...]
    }
})
```

## WebSocket Message Types

**File**: `backend/api/ws_async.py:100-289`

### Client → Server

```json
{"type": "get_snapshot"}  // Request full data snapshot
{"type": "ping"}           // Keepalive
```

### Server → Client

```json
// Full snapshot (on connect or request)
{
  "kind": "snapshot",
  "data": {
    "overview": {...},
    "positions": [...],
    "orders": [...],
    "trades": [...],
    "ai_decisions": [...]
  }
}

// Incremental update (after sync)
{
  "kind": "update",
  "data": {
    "balance": 10000.0,
    "positions": [...]
  }
}
```

## Anti-Patterns (DO NOT DO)

❌ **Reading balance from database**:
```python
# WRONG - Balance not stored in DB
balance = account.current_cash + account.frozen_cash
```

✅ **Reading balance from Hyperliquid**:
```python
# CORRECT - Always from API
user_state = await hyperliquid_trading_service.get_user_state_async()
balance = float(user_state['marginSummary']['accountValue'])
```

❌ **Using stale/fallback prices**:
```python
# WRONG - Shows wrong price
last_price = pos.get('markPx', entry_px)  # Fallback to entry!
```

✅ **Fetching current price**:
```python
# CORRECT - Real price or None
all_mids = await hyperliquid_trading_service.get_all_mids_async()
current_price = all_mids.get(coin)  # None if unavailable
```

## Related Documentation

- **[SYSTEM_ORCHESTRATION.md](SYSTEM_ORCHESTRATION.md)** - Complete operational flow
- **[OVERVIEW.md](OVERVIEW.md)** - Architecture and sync algorithm
- **[WEBSOCKET.md](../api/WEBSOCKET.md)** - WebSocket protocol details
