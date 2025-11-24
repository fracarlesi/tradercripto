# Cosa Chiediamo all'API Hyperliquid e Perché

**Documento**: Guida completa per capire TUTTE le chiamate API Hyperliquid
**Data**: 2025-11-14
**Audience**: Non-tecnico

---

## 🎯 Overview

Il sistema fa **2 tipi di operazioni** con Hyperliquid:

1. **📊 LETTURA** - Recupera dati dal mercato (prezzi, posizioni, candele)
2. **💸 SCRITTURA** - Esegue ordini (compra, vendi, modifica leverage)

**Hyperliquid API**: Gateway per accedere all'exchange decentralizzato

---

## 📊 PARTE 1: LETTURA (Info API)

### 1.1 `info.meta()` - Lista Tutti i Simboli Disponibili

**Cosa fa**: Recupera metadati di TUTTI i symbols tradabili su Hyperliquid

**Quante volte**: 1 volta all'avvio del sistema

**Perché serve**:
- Sapere quali crypto sono disponibili per trading
- Esempio output: `["BTC", "ETH", "SOL", "DOGE", ... 220+ altri]`

**Peso API**: 20
**Usato in**:
- `hourly_momentum.py` - Per sapere quali coins analizzare
- `ai_decision_service.py` - Per validare che il symbol esista

**Esempio**:
```python
meta = info.meta()
all_coins = [asset["name"] for asset in meta["universe"]]
# Risultato: ["BTC", "ETH", "SOL", ...] (220+ coins)
```

---

### 1.2 `info.all_mids()` - Prezzo Corrente di TUTTE le Crypto

**Cosa fa**: Recupera il prezzo mid (media bid-ask) di TUTTI i 220+ symbols in UNA SOLA chiamata

**Quante volte**:
- Ogni 3 minuti (ciclo AI trading)
- Ogni 30 secondi (sync account)

**Perché serve**:
- Calcolare valore corrente del portfolio
- Calcolare P&L delle posizioni aperte
- Validare prezzi per ordini

**Peso API**: 2 (molto leggero!)
**Usato in**:
- `auto_trader.py` - Per costruire portfolio snapshot
- `hyperliquid_sync_service.py` - Per calcolare valore posizioni

**Esempio**:
```python
all_mids = info.all_mids()
# Risultato: {
#     "BTC": 95234.50,
#     "ETH": 3421.80,
#     "SOL": 142.33,
#     ...
# }
```

**Nota**: Questa è la chiamata PIÙ EFFICIENTE - 220+ prezzi con 1 sola API call!

---

### 1.3 `info.candles_snapshot()` - Candele Storiche per 1 Symbol

**Cosa fa**: Recupera candele OHLCV (Open, High, Low, Close, Volume) per UN singolo symbol

**Quante volte**:
- **220 volte ogni 3 minuti** (hourly momentum - PESANTE!)
- **20 volte ogni 3 minuti** (technical analysis - medio)
- **20 volte ogni 3 minuti** (pivot points - medio)

**Perché serve**:
- **Hourly Momentum**: Calcola % change ultima ora per trovare top performers
- **Technical Analysis**: Calcola RSI, MACD su 24 candele orarie
- **Pivot Points**: Calcola support/resistance su 24 candele orarie

**Peso API**: 20 (PESANTE!)
**Usato in**:
- `hourly_momentum.py` - **220 chiamate!**
- `hyperliquid_market_data.py` - 20 chiamate (solo top symbols)
- `pivot_calculator.py` - 20 chiamate (solo top symbols)

**Esempio**:
```python
# Per BTC, prendi ultime 24 candele orarie
candles = info.candles_snapshot(
    name="BTC",
    interval="1h",  # Candele da 1 ora
    startTime=start_time_ms,  # 24 ore fa
    endTime=end_time_ms       # Adesso
)

# Risultato: [
#   {
#     "t": 1699920000000,  # Timestamp
#     "o": 95100.0,        # Open
#     "h": 95300.0,        # High
#     "l": 94900.0,        # Low
#     "c": 95200.0,        # Close
#     "v": 1234.5          # Volume
#   },
#   ... altre 23 candele
# ]
```

**⚠️ QUESTO È IL PROBLEMA**:
- 220 symbols × 20 weight = **4400 weight**
- Hyperliquid limit: **1200 weight/minuto**
- **Sforamento: 3.6x oltre il limite!**

---

### 1.4 `info.user_state()` - Stato Account (Balance, Posizioni, Ordini)

**Cosa fa**: Recupera TUTTO lo stato del tuo account Hyperliquid

**Quante volte**:
- Ogni 30 secondi (sync job)
- Ogni 60 secondi (stop-loss check)
- Ogni 60 secondi (take-profit check)
- Ogni 3 minuti (AI trading cycle)

**Perché serve**:
- Sapere quanto cash hai disponibile
- Vedere posizioni aperte (LONG/SHORT)
- Calcolare P&L unrealized
- Verificare ordini pendenti

**Peso API**: 2 (leggero)
**Usato in**:
- `hyperliquid_sync_service.py` - Sync posizioni/balance
- `auto_trader.py` - Check stop-loss/take-profit
- `portfolio_snapshot_service.py` - Snapshot per charts

**Esempio output**:
```json
{
  "marginSummary": {
    "accountValue": "188.26",        // Valore totale account
    "totalMarginUsed": "42.01",      // Margine utilizzato
    "withdrawable": "146.25"         // Cash prelevabile
  },
  "assetPositions": [
    {
      "position": {
        "coin": "BTC",
        "szi": "0.001",               // Size (+LONG, -SHORT)
        "entryPx": "95000.0",         // Prezzo entrata
        "unrealizedPnl": "0.234",     // P&L non realizzato
        "leverage": {
          "type": "cross",
          "value": 2                  // Leverage 2x
        }
      }
    }
  ]
}
```

---

## 💸 PARTE 2: SCRITTURA (Exchange API)

### 2.1 `exchange.market_order()` - Piazza Ordine di Mercato

**Cosa fa**: Compra o vende crypto IMMEDIATAMENTE al prezzo di mercato

**Quante volte**:
- 0-1 volta ogni 3 minuti (solo se AI decide di tradare)

**Perché serve**:
- Eseguire decisioni dell'AI (LONG/SHORT)
- Chiudere posizioni (take-profit, stop-loss)

**Peso API**: 1 (leggero)
**Usato in**:
- `trading_commands.py` - Execute AI decision
- `auto_trader.py` - Stop-loss/take-profit automation

**Esempio**:
```python
# AI decide: BUY 0.001 BTC con leverage 2x
order_result = exchange.market_order(
    coin="BTC",
    is_buy=True,        # LONG
    sz=0.001,           # Quantity
    reduce_only=False   # Apri nuova posizione
)

# Risultato:
{
  "status": "ok",
  "response": {
    "type": "order",
    "data": {
      "statuses": [
        {
          "filled": {
            "totalSz": "0.001",
            "avgPx": "95234.50"
          }
        }
      ]
    }
  }
}
```

---

### 2.2 `exchange.update_leverage()` - Modifica Leverage

**Cosa fa**: Cambia il leverage (1x-50x) per un symbol PRIMA di aprire posizione

**Quante volte**:
- 0-1 volta ogni 3 minuti (solo se AI apre posizione con leverage >1)

**Perché serve**:
- Hyperliquid richiede di impostare leverage PRIMA di tradare
- Default leverage è 1x se non specificato

**Peso API**: 1 (leggero)
**Usato in**:
- `hyperliquid_trading_service.py:place_market_order_async()`

**Esempio**:
```python
# Imposta leverage 2x su BTC prima di comprare
exchange.update_leverage(
    name="BTC",
    leverage=2,
    is_cross=True  # Cross-margin (usa tutto il balance come collateral)
)
```

---

## 🔄 FLUSSO COMPLETO - Ciclo AI Trading (Ogni 3 Minuti)

Ecco ESATTAMENTE cosa succede ogni 3 minuti:

### STEP 1: Hourly Momentum (12-15 secondi)
```
1. info.meta()              × 1    = 20 weight   [Lista tutti i symbols]
2. info.candles_snapshot()  × 220  = 4400 weight [Candele 1h per ogni coin]
   ↓
Risultato: Top 20 coins con momentum score più alto
```

### STEP 2: Technical Analysis (5-7 secondi)
```
3. info.candles_snapshot()  × 20   = 400 weight  [Candele 1h per top 20]
   ↓
Calcolo RSI, MACD, support/resistance
```

### STEP 3: Pivot Points (3-5 secondi)
```
4. info.candles_snapshot()  × 20   = 400 weight  [Candele 1h per top 20]
   ↓
Calcolo PP, R1-R3, S1-S3
```

### STEP 4: Portfolio Snapshot (1 secondo)
```
5. info.user_state()        × 1    = 2 weight    [Balance, posizioni]
6. info.all_mids()          × 1    = 2 weight    [Prezzi correnti]
   ↓
Portfolio: cash disponibile, posizioni aperte, P&L
```

### STEP 5: AI Decision (DeepSeek) (2-3 secondi)
```
AI riceve:
- Top 20 coins con technical analysis
- Portfolio corrente
- Prezzi di mercato

AI decide:
- BUY/SHORT/HOLD
- Symbol
- Size (% del portfolio)
- Leverage
```

### STEP 6: Order Execution (1-2 secondi)
```
Se AI decide BUY/SHORT:
7. exchange.update_leverage() × 1   = 1 weight   [Imposta leverage]
8. exchange.market_order()    × 1   = 1 weight   [Esegue ordine]

Se AI decide HOLD:
   Nessun ordine
```

### STEP 7: Post-Trade Sync (2-3 secondi)
```
9. info.user_state()        × 1    = 2 weight    [Verifica ordine]
   ↓
Aggiorna database con nuova posizione
```

---

## 📊 TOTALE PESO API PER CICLO

```
┌────────────────────────────────────────────────────────────┐
│ Operazione              │ Calls │ Weight │ Total │ % Time │
├────────────────────────────────────────────────────────────┤
│ Hourly Momentum         │  221  │   20   │ 4420  │  50%   │
│ Technical Analysis      │   20  │   20   │  400  │  20%   │
│ Pivot Points            │   20  │   20   │  400  │  15%   │
│ Portfolio Snapshot      │    2  │    2   │    4  │   5%   │
│ Order Execution         │  0-2  │    1   │  0-2  │   5%   │
│ Post-Trade Sync         │  0-1  │    2   │  0-2  │   5%   │
├────────────────────────────────────────────────────────────┤
│ TOTAL                   │  263  │        │ 5226  │ 100%   │
└────────────────────────────────────────────────────────────┘

Durata totale ciclo: ~30-35 secondi
Peso distribuito su 30s: 5226 / 30s = 174 weight/s = 10,440 weight/min ❌
```

**PROBLEMA**: **8.7x OLTRE il limite di 1200 weight/min!**

---

## ⚠️ Perché Non È Crashato Prima?

1. **Latenza API naturale** (~100-200ms per call)
   - 263 calls × 150ms = 39s effective time
   - Actual rate: 5226 / 39s = 134 weight/s = **8040 weight/min** (ancora 6.7x!)

2. **Rate limiting interno**:
   ```python
   if (i + 1) % 10 == 0:
       await asyncio.sleep(0.1)  # 100ms delay ogni 10 calls
   ```
   - Aggiunge 2.2s di delay (insufficiente!)

3. **Error handling**:
   ```python
   except Exception as e:
       errors += 1  # Cattura 429 errors e continua
       continue
   ```

4. **CloudFront CDN** caching (Hyperliquid usa CloudFront)

---

## ✅ SOLUZIONE: Ridurre Hourly Momentum

### Problema Attuale
```
220 symbols × 20 weight = 4400 weight
Distributed over 15-30s = 8800-17600 weight/min ❌
```

### Soluzione Proposta
```
100 symbols × 20 weight = 2000 weight
Distributed over 120s (1.2s delay/call) = 1000 weight/min ✅
```

**Modifica**:
1. Pre-filtra per volume 24h > $50k (220 → ~100 symbols)
2. Delay da 0.1s → 1.2s per call
3. Tempo calcolo: 2 minuti (vs 15s)
4. Ciclo AI totale: 5 minuti (vs 3min, ma STABILE)

---

## 📈 Budget Finale (Dopo Fix)

```
Per-Minute Weight Distribution:

┌───────────────────────────────────────────────────────────┐
│ Job                      │ Weight │ Calls/min │ Total     │
├───────────────────────────────────────────────────────────┤
│ Hourly Momentum (100)    │  2000  │   0.50    │   400     │
│ Technical Analysis (20)  │   400  │   0.33    │     7     │
│ Pivot Points (20)        │   400  │   0.33    │     7     │
│ Sync Jobs (30s, 60s)     │    6   │   3.00    │    12     │
│ AI Decision + Order      │    2   │   0.20    │     0     │
├───────────────────────────────────────────────────────────┤
│ TOTAL                    │        │           │   426     │
│ HYPERLIQUID LIMIT        │        │           │  1200     │
│ USAGE                    │        │           │  35.5%    │
│ MARGIN                   │        │           │  64.5%    │
└───────────────────────────────────────────────────────────┘
```

✅ **SAFE**: 426 weight/min (35.5% del limite)

---

## 🎯 Riepilogo

**Cosa chiediamo all'API**:
1. **Lista coins disponibili** - 1 volta all'avvio
2. **Prezzi correnti** - Ogni 30s-3min (EFFICIENTE - 1 call = 220+ prezzi)
3. **Candele storiche** - Ogni 3min (PESANTE - 220 calls!)
4. **Stato account** - Ogni 30-60s
5. **Ordini** - Solo quando AI decide di tradare (0-1 ogni 3min)

**Problema principale**:
- Hourly momentum chiama `candles_snapshot` 220 volte
- Sfora rate limit di 6-8x

**Soluzione**:
- Riduci a 100 symbols (pre-filtra per volume)
- Aumenta delay a 1.2s per call
- Distribuzione su 2 minuti → 1000 weight/min ✅ SAFE

**Impatto**:
- Ciclo AI: 3min → 5min (ancora 2x più veloce del vecchio sistema)
- Zero rischio 429 errors
- Sistema stabile
