# WebSocket Protocol

Real-time data updates via WebSocket connection.

## Connection

### Endpoint

```
ws://localhost:5611/ws?user_id=1&account_id=1
```

**Query Parameters**:
- `user_id`: User ID (required)
- `account_id`: Account ID (required)

### JavaScript Client

```javascript
const ws = new WebSocket('ws://localhost:5611/ws?user_id=1&account_id=1');

ws.onopen = () => {
  console.log('Connected');
  ws.send(JSON.stringify({type: 'get_snapshot'}));
};

ws.onmessage = (event) => {
  const message = JSON.parse(event.data);
  console.log('Received:', message);
};

ws.onerror = (error) => {
  console.error('WebSocket error:', error);
};

ws.onclose = () => {
  console.log('Disconnected');
};
```

---

## Message Types

### Client → Server

#### 1. Request Snapshot

Get full data snapshot (all positions, orders, trades).

```json
{"type": "get_snapshot"}
```

#### 2. Ping (Keepalive)

Keep connection alive.

```json
{"type": "ping"}
```

**Response**:
```json
{"type": "pong"}
```

---

### Server → Client

#### 1. Snapshot (Full Data)

Sent on connect or when requested.

```json
{
  "kind": "snapshot",
  "data": {
    "overview": {
      "account": {
        "id": 1,
        "name": "DeepSeek AI",
        "account_type": "AI"
      },
      "portfolio": {
        "total_assets": 10000.0,
        "available_cash": 8000.0,
        "positions_value": 2000.0,
        "unrealized_pnl": 100.0
      }
    },
    "positions": [
      {
        "symbol": "BTC",
        "quantity": 0.1,
        "average_price": 50000.0,
        "current_price": 51000.0,
        "unrealized_pnl": 100.0,
        "market_value": 5100.0
      }
    ],
    "orders": [...],
    "trades": [...],
    "ai_decisions": [...]
  }
}
```

#### 2. Update (Incremental)

Sent after sync or trade execution.

```json
{
  "kind": "update",
  "data": {
    "balance": 9900.0,
    "positions": [
      {
        "symbol": "BTC",
        "quantity": 0.1,
        "unrealized_pnl": 200.0
      }
    ],
    "timestamp": "2025-11-10T14:30:00Z"
  }
}
```

#### 3. Error

Sent when error occurs.

```json
{
  "kind": "error",
  "message": "Failed to fetch data",
  "code": "SYNC_FAILED"
}
```

---

## Connection Lifecycle

### 1. Connection Established

```
Client → Server: WebSocket handshake
Server → Client: {"kind": "snapshot", "data": {...}}
```

### 2. Periodic Updates

```
[Every 30s - Hyperliquid Sync]
Server → All Clients: {"kind": "update", "data": {...}}
```

### 3. Manual Refresh

```
Client → Server: {"type": "get_snapshot"}
Server → Client: {"kind": "snapshot", "data": {...}}
```

### 4. Disconnection

```
Client disconnect detected
Server: Remove from active connections
```

---

## Update Triggers

WebSocket broadcasts triggered by:

1. **Hyperliquid Sync** (every 30s)
   - Balance updated
   - Positions changed
   - New orders/trades

2. **AI Trading** (every 3 min)
   - New order placed
   - Position opened/closed
   - AI decision logged

3. **Manual Actions**
   - User updates settings
   - User triggers manual sync

---

## Connection Management

### Heartbeat

Server sends ping every 30 seconds:

```json
{"type": "ping"}
```

Client should respond:

```json
{"type": "pong"}
```

If no response for 90 seconds → connection closed.

### Reconnection

Client should implement exponential backoff:

```javascript
let reconnectDelay = 1000; // Start at 1 second

function connect() {
  const ws = new WebSocket(url);

  ws.onclose = () => {
    console.log(`Reconnecting in ${reconnectDelay}ms`);
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 30000); // Max 30s
  };
}
```

---

## Error Handling

### Common Errors

**Invalid account_id**:
```json
{
  "kind": "error",
  "message": "Account not found",
  "code": "ACCOUNT_NOT_FOUND"
}
```

**Sync failure**:
```json
{
  "kind": "error",
  "message": "Failed to sync from Hyperliquid",
  "code": "SYNC_FAILED"
}
```

**Connection limit**:
```json
{
  "kind": "error",
  "message": "Too many connections",
  "code": "RATE_LIMITED"
}
```

---

## Performance

### Message Size

- **Snapshot**: ~50KB (100 positions + 100 orders + 100 trades)
- **Update**: ~5KB (delta changes only)

### Update Frequency

- **Normal**: Every 30s (sync interval)
- **High activity**: Every 3-10s (during active trading)

### Connection Limits

- **Max per account**: 10 concurrent connections
- **Max total**: 100 concurrent connections

---

## Implementation Reference

**File**: `backend/api/ws_async.py`

**Key Functions**:
- `websocket_endpoint()`: Connection handler (line 90-289)
- `send_portfolio_snapshot()`: Send full snapshot (line 215-289)
- `broadcast()`: Send update to all clients (line 120-150)

---

## Related Documentation

- **[ENDPOINTS.md](ENDPOINTS.md)** - REST API reference
- **[DATA_FLOW.md](../architecture/DATA_FLOW.md)** - Data flow architecture
- **[SYSTEM_ORCHESTRATION.md](../architecture/SYSTEM_ORCHESTRATION.md)** - System operations
