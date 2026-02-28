# HLQuantBot - Claude Code Configuration

> Trading bot for Hyperliquid DEX - All assets, EMA Momentum strategy, live on mainnet

## Startup: Activate Serena

All'inizio di ogni conversazione, attiva il progetto Serena con:
```
mcp__plugin_serena_serena__activate_project(project="trader_bitcoin")
```

## Workflow: Teammate Agents + Background

- Usa SEMPRE i teammate agents specializzati (Task tool) in parallelo dove possibile.
- Lancia SEMPRE i task in **background** (`run_in_background: true`) così l'utente può continuare a lavorare.
- Non bloccare mai la conversazione aspettando un agent - lancia in background e rispondi subito.
- Agents: hlquantbot-debugger, hyperliquid-trade-verifier, trading-code-reviewer, hlquantbot-developer.

---

## Current Strategy: Trend Momentum (EMA Crossover)

| Aspect | Configuration |
|--------|---------------|
| **Universe** | ALL assets su Hyperliquid (`mode: "all"`) |
| **Timeframe** | 15m (scan every 5 min) |
| **Strategy** | `trend_momentum` - EMA9/EMA21 crossover + RSI filter |
| **Entry LONG** | EMA9 > EMA21, RSI 30-65, regime TREND (ADX>25) |
| **Entry SHORT** | EMA9 < EMA21, RSI 35-70 (enabled) |
| **TP / SL** | 1.6% / 0.8% (1:2 R:R) |
| **Leverage** | 10x |
| **Max Positions** | 3 concurrent |
| **LLM Veto** | Enabled (DeepSeek, max 20 calls/day) |
| **Mode** | **LIVE mainnet** (`dry_run: false`) |
| **Dashboard** | Rimossa (commit 3afca98) |

### Key Files

| File | Purpose |
|------|---------|
| `strategies/momentum_scalper.py` | Trend momentum strategy (EMA9/21 + RSI + ATR) |
| `services/market_state.py` | Indicator calculation |
| `services/execution_engine.py` | Order execution + TP/SL |
| `services/risk_manager.py` | Position sizing + in-memory trade counter |
| `services/kill_switch.py` | Circuit breaker |
| `services/llm_veto.py` | DeepSeek LLM confirmation/veto |
| `config/trading.yaml` | Primary configuration |
| `main.py` | Entry point, orchestration |

---

## Quick Start Commands

| Comando | Descrizione |
|---------|-------------|
| `/test` | Esegui test suite |
| `/lint` | Type-check e linting |
| `/review` | Code review con trading-code-reviewer agent |
| `/start-bot` | Avvia il trading bot |

---

## Project Structure

```
crypto_bot/
├── config/           # trading.yaml (primary config)
├── core/             # models.py (MarketState, Regime, Direction)
├── services/         # risk_manager, execution_engine, market_state, llm_veto, kill_switch
├── strategies/       # momentum_scalper (active), trend_follow (disabled)
├── tests/            # pytest test suite
└── main.py           # Entry point
```

---

## Development Commands

### Testing
```bash
cd crypto_bot && python3 -m pytest tests/ -v
cd crypto_bot && python3 -m pytest tests/ --cov=. --cov-report=html
```

### Code Quality
```bash
pyright crypto_bot/                 # Type checking (from project root)
cd crypto_bot && ruff check .       # Linting
cd crypto_bot && black .            # Formatting
```

### Running
```bash
cd crypto_bot && python3 main.py    # Bot (live)
```

---

## Deploy Workflow: Locale -> Hetzner

1. **Sviluppo locale**: modifica codice
2. **Test**: `cd crypto_bot && python3 -m pytest tests/ -v`
3. **Lint**: `pyright crypto_bot/ && cd crypto_bot && ruff check .`
4. **Commit + push**: solo dopo che tutti i test passano
5. **Deploy**: `./deploy.sh` (rsync + docker rebuild)
6. **Verifica**: `ssh root@<VPS_IP_REDACTED> "cd /opt/hlquantbot && docker compose logs -f bot"`

---

## Technology Stack

| Category | Tools |
|----------|-------|
| **Core** | Python 3.11+, asyncio, Pydantic |
| **Exchange** | Hyperliquid SDK |
| **AI/ML** | DeepSeek LLM veto, regime detection (ADX) |
| **Notifications** | ntfy.sh push notifications |
| **Quality** | Pyright, Ruff, Black, pytest |

---

## Coding Standards

### Python Guidelines
- **Type hints obbligatori** per tutti i parametri e return
- **Async/await** per tutte le operazioni I/O
- **Decimal** per calcoli finanziari (mai float!)
- **Pydantic** per validazione dati
- **Snake_case** per funzioni/variabili, **PascalCase** per classi

### Trading-Specific
- Validare SEMPRE gli ordini prima dell'invio
- Usare il RiskManager per ogni trade
- Loggare tutte le operazioni finanziarie
- Gestire gracefully disconnessioni WebSocket

---

## Available Agents

| Agent | Uso |
|-------|-----|
| `hlquantbot-developer` | Sviluppo features Python |
| `hlquantbot-debugger` | Debug real-time, query DB |
| `trading-code-reviewer` | Review codice trading |
| `hyperliquid-trade-verifier` | Verifica posizioni exchange |

---

## Environment Variables

Required in `.env`:
```
HYPERLIQUID_WALLET_ADDRESS=...
HYPERLIQUID_PRIVATE_KEY=...
DEEPSEEK_API_KEY=...
NTFY_TOPIC=...
```

---

## Security Guidelines

**MAI hardcodare secrets nel codice.** Usare sempre `os.environ.get()` o `python-dotenv`.

### Safety Rules

1. **Mai** committare `.env` o chiavi private
2. **Mai** eseguire ordini live senza conferma esplicita
3. **Sempre** verificare posizioni prima di modifiche al trading
4. **Mai** usare `float` per importi finanziari

---

## Hetzner VPS (Production)

| Risorsa | Valore |
|---------|--------|
| **IP** | `<VPS_IP_REDACTED>` |
| **SSH** | `ssh root@<VPS_IP_REDACTED>` |
| **Deploy Dir** | `/opt/hlquantbot` |

### Comandi Server
```bash
ssh root@<VPS_IP_REDACTED>
cd /opt/hlquantbot
docker compose ps                # Status
docker compose logs -f bot       # Log bot
docker compose restart bot       # Restart
```
