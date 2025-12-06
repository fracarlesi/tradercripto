# Rizzo Trading Bot - Guida Deploy e Sviluppo

## Note per Claude Code

**REGOLA IMPORTANTE**: MAI prendere iniziativa su modifiche tecniche (cambiare modelli AI, endpoint, configurazioni critiche) senza prima condividere e chiedere conferma all'utente.

Per ricerche semantiche nella codebase, usare l'MCP `claude-context`:
- `mcp__claude-context__search_code` - ricerca semantica nel codice
- `mcp__claude-context__index_codebase` - indicizza la codebase (se necessario)

## Repository GitHub
https://github.com/fracarlesi/tradercripto2

## Architettura

Bot di trading crypto automatizzato che usa **GPT-5.1** (via API OpenAI diretta) per decisioni di trading su Hyperliquid.

### Sistema Monolitico

Il bot esegue un ciclo completo ogni 15 minuti:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        FLUSSO ESECUZIONE                                │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  1. Raccolta Dati (ogni 15 min)                                        │
│     └─ Indicatori tecnici: EMA, MACD, RSI, ATR, Pivot Points           │
│     └─ Forecast prezzi: Prophet (15min e 1h)                           │
│     └─ Sentiment: Fear & Greed Index (CoinMarketCap)                   │
│     └─ News: Feed RSS crypto                                           │
│     └─ Account: Balance e posizioni Hyperliquid                        │
│                                                                         │
│  2. Decisione LLM                                                       │
│     └─ Prompt con tutti i dati aggregati                               │
│     └─ GPT-5.1 via API OpenAI                                          │
│     └─ Output JSON: operation, symbol, direction, leverage, reason     │
│                                                                         │
│  3. Esecuzione                                                          │
│     └─ Azione: OPEN / CLOSE / HOLD                                     │
│     └─ Simboli: BTC, ETH, SOL                                          │
│     └─ Leverage: 1-10x (raccomandato max 5x)                           │
│                                                                         │
│  4. Logging                                                             │
│     └─ PostgreSQL: operazioni, errori, contesti                        │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Componenti

**Core:**
- **main.py**: Entry point, orchestrazione ciclo
- **trading_agent.py**: Chiamate GPT-5.1 via API OpenAI
- **hyperliquid_trader.py**: Interfaccia con exchange Hyperliquid
- **indicators.py**: Analisi tecnica (EMA, MACD, RSI, Pivot Points)
- **forecaster.py**: Previsioni prezzi con Prophet
- **sentiment.py**: Fear & Greed Index da CoinMarketCap
- **news_feed.py**: Feed RSS notizie crypto
- **db_utils.py**: Logging PostgreSQL

**Configurazione:**
- **system_prompt.txt**: Istruzioni per il modello LLM
- **requirements.txt**: Dipendenze Python

## Configurazione LLM

Il bot usa **GPT-5.1** via API OpenAI diretta. Parametri in `trading_agent.py`:
```python
model="gpt-5.1"
temperature=0.3      # Basso per reasoning deterministico
max_tokens=1000      # Spazio per risposta JSON
response_format={"type": "json_object"}
```

## Variabili d'Ambiente (.env)

```bash
PRIVATE_KEY=<hyperliquid_private_key>
WALLET_ADDRESS=<hyperliquid_wallet>
OPENAI_API_KEY=<openai_api_key>
CMC_PRO_API_KEY=<coinmarketcap_api_key>
DATABASE_URL=postgresql://trader:password@postgres:5432/trader_db
```

---

## ⚠️ IMPORTANTE: Modifiche Sempre in Locale

**Le modifiche al codice vanno SEMPRE fatte sui file locali, MAI direttamente sul server.**

Il deploy usa `rsync --delete` che sovrascrive tutto sul server con i file locali. Modifiche fatte direttamente sul server verranno perse al prossimo deploy.

---

## 🚨 REGOLA FONDAMENTALE: Usare SEMPRE deploy.sh

**Per il deploy usare SEMPRE `./deploy.sh`, MAI rsync manuale!**

Il file `run_bot.sh` ha bisogno di permessi di esecuzione (`chmod +x`) e ownership `root:root`.
Quando si usa `rsync` direttamente, i permessi vengono sovrascritti con quelli del Mac (owner 501:staff, senza +x) e il **cron smette di funzionare silenziosamente**.

Lo script `deploy.sh` include automaticamente:
```bash
ssh root@$VPS_IP "chown root:root $DEPLOY_DIR/run_bot.sh && chmod +x $DEPLOY_DIR/run_bot.sh"
```

**Se il bot non esegue decisioni**, la prima cosa da verificare è:
```bash
ssh root@<VPS_IP_REDACTED> "ls -la /opt/trader_bitcoin/run_bot.sh"
# Deve mostrare: -rwx--x--x 1 root root ...
# Se mostra: -rw------- 1 501 staff ... → i permessi sono sbagliati!

# Fix manuale:
ssh root@<VPS_IP_REDACTED> "chown root:root /opt/trader_bitcoin/run_bot.sh && chmod +x /opt/trader_bitcoin/run_bot.sh"
```

---

## Deploy su Hetzner VPS con Docker

### Server
- **IP**: <VPS_IP_REDACTED>
- **Directory**: /opt/trader_bitcoin
- **User**: root
- **Dashboard**: http://<VPS_IP_REDACTED>:5611/

### Comandi Deploy Rapido

```bash
# Deploy completo (da locale)
./deploy.sh

# Oppure manualmente:
rsync -avz --delete --exclude='.git' --exclude='__pycache__' --exclude='.env' ./ root@<VPS_IP_REDACTED>:/opt/trader_bitcoin/
scp .env root@<VPS_IP_REDACTED>:/opt/trader_bitcoin/.env
ssh root@<VPS_IP_REDACTED> "cd /opt/trader_bitcoin && docker compose build --no-cache && docker compose up -d postgres dashboard"
```

### Gestione Container sul Server

```bash
# SSH al server
ssh root@<VPS_IP_REDACTED>

# Vai alla directory
cd /opt/trader_bitcoin

# Vedere logs bot
tail -f /opt/trader_bitcoin/logs/bot.log

# Vedere logs Docker
docker compose logs -f postgres
docker compose logs -f dashboard

# Eseguire bot manualmente
docker compose run --rm app python main.py

# Inizializzare database (se necessario)
docker compose run --rm app python -c "import db_utils; db_utils.init_db()"

# Entrare nel container
docker compose exec app bash
docker compose exec postgres psql -U trader -d trader_db
```

### Struttura Docker

```
docker-compose.yml
├── postgres (porta 5432)
│   └─ Volume: postgres_data
│   └─ Healthcheck attivo
├── dashboard (porta 5611)
│   └─ FastAPI + HTMX
│   └─ restart: unless-stopped
└── app (bot trading)
    └─ restart: "no" (eseguito da cron)
```

### Cron Job

```bash
# Vedere crontab attuale
crontab -l

# Configurazione attiva:
*/15 * * * * /opt/trader_bitcoin/run_bot.sh
```

Esegue ogni 15 minuti:
- X:00, X:15, X:30, X:45

---

## Database PostgreSQL

### Connessione da locale
```bash
psql postgresql://trader:password@<VPS_IP_REDACTED>:5432/trader_db
```

### Query utili
```sql
-- Ultime operazioni
SELECT * FROM bot_operations ORDER BY id DESC LIMIT 10;

-- Balance history
SELECT * FROM account_snapshots ORDER BY id DESC LIMIT 20;

-- Errori recenti
SELECT * FROM errors ORDER BY id DESC LIMIT 10;

-- Contesti AI
SELECT * FROM ai_contexts ORDER BY id DESC LIMIT 5;
```

### Inizializzare/Reset Database
```bash
# Reset completo
docker compose exec postgres psql -U trader -d trader_db -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public; GRANT ALL ON SCHEMA public TO trader;"

# Ricreare tabelle
docker compose run --rm app python -c "import db_utils; db_utils.init_db()"
```

---

## Troubleshooting

### Bot non esegue trade
1. Verificare logs: `tail -50 /opt/trader_bitcoin/logs/bot.log`
2. Controllare API keys nel .env
3. Verificare balance su Hyperliquid
4. Test manuale: `docker compose run --rm app python main.py`

### Errore OpenAI API
1. Verificare OPENAI_API_KEY nel .env
2. Controllare crediti su platform.openai.com
3. Verificare modello disponibile: `gpt-5.1`

### Database connection refused
1. Verificare che postgres sia up: `docker compose ps`
2. Controllare DATABASE_URL nel .env
3. Riavviare: `docker compose restart postgres`

### Tabelle non esistono
```bash
docker compose run --rm app python -c "import db_utils; db_utils.init_db()"
```

### Dashboard non raggiungibile (porta 5611)
1. Verificare che il container sia attivo: `docker compose ps`
2. Avviare il dashboard: `docker compose up -d dashboard`
3. Controllare logs: `docker compose logs -f dashboard`
4. Verificare porta esposta: `curl http://localhost:5611/`

---

## Struttura File

```
trader_bitcoin/
├── .env                    # Credenziali (NON committare)
├── .gitignore
├── CLAUDE.md               # Questa guida
├── deploy.sh               # Script deploy Hetzner
├── docker-compose.yml      # Configurazione Docker
├── Dockerfile              # Build image Python
├── requirements.txt        # Dipendenze Python
├── run_bot.sh              # Script per cron
├── main.py                 # Entry point
├── trading_agent.py        # LLM GPT-5.1 via API OpenAI
├── hyperliquid_trader.py   # Exchange API
├── indicators.py           # Analisi tecnica
├── forecaster.py           # Prophet forecasting
├── sentiment.py            # Fear & Greed
├── news_feed.py            # RSS news
├── db_utils.py             # PostgreSQL logging
├── system_prompt.txt       # Prompt LLM
├── logs/                   # Directory logs (sul server)
└── frontend/               # Dashboard web (FastAPI + HTMX)
    ├── Dockerfile          # Build image dashboard
    ├── main.py             # API e pagine HTML
    ├── requirements.txt    # Dipendenze frontend
    └── templates/          # Template Jinja2
```
