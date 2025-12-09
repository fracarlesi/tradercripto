# HLQuantBot - Guida Deploy e Sviluppo

## Note per Claude Code

**REGOLA IMPORTANTE**: MAI prendere iniziativa su modifiche tecniche (cambiare modelli AI, endpoint, configurazioni critiche) senza prima condividere e chiedere conferma all'utente.

Per ricerche semantiche nella codebase, usare l'MCP `claude-context`:
- `mcp__claude-context__search_code` - ricerca semantica nel codice
- `mcp__claude-context__index_codebase` - indicizza la codebase (se necessario)

## Ambiente Attivo

**SIAMO SU MAINNET - SOLDI VERI!**

| Parametro | Valore |
|-----------|--------|
| Environment | **PRODUCTION (MAINNET)** |
| Directory server | `/opt/trader_bitcoin` |
| Dashboard | http://<VPS_IP_REDACTED>:5611/ |
| PostgreSQL | porta 5432 |
| Health | porta 8081 |
| API | https://hyperliquid.xyz |

## Repository GitHub
https://github.com/fracarlesi/tradercripto2

## Architettura

Bot di trading HFT quantitativo per Hyperliquid con AI DeepSeek V3.2-Speciale per regime detection.

### Main Loop (ogni 1 secondo)

```
1. Check circuit breaker (temporal + hard)
2. Get account state (REST)
3. Detect market regime (AI ogni 5 min, cached)
4. Get market data (WebSocket + BarAggregator)
5. Evaluate strategies (5 HFT strategies)
6. Risk engine (validate, size, leverage check)
7. Execute orders (maker-only ALO)
8. Save metrics + snapshots
```

### Componenti Principali

```
hlquantbot/
├── bot.py               # Main orchestrator
├── config/
│   ├── settings.py      # Pydantic settings + YAML
│   └── config.yaml      # Parametri strategie e risk
├── strategies/hft/
│   ├── mmr_hft.py       # Micro Mean Reversion (VWAP)
│   ├── micro_breakout.py    # Breakout BB compression
│   ├── pair_trading.py      # Spread trading BTC/ETH, ETH/SOL
│   └── liquidation_sniping.py  # Liquidation cascade
├── ai/
│   └── regime_detector.py   # DeepSeek V3.2-Speciale
├── risk/
│   ├── risk_engine.py       # Portfolio risk
│   └── circuit_breaker.py   # Hard + temporal stops
└── execution/
    └── execution_engine.py  # Order execution ALO
```

## AI Layer - DeepSeek V3.2-Speciale

```yaml
openai:
  enabled: true
  model: deepseek-reasoner
  base_url: https://api.deepseek.com
  regime_detection_interval_minutes: 5
  max_tokens: 4000
  temperature: 0.3
```

**Regime Output**: `trend_up | trend_down | range_bound | high_volatility | low_volatility | uncertain`

**API Key**: `DEEPSEEK_API_KEY` nel .env (sk-cbd31...)

## Risk Management

### Fee Structure (MAINNET)
```
MAKER_FEE = 0.02%   # SEMPRE usare questo (ALO)
TAKER_FEE = 0.05%   # MAI per HFT
```

### Position Limits
```
RISK_PER_TRADE = 0.7%
MAX_PORTFOLIO_LEVERAGE = 4x
MAX_EXPOSURE_PER_ASSET = 40%
MAX_OPEN_POSITIONS = 15
```

### Circuit Breaker
- **Daily loss > 10%** → Exit process
- **Total drawdown > 50%** → Exit process
- Richiede restart manuale

### Temporal Kill-Switch
| Livello | Finestra | Max Drawdown | Cooldown |
|---------|----------|--------------|----------|
| 1 | 30s | 0.7% | 15 min |
| 2 | 10 min | 2.0% | 1 ora |
| 3 | 1 ora | 4.5% | 6 ore |

---

## Deploy su Hetzner VPS

### Server MAINNET
- **IP**: <VPS_IP_REDACTED>
- **Directory**: /opt/trader_bitcoin
- **User**: root

### Comandi Deploy

```bash
# Deploy completo (da Mac locale)
cd "/Users/francescocarlesi/Downloads/Progetti Python/trader_bitcoin"
./deploy.sh
```

### Gestione Container

```bash
# SSH al server
ssh root@<VPS_IP_REDACTED>

# Logs bot MAINNET
docker logs trader_mainnet_app --tail 100 -f

# Stato container
docker ps --filter 'name=trader'

# Restart bot
cd /opt/trader_bitcoin && docker compose restart app

# Health check
curl http://localhost:8081/health

# Database
docker exec trader_mainnet_postgres psql -U trader -d trader_db
```

### Query Database Utili

```sql
-- Ultime analisi AI regime
SELECT id, timestamp, regime, confidence, analysis
FROM regime_history ORDER BY id DESC LIMIT 5;

-- Account snapshots
SELECT * FROM account_snapshots ORDER BY id DESC LIMIT 20;

-- Trade recenti
SELECT * FROM trades ORDER BY id DESC LIMIT 10;
```

---

## Troubleshooting

### Bot non genera segnali
1. Verificare regime: se "uncertain", condizioni non ottimali
2. Controllare se ha posizioni aperte (skippa simboli con posizione)
3. Verificare spread (troppo largo per HFT)

### DeepSeek timeout
- Timeout configurato: 90 secondi
- Se persiste, controllare connettività API

### Bot frozen/unhealthy
```bash
# Restart
ssh root@<VPS_IP_REDACTED> "cd /opt/trader_bitcoin && docker compose restart app"
```

### Verificare API key DeepSeek
```bash
ssh root@<VPS_IP_REDACTED> "docker exec trader_mainnet_app env | grep DEEPSEEK"
# Deve mostrare: DEEPSEEK_API_KEY=sk-cbd31...
```

---

## Variabili d'Ambiente (.env)

```bash
ENVIRONMENT=production
PRIVATE_KEY=0x...
WALLET_ADDRESS=0x...
DEEPSEEK_API_KEY=sk-cbd31...  # DeepSeek V3.2-Speciale
CMC_PRO_API_KEY=...
DATABASE_URL=postgresql://trader:password@postgres:5432/trader_db
```

---

## Note

- I valori dinamici (equity, posizioni, regime) cambiano continuamente - controllare sempre via dashboard o database
- Per lo stato attuale: `docker logs trader_mainnet_app --tail 20`
