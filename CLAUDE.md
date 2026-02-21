# HLQuantBot - Claude Code Configuration

> BTC trading bot for Hyperliquid DEX - EMA Momentum Scalper strategy, live on mainnet

---

## Current Strategy: EMA Momentum Scalper

| Aspect | Configuration |
|--------|---------------|
| **Asset** | BTC only |
| **Timeframe** | 15m (scan every 5 min) |
| **Strategy** | EMA9/EMA21 crossover + RSI filter |
| **Entry LONG** | EMA9 > EMA21, RSI 30-65 |
| **Entry SHORT** | EMA9 < EMA21, RSI 35-70 (enabled) |
| **TP / SL** | 0.8% / 0.4% (1:2 R:R) |
| **Leverage** | 10x |
| **Position Size** | Max 70% account |
| **Limits** | Max 8 trades/day |
| **LLM Veto** | Enabled (DeepSeek, max 20 calls/day) |
| **Mode** | **LIVE mainnet** (`dry_run: false`) |

### Key Files

| File | Purpose |
|------|---------|
| `strategies/momentum_scalper.py` | EMA9/21 crossover + RSI + ATR filter |
| `strategies/trend_follow.py` | Legacy SMA crossover (disabled) |
| `services/market_state.py` | Indicator calculation (EMA, SMA, RSI, ATR) |
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
| `/db-migrate` | Gestione migrazioni database |
| `/start-bot` | Avvia il trading bot |

---

## Project Structure

```
simple_bot/
├── config/           # trading.yaml (primary config)
├── core/             # models.py (MarketState, Regime, Direction)
├── services/         # risk_manager, execution_engine, market_state, llm_veto, kill_switch
├── strategies/       # momentum_scalper (active), trend_follow (disabled)
├── dashboard/        # Flask + HTMX frontend
├── database/         # PostgreSQL models e migrations
├── tests/            # pytest test suite (218 tests)
└── main.py           # Entry point
```

---

## Development Commands

### Testing
```bash
cd simple_bot && python3 -m pytest tests/ -v
cd simple_bot && python3 -m pytest tests/ --cov=. --cov-report=html
```

### Code Quality
```bash
pyright simple_bot/                 # Type checking (from project root)
cd simple_bot && ruff check .       # Linting
cd simple_bot && black .            # Formatting
```

### Running
```bash
cd simple_bot && python3 main.py              # Bot (live)
cd simple_bot && python3 -m dashboard.app     # Dashboard only
```

---

## Deploy Workflow: Locale -> Hetzner

1. **Sviluppo locale**: modifica codice
2. **Test**: `cd simple_bot && python3 -m pytest tests/ -v`
3. **Lint**: `pyright simple_bot/ && cd simple_bot && ruff check .`
4. **Commit + push**: solo dopo che tutti i test passano
5. **Deploy**: `./deploy.sh` (rsync + docker rebuild)
6. **Verifica**: `ssh root@<VPS_IP_REDACTED> "cd /opt/hlquantbot && docker compose logs -f bot"`

---

## Technology Stack

| Category | Tools |
|----------|-------|
| **Core** | Python 3.11+, asyncio, Pydantic |
| **Exchange** | Hyperliquid SDK, WebSocket |
| **Database** | PostgreSQL, asyncpg |
| **Frontend** | Flask, HTMX, TailwindCSS |
| **AI/ML** | DeepSeek LLM veto, custom regime detection |
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
| `postgres-dba-expert` | Schema, migrations, query |
| `flask-htmx-frontend` | Dashboard e UI |
| `frontend-qa-verifier` | Visual QA dashboard |
| `hyperliquid-trade-verifier` | Verifica posizioni exchange |

---

## Environment Variables

Required in `.env`:
```
DATABASE_URL=postgresql://...
HYPERLIQUID_WALLET_ADDRESS=...
HYPERLIQUID_PRIVATE_KEY=...
DEEPSEEK_API_KEY=...
GITHUB_PERSONAL_ACCESS_TOKEN=...
```

---

## Security Guidelines

**MAI hardcodare secrets nel codice.** Usare sempre `os.environ.get()` o `python-dotenv`.

| Secret | Rischio se esposto |
|--------|-------------------|
| `HYPERLIQUID_PRIVATE_KEY` | **CRITICO** - Perdita fondi |
| `DATABASE_URL` | Alto - Accesso DB |
| `DEEPSEEK_API_KEY` | Medio - Costi API |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | Medio - Accesso repo |

### Safety Rules

1. **Mai** committare `.env` o chiavi private
2. **Mai** eseguire ordini live senza conferma esplicita
3. **Sempre** verificare posizioni prima di modifiche al trading
4. **Sempre** testare nuove strategie in paper mode prima del live
5. **Mai** usare `float` per importi finanziari

---

## Hetzner VPS (Production)

| Risorsa | Valore |
|---------|--------|
| **IP** | `<VPS_IP_REDACTED>` |
| **SSH** | `ssh root@<VPS_IP_REDACTED>` |
| **Deploy Dir** | `/opt/hlquantbot` |
| **Dashboard** | http://<VPS_IP_REDACTED>:5000/ |
| **PostgreSQL** | `<VPS_IP_REDACTED>:5432` |

### Comandi Server
```bash
ssh root@<VPS_IP_REDACTED>
cd /opt/hlquantbot
docker compose ps                # Status
docker compose logs -f bot       # Log bot
docker compose restart bot       # Restart
```

---

## Resources

- [Hyperliquid Docs](https://hyperliquid.gitbook.io/)
- Dashboard locale: http://localhost:5001
- Dashboard Hetzner: http://<VPS_IP_REDACTED>:5000/
