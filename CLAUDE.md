# HLQuantBot - Claude Code Configuration

> Trading bots repo: crypto (Hyperliquid DEX) + IB (Interactive Brokers). Due progetti separati.

## Regola fondamentale

**Non descrivere MAI il comportamento del bot basandoti su questo file o sulla memoria.**
Per sapere come funziona il sistema, LEGGI SEMPRE il codice sorgente. Questo file contiene solo
istruzioni stabili (workflow, comandi, standards, regole). La logica di trading cambia spesso.

## Startup: Activate Serena

All'inizio di ogni conversazione, attiva il progetto Serena con:
```
mcp__plugin_serena_serena__activate_project(project="trader_bitcoin")
```

## Workflow: Teammate Agents + Background

- Usa SEMPRE i teammate agents specializzati (Task tool) in parallelo dove possibile.
- Lancia SEMPRE i task in **background** (`run_in_background: true`) così l'utente può continuare a lavorare.
- Non bloccare mai la conversazione aspettando un agent - lancia in background e rispondi subito.

| Agent | Uso |
|-------|-----|
| `hlquantbot-developer` | Sviluppo features Python |
| `hlquantbot-debugger` | Debug real-time, query DB |
| `trading-code-reviewer` | Review codice trading |
| `hyperliquid-trade-verifier` | Verifica posizioni exchange |

---

## Project Structure

```
crypto_bot/                   # Crypto bot (Hyperliquid DEX) ← MAIN
├── main.py                   # Entry point
├── api/                      # Exchange connector, rate limiter
├── config/                   # trading.yaml (prod), trading_paper.yaml (testnet)
├── core/                     # Pydantic models, enums
├── flag_trader/              # LLM agent, model, env, trainer, RAG
├── services/                 # Microservices (execution, risk, alerts...)
├── strategies/               # Legacy (deprecated)
├── scripts/                  # Training, replay, download
└── tests/                    # pytest suite

ib_bot/                       # IB bot (MES futures) — PROGETTO SEPARATO
models/                       # Checkpoints modelli
```

Per capire cosa è attivo e cosa no, leggere `main.py` e `config/trading.yaml`.

---

## Quick Start Commands

| Comando | Descrizione |
|---------|-------------|
| `/test` | Esegui test suite |
| `/lint` | Type-check e linting |
| `/review` | Code review con trading-code-reviewer agent |
| `/start-bot` | Avvia il trading bot |

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

## Deploy Workflow: Locale -> VPS

1. **Sviluppo locale**: modifica codice
2. **Test**: `cd crypto_bot && python3 -m pytest tests/ -v`
3. **Lint**: `pyright crypto_bot/ && cd crypto_bot && ruff check .`
4. **Commit + push**: solo dopo che tutti i test passano
5. **Deploy**: `./deploy.sh crypto` (rsync + docker rebuild)
6. **Verifica**: `docker compose logs -f crypto_bot`

---

## Technology Stack

| Category | Tools |
|----------|-------|
| **Core** | Python 3.11+, asyncio, Pydantic |
| **Exchange** | Hyperliquid SDK |
| **AI/ML** | Transformers, PPO, Gymnasium |
| **Notifications** | ntfy.sh |
| **Deploy** | Docker Compose, Hetzner VPS, rsync |
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

## Environment Variables

Required in `.env`:
```
HYPERLIQUID_WALLET_ADDRESS=...
HYPERLIQUID_PRIVATE_KEY=...
NTFY_TOPIC=...
```

---

## Security Guidelines

**MAI hardcodare secrets nel codice.** Usare sempre `os.environ.get()` o `python-dotenv`.

1. **Mai** committare `.env` o chiavi private
2. **Mai** eseguire ordini live senza conferma esplicita
3. **Sempre** verificare posizioni prima di modifiche al trading
4. **Mai** usare `float` per importi finanziari

---

## VPS (Production)

Connection details in `deploy.env` (gitignored). See `deploy.env.example` for template.

```bash
source deploy.env
ssh $VPS_USER@$VPS_IP
cd $DEPLOY_DIR
docker compose ps                    # Status
docker compose logs -f crypto_bot    # Log bot
docker compose restart crypto_bot    # Restart
```
