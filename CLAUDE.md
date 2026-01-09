# HLQuantBot - Claude Code Configuration

> Trading bot per Hyperliquid DEX con architettura multi-agent

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
├── agents/           # Trading agents (execution, market data, regime)
├── config/           # YAML configurations
├── dashboard/        # Flask + HTMX frontend
├── database/         # PostgreSQL models e migrations
├── services/         # Core services (risk manager, LLM veto)
├── strategies/       # Trading strategies
└── main_conservative.py  # Entry point
```

---

## Development Commands

### Testing
```bash
cd simple_bot && python -m pytest tests/ -v
cd simple_bot && python -m pytest tests/ --cov=. --cov-report=html
```

### Code Quality
```bash
pyright simple_bot/                 # Type checking (run from project root)
cd simple_bot && ruff check .       # Linting
cd simple_bot && black .            # Formatting
```

### Database
```bash
# Migrazioni in simple_bot/database/migrations/
python -c "from database.database import run_migrations; import asyncio; asyncio.run(run_migrations())"
```

### Running
```bash
cd simple_bot && python main_conservative.py   # Bot
cd simple_bot && python -m dashboard.app       # Dashboard only
```

---

## Technology Stack

| Category | Tools |
|----------|-------|
| **Core** | Python 3.11+, asyncio, Pydantic |
| **Exchange** | Hyperliquid SDK, WebSocket |
| **Database** | PostgreSQL, asyncpg |
| **Frontend** | Flask, HTMX, TailwindCSS |
| **AI/ML** | OpenAI (LLM veto), custom regime detection |
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

### Async Patterns
```python
# ✅ Corretto
async def fetch_data():
    async with aiohttp.ClientSession() as session:
        return await session.get(url)

# ❌ Sbagliato - blocking call in async
async def fetch_data():
    return requests.get(url)  # BLOCKING!
```

---

## Available Agents

Usa `Task(subagent_type="agent-name")` per invocare:

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

## MCP Servers

Configurati in `.mcp.json`:

| Server | Funzione |
|--------|----------|
| `github` | Gestione repo, PR, issues |

---

## Environment Variables

Required in `.env`:
```
DATABASE_URL=postgresql://...
HYPERLIQUID_WALLET_ADDRESS=...
HYPERLIQUID_PRIVATE_KEY=...
OPENAI_API_KEY=...  # Per LLM veto
```

---

## Security Guidelines

### CRITICAL: Mai Hardcodare Secrets

**MAI scrivere API keys, private keys, o password nel codice.**

```python
# ❌ SBAGLIATO
PRIVATE_KEY = "0x1234567890abcdef..."
API_KEY = "sk-proj-..."

# ✅ CORRETTO
import os
PRIVATE_KEY = os.environ.get("HYPERLIQUID_PRIVATE_KEY")
API_KEY = os.environ.get("OPENAI_API_KEY")
```

### Secrets da Proteggere

| Secret | Rischio se esposto |
|--------|-------------------|
| `HYPERLIQUID_PRIVATE_KEY` | **CRITICO** - Accesso completo al wallet, perdita fondi |
| `HYPERLIQUID_WALLET_ADDRESS` | Medio - Visibilità posizioni |
| `DATABASE_URL` | Alto - Accesso DB, manipolazione dati |
| `OPENAI_API_KEY` | Medio - Costi API non autorizzati |

### Quando Crei Script con Secrets

1. Usa `os.environ.get()` o `python-dotenv`
2. Aggiungi variabile a `.env.example` con placeholder
3. Verifica che `.env` sia in `.gitignore`
4. Mai loggare valori di secrets (usa `***` per mascherare)

### Se Committi Accidentalmente un Secret

1. **Revoca IMMEDIATAMENTE** la chiave/token
2. Genera nuova chiave
3. Aggiorna `.env` locale e su server
4. La vecchia chiave è compromessa per sempre (git history)

> **ATTENZIONE**: Per `HYPERLIQUID_PRIVATE_KEY`, un commit accidentale significa potenziale perdita di TUTTI i fondi nel wallet. Genera sempre un nuovo wallet se esposto.

---

## Safety Rules

1. **Mai** committare `.env` o chiavi private
2. **Mai** eseguire ordini live senza conferma esplicita
3. **Sempre** verificare posizioni prima di modifiche al trading
4. **Sempre** usare paper trading per test nuove strategie
5. **Mai** usare `float` per importi finanziari

---

## Hetzner VPS (Production)

| Risorsa | Valore |
|---------|--------|
| **IP** | `<VPS_IP_REDACTED>` |
| **SSH** | `ssh root@<VPS_IP_REDACTED>` |
| **Deploy Dir** | `/opt/hlquantbot` |
| **Dashboard** | http://<VPS_IP_REDACTED>:5000/ |
| **Frontend** | http://<VPS_IP_REDACTED>:5611/ |
| **PostgreSQL** | `<VPS_IP_REDACTED>:5432` |

### Deploy
```bash
./deploy.sh  # Rsync + Docker rebuild
```

### Comandi Utili sul Server
```bash
ssh root@<VPS_IP_REDACTED>
cd /opt/hlquantbot
docker compose ps                # Status servizi
docker compose logs -f bot       # Log bot
docker compose logs -f dashboard # Log dashboard
docker compose restart bot       # Restart bot
```

### Database Remoto
```bash
# Connessione diretta
psql -h <VPS_IP_REDACTED> -U trader -d trading_db

# Nel .env per sviluppo locale con DB remoto:
DATABASE_URL=postgresql://trader:trader_password@<VPS_IP_REDACTED>:5432/trading_db
```

---

## Resources

- [Hyperliquid Docs](https://hyperliquid.gitbook.io/)
- [Claude Code Changelog](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md)
- Dashboard locale: http://localhost:5001
- Dashboard Hetzner: http://<VPS_IP_REDACTED>:5000/
