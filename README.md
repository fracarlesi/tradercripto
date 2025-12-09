# HLQuantBot

Bot di trading quantitativo ad alta frequenza (HFT) per Hyperliquid, con intelligenza artificiale DeepSeek per l'analisi del regime di mercato.

## Architettura

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            HLQuantBot v1.0                                   │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐                   │
│  │  Market Data │    │  AI Layer    │    │ Risk Engine  │                   │
│  │  (WebSocket) │───▶│  (DeepSeek)  │───▶│  (Circuit    │                   │
│  │              │    │              │    │   Breaker)   │                   │
│  └──────────────┘    └──────────────┘    └──────────────┘                   │
│         │                   │                   │                            │
│         ▼                   ▼                   ▼                            │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │                      STRATEGIE HFT                                   │    │
│  │  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐    │    │
│  │  │  MMR-HFT    │ │   Micro-    │ │    Pair     │ │ Liquidation │    │    │
│  │  │  (Mean Rev) │ │  Breakout   │ │   Trading   │ │   Sniping   │    │    │
│  │  └─────────────┘ └─────────────┘ └─────────────┘ └─────────────┘    │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│         │                                                                    │
│         ▼                                                                    │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐                   │
│  │  Execution   │───▶│  PostgreSQL  │◀───│  Dashboard   │                   │
│  │  Engine      │    │  (Persist)   │    │  (FastAPI)   │                   │
│  └──────────────┘    └──────────────┘    └──────────────┘                   │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Strategie di Trading

### 1. MMR-HFT (Micro Mean Reversion)
- **Logica**: Trading contro deviazioni dal VWAP
- **Timeframe**: 5 minuti
- **Entry**: Deviazione 0.1% - 0.3% dal VWAP
- **TP/SL**: 0.2% / 0.3%
- **Leverage**: 15x - 20x

### 2. Micro-Breakout
- **Logica**: Breakout dopo compressione delle Bollinger Bands
- **Timeframe**: 5 minuti
- **Entry**: Range < 0.2%, breakout > 0.1%
- **TP/SL**: 0.3% / 0.4%
- **Leverage**: 10x - 15x

### 3. Pair Trading
- **Logica**: Spread trading su coppie correlate (BTC/ETH, ETH/SOL)
- **Entry**: Z-score > 2.0
- **Exit**: Z-score < 0.5
- **Leverage**: 15x - 20x per leg

### 4. Liquidation Sniping
- **Logica**: Counter-trend dopo cascate di liquidazioni
- **Trigger**: OI spike 2% + price spike 0.5%
- **TP/SL**: 0.3% / 0.2%
- **Hold max**: 30 secondi

### 5. Momentum Scalping
- **Logica**: Segue momentum su RSI e volume
- **Entry**: RSI > 60 (long) o RSI < 40 (short) + volume ratio > 1.2
- **Leverage**: 10x - 15x

## AI Layer - DeepSeek V3.2-Speciale

Il bot utilizza **DeepSeek V3.2-Speciale** per:
- **Regime Detection**: Classifica il mercato (trend_up, trend_down, range_bound, high_volatility, low_volatility, uncertain)
- **Risk Adjustment**: Adatta dinamicamente il rischio in base alle condizioni di mercato
- **Intervallo**: Analisi ogni 5 minuti

## Risk Management

### Circuit Breaker (Hard Stop)
- **Daily Loss > 10%**: Exit immediato del processo
- **Total Drawdown > 50%**: Stop completo, richiede restart manuale

### Temporal Kill-Switch (Soft Stop)
| Livello | Finestra | Max Drawdown | Cooldown |
|---------|----------|--------------|----------|
| 1 | 30 sec | 0.7% | 15 min |
| 2 | 10 min | 2.0% | 1 ora |
| 3 | 1 ora | 4.5% | 6 ore |

### Position Limits
- **Risk per trade**: 0.7% del capitale
- **Max leverage portfolio**: 4x
- **Max esposizione per asset**: 40%
- **Max posizioni aperte**: 15

## Struttura del Progetto

```
hlquantbot/
├── bot.py                  # Main orchestrator
├── config/
│   ├── settings.py         # Pydantic settings
│   ├── config.yaml         # Strategy parameters
│   └── symbols.yaml        # Trading symbols
├── core/
│   ├── models.py           # Data models
│   └── enums.py            # Enumerations
├── data/
│   ├── market_data.py      # Market data layer
│   ├── websocket_client.py # WebSocket connection
│   └── rest_client.py      # REST API client
├── strategies/
│   ├── base.py             # Base strategy class
│   └── hft/                # HFT strategies
│       ├── mmr_hft.py
│       ├── micro_breakout.py
│       ├── pair_trading.py
│       ├── liquidation_sniping.py
│       └── momentum_scalping.py
├── risk/
│   ├── risk_engine.py      # Risk management
│   ├── circuit_breaker.py  # Circuit breaker
│   └── position_sizer.py   # Position sizing
├── execution/
│   ├── execution_engine.py # Order execution
│   └── order_manager.py    # Order tracking
├── ai/
│   ├── regime_detector.py  # AI regime detection
│   └── prompts.py          # AI prompts
├── persistence/
│   └── database.py         # PostgreSQL operations
└── monitoring/
    ├── health_server.py    # Health check endpoint
    └── hft_metrics.py      # Performance metrics
```

## Deployment

### Requisiti
- Docker & Docker Compose
- PostgreSQL 15
- Python 3.11+

### Configurazione

1. Crea il file `.env`:
```env
ENVIRONMENT=production
PRIVATE_KEY=0x...
WALLET_ADDRESS=0x...
DEEPSEEK_API_KEY=sk-...  # DeepSeek API key
DATABASE_URL=postgresql://trader:password@postgres:5432/trader_db
```

2. Deploy:
```bash
./deploy.sh
```

### Comandi Utili

```bash
# Logs
docker logs trader_mainnet_app -f

# Restart
docker compose restart app

# Status
docker ps --filter 'name=trader'

# Health check
curl http://localhost:8080/health
```

## Dashboard

La dashboard web è disponibile su porta 5611:
- **Equity Curve**: Grafico del valore del portfolio
- **Posizioni Aperte**: Snapshot delle posizioni correnti
- **Metriche HFT**: Latenza, CPU, RAM
- **Decisioni AI**: Storico delle analisi di regime

## Configurazione

### config.yaml

```yaml
risk:
  max_portfolio_leverage: 4.0
  max_daily_loss_pct: 0.10
  max_risk_per_trade_pct: 0.007

strategies:
  hft:
    mmr_hft:
      enabled: true
      symbols: [BTC, ETH]
      deviation_threshold_pct: 0.001
      max_deviation_pct: 0.003

openai:
  enabled: true
  model: deepseek-reasoner
  base_url: https://api.deepseek.com/v3.2_speciale_expires_on_20251215
  regime_detection_interval_minutes: 5
```

## Exchange

Il bot opera su **Hyperliquid**:
- Mainnet: https://hyperliquid.xyz
- Testnet: https://testnet.hyperliquid.xyz

### Fee Structure
- **Maker**: 0.02%
- **Taker**: 0.05%

Il bot utilizza esclusivamente ordini **maker** (ALO - Add Liquidity Only) per minimizzare le fee.

## Licenza

MIT License

## Autore

Francesco Carlesi
