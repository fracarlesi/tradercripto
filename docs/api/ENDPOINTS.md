# REST API Endpoints

Complete REST API reference for the Bitcoin Trading System.

## Base URL

- **Development**: `http://localhost:5611/api`
- **Production**: `https://your-domain.com/api`

---

## Account Endpoints

### GET `/accounts`

List all accounts for current user.

**Response**:
```json
[
  {
    "id": 1,
    "name": "DeepSeek AI",
    "account_type": "AI",
    "model": "deepseek-chat",
    "is_active": true
  }
]
```

### GET `/accounts/{account_id}`

Get account details with portfolio overview.

**Response**:
```json
{
  "account": {
    "id": 1,
    "name": "DeepSeek AI"
  },
  "portfolio": {
    "positions_value": 8000.0,
    "positions_count": 2,
    "pending_orders": 0
  }
}
```

### PUT `/accounts/{account_id}`

Update account settings (AI model, API key).

**Request**:
```json
{
  "name": "New Name",
  "model": "deepseek-chat",
  "api_key": "sk-xxx"
}
```

---

## Position Endpoints

### GET `/positions`

Get all positions for account.

**Query Parameters**:
- `account_id`: Account ID (optional, defaults to active account)

**Response**:
```json
[
  {
    "symbol": "BTC",
    "quantity": 0.1,
    "average_price": 50000.0,
    "current_price": 51000.0,
    "unrealized_pnl": 100.0,
    "market_value": 5100.0
  }
]
```

---

## Order Endpoints

### GET `/orders`

Get order history.

**Query Parameters**:
- `account_id`: Account ID
- `status`: Filter by status (PENDING, FILLED, CANCELLED)
- `limit`: Max results (default: 100)

**Response**:
```json
[
  {
    "id": 1,
    "symbol": "BTC",
    "side": "BUY",
    "quantity": 0.1,
    "price": 50000.0,
    "status": "FILLED",
    "created_at": "2025-11-10T14:00:00Z"
  }
]
```

---

## Trade Endpoints

### GET `/trades`

Get trade execution history.

**Query Parameters**:
- `account_id`: Account ID
- `limit`: Max results (default: 100)

---

## Sync Endpoints

### POST `/sync/account/{account_id}`

Manually trigger account sync from Hyperliquid.

**Response**:
```json
{
  "success": true,
  "positions_synced": 2,
  "orders_synced": 5,
  "trades_synced": 10
}
```

### GET `/sync/status`

Get sync status for all accounts.

---

## AI Endpoints

### GET `/ai/usage`

Get AI API usage and cost tracking.

**Response**:
```json
{
  "today": {
    "calls": 120,
    "tokens": 150000,
    "cost": 0.75
  },
  "projections": {
    "monthly": 22.50,
    "yearly": 273.75
  }
}
```

---

## Health Endpoints

### GET `/health`

System health check.

**Response**: See [MONITORING.md](../operations/MONITORING.md#1-system-health-apihealth)

### GET `/readiness`

System readiness check.

---

## Config Endpoints

### GET `/config/scheduler-status`

Get scheduler status and job list.

**Response**:
```json
{
  "scheduler_running": true,
  "total_jobs": 8,
  "jobs": [
    {
      "id": "ai_crypto_trade",
      "function": "place_ai_driven_crypto_order",
      "next_run": "2025-11-10T14:30:00Z"
    }
  ]
}
```

---

## Error Responses

All endpoints return errors in this format:

```json
{
  "error": "Error Type",
  "detail": "Human-readable message",
  "request_id": "abc-123"
}
```

**HTTP Status Codes**:
- `200`: Success
- `400`: Bad Request (validation error)
- `404`: Not Found
- `500`: Internal Server Error
- `503`: Service Unavailable (system down)

---

## Related Documentation

- **[WEBSOCKET.md](WEBSOCKET.md)** - Real-time WebSocket protocol
- **[MONITORING.md](../operations/MONITORING.md)** - Health checks and debugging
