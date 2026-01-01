# HLQuantBot - Architettura Codebase

## Panoramica

Trading bot per **Hyperliquid DEX** con architettura modulare Python async basata su servizi pub/sub.

## Struttura Directory

```
trader_bitcoin/
├── database/              # PostgreSQL + asyncpg
│   ├── db.py              # Client Database asincrono
│   ├── schema.sql         # Schema completo
│   ├── docker-compose.yml # Docker PostgreSQL
│   └── migrations/        # Migrazioni SQL
├── frontend/              # Dashboard Flask + HTMX (esterna)
│   ├── main.py            # Server Flask
│   └── templates/         # Template Jinja2
└── simple_bot/            # Core trading bot
    ├── main.py            # Entry point - HLQuantBot orchestrator
    ├── bot.py             # SimpleBot - bot singolo simbolo
    ├── multi_bot.py       # MultiStrategyBot - orchestratore multi-strategia
    ├── strategies.py      # Strategie: Momentum, MeanReversion, Breakout
    ├── api/               # Client Hyperliquid
    ├── llm/               # Client DeepSeek per AI
    ├── services/          # Servizi pub/sub
    ├── optimization/      # Auto-ottimizzazione AI
    ├── config/            # Configurazione YAML
    ├── dashboard/         # Dashboard locale Flask
    └── tests/             # Test integration
```

## Entry Points

### 1. HLQuantBot (main.py) - Entry point principale
```bash
python -m simple_bot.main
python -m simple_bot.main -c path/to/config.yaml
```

**Classe `HLQuantBot`** orchestra:
- Configuration loading
- Database connection
- Message bus initialization
- LLM client (DeepSeek)
- Exchange client (Hyperliquid)
- Service lifecycle management

### 2. SimpleBot (bot.py) - Bot singolo simbolo
```bash
python -m simple_bot.bot
```

**Classe `SimpleBot`** - Bot trading per singolo simbolo:
- `fetch_prices()` - Recupera candele da API
- `sync_position()` - Sincronizza posizione da exchange
- `check_entries()` / `check_exits()` - Valuta segnali
- `open_position()` / `close_position()` - Esegue ordini

### 3. MultiStrategyBot (multi_bot.py) - Multi-strategia
**Classe `MultiStrategyBot`** orchestra:
- `SymbolScanner` - Scansiona e classifica simboli per volatilità/momentum
- `StrategyRunner` - Esegue strategie individuali
- Hot-reload config tramite `HotReloadConfigManager`

---

## Architettura Servizi (simple_bot/services/)

Sistema pub/sub con `MessageBus` per comunicazione tra servizi.

### BaseService (base.py)
Classe base astratta per tutti i servizi:
- Lifecycle: `start()`, `stop()`, `restart()`
- Health check: `health_check()`, `is_running`, `is_healthy`
- Pub/Sub: `publish()`, `subscribe()`
- Config hot-reload: `reload_config()`
- Retry logic con `RetryConfig`
- Logging centralizzato

### MessageBus (message_bus.py)
Broker pub/sub async per comunicazione inter-servizio:
- `Topic` - Enum dei topic
- `Message` - Payload messaggi
- `TopicStats` - Statistiche per topic

### Servizi Disponibili

| Servizio | Classe | Descrizione |
|----------|--------|-------------|
| **MarketScanner** | `MarketScannerService` | Scansiona mercato, raccoglie dati coins (spread, volume, funding) |
| **OpportunityRanker** | `OpportunityRankerService` | Classifica opportunità, calcola score, rileva regime |
| **StrategySelector** | `StrategySelectorService` | Seleziona strategia (regole o LLM), genera segnali |
| **CapitalAllocator** | `CapitalAllocatorService` | Sizing posizioni (Kelly, ATR, Risk Parity), gestisce rischio |
| **ExecutionEngine** | `ExecutionEngineService` | Esegue ordini su Hyperliquid, gestisce posizioni |
| **LearningModule** | `LearningModuleService` | Ottimizzazione continua (cicli orari/giornalieri), rollback |

### Flusso Dati Servizi

```
MarketScanner ──▶ [market_data] ──▶ OpportunityRanker
                                          │
                                   [opportunities]
                                          │
                                          ▼
                                   StrategySelector ──▶ [signals] ──▶ CapitalAllocator
                                                                            │
                                                                     [sized_signals]
                                                                            │
                                                                            ▼
                                                                     ExecutionEngine ──▶ [fills]
                                                                                            │
                                                                                            ▼
                                                                                     LearningModule
```

---

## API Client (simple_bot/api/)

### HyperliquidClient (hyperliquid.py)
Client async per Hyperliquid DEX:

**Market Data:**
- `get_all_markets()`, `get_market_summary()`
- `get_funding_rates()`, `get_open_interest()`
- `get_candles()`, `get_orderbook()`

**Account:**
- `get_account_state()`, `get_positions()`
- `get_open_orders()`, `get_fills()`

**Trading:**
- `place_order()`, `cancel_order()`, `cancel_all_orders()`
- `update_leverage()`, `close_position()`

Features:
- TTL cache per ridurre chiamate
- Retry automatico con backoff
- Rate limiting integrato

---

## LLM Client (simple_bot/llm/)

### DeepSeekClient (client.py)
Client per DeepSeek API con rate limiting giornaliero:

**Metodi:**
- `chat()` - Chat generica
- `select_strategy()` - Selezione strategia per opportunità
- `analyze_market()` - Analisi mercato

**Rate Limiting:**
- `RateLimiter` con limite giornaliero
- `remaining_requests`, `usage_pct`

**Tipi:**
- `StrategyDecision` - Output selezione strategia
- `MarketAnalysis` - Output analisi mercato
- `StrategyType`, `DirectionType` - Enum

---

## Strategie (simple_bot/strategies.py)

| Strategia | Logica | Indicatori |
|-----------|--------|------------|
| `MomentumStrategy` | Trend following | EMA crossover + RSI filter |
| `MeanReversionStrategy` | Contrarian | RSI extreme + Bollinger Bands |
| `BreakoutStrategy` | Breakout trading | High/Low + ATR volatility |

**Funzioni helper:**
- `calculate_ema()`, `calculate_sma()`, `calculate_rsi()`
- `calculate_bollinger_bands()`, `calculate_atr()`, `calculate_adx()`

Tutte implementano: `evaluate(prices) -> Signal(side, reason)`

---

## Ottimizzazione AI (simple_bot/optimization/)

| Componente | Descrizione |
|------------|-------------|
| `OptimizationOrchestrator` | Ciclo orario ottimizzazione |
| `HourlyMetricsCollector` | Raccoglie PnL, Sharpe, drawdown |
| `TieredSummarizer` | Aggrega dati per prompt LLM |
| `DeepSeekOptimizer` | Chiama DeepSeek per ottimizzare |
| `HotReloadConfigManager` | Applica config senza restart |
| `SafetyMonitor` | Rollback automatico se degrada |

---

## Frontend

### Dashboard Esterna (frontend/main.py)
Flask + HTMX con polling real-time su `http://localhost:5000`

**Endpoint principali:**
- `/` - Dashboard principale
- `/ui/overview` - Overview sistema
- `/ui/account` - Stato account
- `/ui/positions` - Posizioni aperte
- `/ui/orders` - Ordini pendenti
- `/ui/trades` - Storico trade
- `/ui/fills_raw` - Fill raw da exchange

**Endpoint Agenti:**
- `/ui/agent_orchestrator`, `/ui/agent_market_data`
- `/ui/agent_regime`, `/ui/agent_strategy_selector`
- `/ui/agent_strategies`, `/ui/agent_execution`
- `/ui/agent_capital_allocator`

**Endpoint Strategie:**
- `/ui/strategy_mmr_hft`, `/ui/strategy_micro_breakout`
- `/ui/strategy_pair_trading`, `/ui/strategy_liquidation_sniping`
- `/ui/strategy_momentum_scalping`

### Dashboard Locale (simple_bot/dashboard/)
Dashboard Flask leggera integrata nel bot:
- `/` - Index con overview
- `/positions`, `/signals`, `/opportunities`
- `/performance`, `/services`, `/learning`

---

## Configurazione

**File config:**
- `simple_bot/config.yaml` - Config singolo bot
- `simple_bot/multi_config.yaml` - Config multi-strategia
- `simple_bot/config/intelligent_bot.yaml` - Config bot intelligente

**Parametri chiave:**
- `position_size_usd`, `leverage` - Sizing
- `tp_pct`, `sl_pct`, `trailing_stop_pct` - Risk management
- Parametri per-strategia

---

## Database

```python
from database.db import Database, get_database

db = await get_database()
await db.update_account(equity=..., ...)
await db.upsert_positions([...])
```

**Docker:**
```bash
cd database && docker compose up -d
```
- Host: localhost:5432
- User: trader / Password: trader_password
- Database: trading_db

Vedi memoria `database-schema-overview` per dettagli tabelle.
