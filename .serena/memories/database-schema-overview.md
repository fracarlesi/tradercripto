# Database Schema Overview - HLQuantBot

## Location
- Schema: `/database/schema.sql`
- Python client: `/database/db.py`
- Docker: `/database/docker-compose.yml`
- Migrations: `/database/migrations/`

## Connessione

```python
from database.db import Database, get_database

db = await get_database()
await db.update_account(equity=..., ...)
await db.upsert_positions([...])
await db.insert_fill({...})
```

**Docker:**
```bash
cd database && docker compose up -d
```
- Host: `localhost:5432`
- User: `trader`
- Password: `trader_password`
- Database: `trading_db`

---

## Tabelle LIVE (sync real-time da Hyperliquid)

Queste tabelle vengono **SOVRASCRITTE** ad ogni sync.

| Tabella | Descrizione | Chiave | Note |
|---------|-------------|--------|------|
| `live_account` | Stato account (equity, balance, margin) | `id=1` | Singola riga |
| `realtime_account` | Stato account esteso per frontend | `id=1` | Include position_count, current_leverage |
| `live_positions` | Posizioni aperte correnti | `symbol` | LONG/SHORT, entry_price, unrealized_pnl |
| `live_orders` | Ordini aperti (pending) | `order_id` | BUY/SELL, price, reduce_only |

### Struttura live_account
```sql
equity, available_balance, margin_used, unrealized_pnl, updated_at
```

### Struttura live_positions
```sql
symbol, side, size, entry_price, mark_price, unrealized_pnl, 
leverage, liquidation_price, margin_used, updated_at
```

### Struttura live_orders
```sql
order_id, symbol, side, size, price, order_type, 
reduce_only, created_at, updated_at
```

---

## Tabelle STORICO (append-only)

Queste tabelle **crescono nel tempo** e non vengono modificate dopo inserimento.

| Tabella | Descrizione | Chiave | Note |
|---------|-------------|--------|------|
| `fills` | Fills da Hyperliquid | `fill_id` (tid) | Ogni esecuzione ordine |
| `trades` | Trade completi entry+exit | `trade_id` (UUID) | Aggregazione fills |
| `signals` | Segnali strategie | `signal_id` (UUID) | Entry/Exit/Scale |
| `daily_summary` | Riepilogo giornaliero | `date` | Performance daily |
| `agent_activity` | Log attività agenti | `id` (SERIAL) | Audit trail |
| `agent_decisions` | Decisioni AI agenti | `id` (UUID) | Input/reasoning/output |

### Struttura fills
```sql
fill_id, order_id, symbol, side, size, price, fee, fee_token,
fill_time, closed_pnl, is_maker, created_at
```

### Struttura trades
```sql
trade_id, symbol, side, size,
-- Entry
entry_price, entry_time, entry_fill_ids[],
-- Exit (NULL se aperto)
exit_price, exit_time, exit_fill_ids[],
-- PnL
gross_pnl, fees, net_pnl,
-- Metadata
strategy, duration_seconds, is_closed, notes
```

### Struttura signals
```sql
signal_id, timestamp, symbol, strategy, side, signal_type,
confidence, reason, executed, order_id, execution_price, rejected_reason
```

### Struttura agent_activity
```sql
id, agent_id, timestamp, activity_type, status, message, details (JSONB), symbol
```

### Struttura agent_decisions
```sql
id, agent_id, timestamp, decision_type, symbol,
input_data (JSONB), reasoning, output_data (JSONB),
action_taken, confidence, duration_ms
```

### Struttura daily_summary
```sql
date, starting_equity, ending_equity,
trades_count, win_count, loss_count,
gross_pnl, fees, net_pnl,
max_drawdown, max_equity, min_equity,
pnl_by_symbol (JSONB), pnl_by_strategy (JSONB)
```

---

## Viste

| Vista | Descrizione |
|-------|-------------|
| `open_trades` | Trade ancora aperti (`is_closed = FALSE`) |
| `recent_performance` | Ultimi 30 giorni con win_rate |
| `account_status` | Stato account con conteggio posizioni |

---

## Indici Principali

```sql
-- Fills
idx_fills_symbol, idx_fills_order_id, idx_fills_fill_time, idx_fills_symbol_time

-- Trades
idx_trades_symbol, idx_trades_strategy, idx_trades_entry_time, 
idx_trades_is_closed, idx_trades_symbol_closed

-- Signals
idx_signals_symbol, idx_signals_strategy, idx_signals_timestamp, idx_signals_executed

-- Agent Activity
idx_agent_activity_agent_id, idx_agent_activity_timestamp, 
idx_agent_activity_type, idx_agent_activity_agent_time

-- Agent Decisions
idx_agent_decisions_agent_id, idx_agent_decisions_timestamp,
idx_agent_decisions_symbol, idx_agent_decisions_agent_time, idx_agent_decisions_agent_symbol
```

---

## Migrations

| File | Descrizione |
|------|-------------|
| `001_optimization_tables.sql` | Tabelle ottimizzazione |
| `002_add_performance_metrics.sql` | Metriche performance |
| `003_add_multi_agent_tables.sql` | Tabelle multi-agent |
| `004_intelligent_bot.sql` | Tabelle bot intelligente |

---

## Trigger

- `update_trades_updated_at` - Aggiorna `updated_at` su trades
- `update_daily_summary_updated_at` - Aggiorna `updated_at` su daily_summary
