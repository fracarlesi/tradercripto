# FLAG-Trader Logging Audit — Gap Analysis

**Data**: 2026-03-24
**Bot**: crypto_bot (Hyperliquid DEX, FLAG-Trader LLM)
**Scope**: Visibilità completa su quando e perché il LLM si attiva, cosa pensa, cosa decide

---

## FILE: crypto_bot/main.py

### Già loggato:
- `_load_config()`: carica YAML, log livello config (assets, risk %, max DD) ✓
- `_init_flag_trader()`: carica modello, log checkpoint path e device ✓
- Scan loop: `FLAG-Trader scan: N assets, M actionable decisions` — **conta decisioni ma NON filtra quali** ✓
- Error in `_evaluate_asset()`: warning con symbol e eccezione ✓

### Manca:
1. **Trigger evaluation** — Non logga QUALE evento ha scatenato la valutazione:
   - Scheduled scan ogni 5 min
   - Price move > 2%
   - Position PnL > threshold
   - Nuovo fill
   - RealTimeMonitor causa?
   - **Gap**: il log non dice PERCHÉ il bot sta valutando ADESSO

2. **Filtri/Gates applicati** — Non si vede quale simbolo:
   - È stato SALTATO dal filtro BTC macro (short blocks)
   - È stato rifiutato per cooldown
   - Non ha dati sufficienti
   - **Gap**: non si capisce il filtering logic applicato

3. **Prompt costruito** — Non logga il testo del prompt passato al modello
   - Solo logga risultato, non input
   - **Gap**: non si sa cosa il LLM ha letto (candles, portfolio, history, RAG context)

4. **Ragionamento model** — Non logga:
   - Token count del prompt (importante per verifica truncation)
   - Logits raw (prima di softmax)
   - Sampling distribution (Categorical dist.)
   - **Gap**: non si vede il "pensiero" interno del modello

5. **Config drift check** — Non logga se la config in memory corrisponde a quella su disco
   - **Gap**: difficile debuggare se `_config` è corrotta

---

## FILE: crypto_bot/flag_trader/agent.py

### Già loggato:
- `scan_and_decide()`: conteggio totale (N assets scanned, M actionable) ✓
- `_evaluate_asset()`: una riga per asset con action, value, TP%, SL% ✓
- `evaluate_position()`: EXIT eval con direction, model action, confidence, close decision ✓
- Decision recorded nella trade_logger JSONL ✓
- Exit decision recorded nella trade_logger JSONL ✓

### Manca:
1. **RAG context retrieval** — Non logga:
   - Quanti "similar trades" trovati in memory
   - Quali erano le condizioni dei trade storici
   - Quanto è stata utile la RAG (es. "RAG appended 300 chars")
   - **Gap**: non si vede il feedback storico che il modello riceve

2. **Confidence filtering** — Non logga:
   - La soglia (default 0.6)
   - I trade scartati per bassa confidence (action=BUY ma |value|=0.15 < 0.6)
   - **Gap**: non capire quanti trade buoni vengono filtrati

3. **Market state dict construction** — `_build_market_state_dict()`:
   - Logga che costruisce RSI/ATR/EMA9
   - Ma non mostra i **valori reali** (RSI=48, ATR=1.2%, EMA9_slope=+0.0015)
   - **Gap**: debugging su quale candle pattern ha portato a decisione

4. **Token length** — Nel `get_action()` del modello:
   - Non logga lunghezza tokens passata al tokenizer
   - Non dice se troncata (max_length=512)
   - **Gap**: silent truncation che affoga le candles recenti

5. **Position evaluation skip reasons** — Non logga:
   - Quando non ci sono candles sufficienti
   - Quando RAG ritorna zero storici
   - **Gap**: capire perché exit eval non è stato eseguito

---

## FILE: crypto_bot/flag_trader/prompt.py

### Già loggato:
- **ZERO**: non fa nessun logging

### Manca:
1. **Prompt costruito** — Non logga:
   - Le candele normalize
   - Portfolio state (cash, position, total)
   - Previous decision metrics (recent_rewards, net_values, actions history)
   - Similar trades text (lunghezza/riassunto)
   - Position info (se in exit eval)
   - **Gap**: non si sa cosa il modello sta leggendo

2. **Parsing output** — Nel `parse_action()`:
   - Non logga il raw output dal modello
   - Non logga le failure cases (JSON parse fail, regex fallback, default HOLD)
   - **Gap**: capire se il modello parla un linguaggio strano

3. **Normalization** — Nel `_normalize_candles()`:
   - Non logga la base_close
   - Non logga se truncate a candle_window
   - **Gap**: data quality verification

---

## FILE: crypto_bot/flag_trader/model.py

### Già loggato:
- **ZERO**: nessun logging nel modello stesso

### Manca:
1. **Tokenization** — In `get_action()`:
   - Non logga token count
   - Non logga se truncated
   - Non logga token_ids (primo/ultimo token)
   - **Gap**: capire se prompt è stato massacrato

2. **Forward pass** — In `forward()`:
   - Non logga hidden state shape/stats
   - Non logga logits raw (prima softmax)
   - Non logga value estimate
   - Non logga TP/SL head output raw
   - **Gap**: zero visibility sul forward pass

3. **Action sampling** — In `get_action()`:
   - Non logga distribution probabilities (P(Sell), P(Hold), P(Buy))
   - Non logga se action sampled o argmax
   - Non logga entropy della distribution
   - **Gap**: capire se il modello è confident o incerto

4. **Checkpoint loading** — Nel `load_trainable()`:
   - Non logga quanti layer vengono caricati
   - Non logga se frozen sono ancora frozen
   - **Gap**: debugging model degradation

---

## FILE: crypto_bot/services/realtime_monitor.py

### Già loggato:
- `start()`: log con poll_interval, cooldown, universe_count ✓
- `stop()`: log di shutdown ✓
- `set_universe()`: log universo aggiornato ✓

### Manca:
1. **Trigger detection** — Non logga:
   - Quando scatta scheduled scan (every 5 min)
   - Quali asset hanno PREZZO MOVE > 2% (e per quanto %)
   - Quali position hanno PnL change > 3%
   - Nuovo fill ID
   - **Gap**: non si sa qual è il trigger

2. **Price tracking** — In `_price_snapshots`:
   - Non logga snapshot corrente vs precedente
   - Non logga quali asset non hanno dati
   - **Gap**: data availability blindness

3. **Cooldown logic** — In `_min_trigger_cooldown`:
   - Non logga se trigger è stato cooldowned
   - Non logga ultimo trigger timestamp
   - **Gap**: capire perché la valutazione non è partita

---

## FILE: crypto_bot/flag_trader/trade_logger.py

### Già loggato:
- `log_decision()`: debug log decision con symbol, action, confidence ✓
- `update_outcome()`: info log trade outcome con symbol, action, PnL, exit_reason ✓

### Manca:
1. **Prompt storage** — Nel TradeRecord:
   - Non salva il prompt costruito
   - Non salva il raw model output (prima parse)
   - Non salva token count / truncation flag
   - **Gap**: retraining data è incomplete, non si sa cosa il modello leggeva

2. **Decision justification** — Non logga:
   - Perché il modello ha scelto BUY vs SELL (logits raw, confidence)
   - RAG context utilizzato
   - Candle pattern (oltre al riassunto numerico)
   - **Gap**: analysis dei decision patterns è cieco

3. **Exit reason details** — Nel TradeRecord:
   - `exit_reason` è string generico ("take_profit", "stop_loss", "timeout")
   - Non logga quale condition ha triggerato (TP hit exactly? broker network delay?)
   - **Gap**: debugging slippage / sync issues difficile

---

## FILE: crypto_bot/services/execution_engine.py

### Già loggato:
- Order submission: log order_id, symbol, side, size, price, status ✓
- Fill events: log symbol, side, filled_size, avg_price, fee ✓
- TP/SL placement: log symbol, tp_price, sl_price
- Position tracking: log position status updates

### Manca:
1. **Order routing decision** — Non logga:
   - Maker vs taker? (prefer_limit?→market?)
   - Spread snapshot al momento dell'order
   - Slippage limit check
   - **Gap**: capire se i trade vengono eseguiti al prezzo corretto

2. **Rejection reasons** — Non logga:
   - OrderRejectedError: Cosa ha causato (OI cap? Spread too wide? Network?)
   - Retry count e backoff delays
   - **Gap**: debugging failure difficile, solo warning generico

3. **Partial fill handling** — Non logga:
   - Fill progression (0.5 filled, 0.75 filled, 1.0 filled)
   - Timing tra fill e TP/SL setup
   - Se TP/SL sono "live" o pending
   - **Gap**: capire se la posizione è fully protected al momento di ogni fill

---

## FILE: crypto_bot/services/risk_manager.py

### Già loggato:
- Initialization: risk config, max_pos, max_exposure ✓
- Equity updates: equity amount ✓
- Cooldown checks: symbol cooldown status
- Position sync: synced N positions

### Manca:
1. **Position sizing logic** — Non logga:
   - `risk_amount = equity * per_trade_pct` (per-trade risk USD)
   - `stop_distance_pct` calcolato da SL%
   - `position_size = risk_amount / stop_distance` (formula)
   - **Gap**: non si vede il calcolo della size

2. **Rejection gates** — Non logga:
   - Max positions reached (attuale vs limit)
   - Max exposure exceeded (attuale vs limit)
   - Correlation check failed (quale asset correlato?)
   - Daily trade limit reached (N trades today vs limit)
   - Per-symbol trade limit (N trades on symbol today vs limit)
   - **Gap**: non si sa perché il trade è stato rifiutato

3. **Cooldown state** — Non logga:
   - Cooldown durata (active until T)
   - Cooldown reason (qual'era il precedente trade? quando?)
   - Cooldown file load/save status
   - **Gap**: difficile capire perché un asset è in cooldown

4. **Correlation groups** — Non logga:
   - Quali correlazioni sono attive
   - Score di correlazione (se calcolato)
   - **Gap**: black box sulla correlation logic

---

## SOMMARIO: TOP 5 LOGGING GAPS

| # | Gap | File | Impatto | Priorità |
|---|-----|------|---------|----------|
| 1 | **Trigger causa** (perché LLM è stato eval adesso?) | main.py / realtime_monitor.py | Capire flusso bot | CRITICA |
| 2 | **Prompt costruito** (cosa il modello ha letto) | prompt.py / trade_logger.py | Debug model input | CRITICA |
| 3 | **Model reasoning** (logits, distribution, confidence) | model.py / agent.py | Capire decisioni | ALTA |
| 4 | **Rejection gates** (perché il trade è stato rifiutato) | risk_manager.py / execution_engine.py | Debug flow | ALTA |
| 5 | **Order execution details** (slippage, retry, partial fill) | execution_engine.py | Debug trade quality | MEDIA |

---

## RACCOMANDAZIONI

**Logging layer ideal per completezza:**

1. **RealtimeMonitorService** — Log ogni trigger con dettagli
2. **agent._evaluate_asset()** — Log prompt, raw model output, confidence delta
3. **prompt.build_prompt()** — Log candle summary, portfolio, RAG context
4. **model.get_action()** — Log tokens, logits, distribution probs, value
5. **RiskManagerService** — Log sizing formula, rejection gates con motivi
6. **ExecutionEngineService** — Log order routing, slippage, fill progression

**Format raccomandato:**
```json
{
  "timestamp": "2026-03-24T15:32:10Z",
  "event": "FLAG_TRADER_EVAL",
  "trigger": "scheduled_scan",
  "symbol": "BTC",
  "prompt_tokens": 412,
  "prompt_truncated": false,
  "candles": 20,
  "logits": [0.2, 0.5, 0.3],
  "action": "BUY",
  "confidence": 0.52,
  "log_prob": -0.65,
  "tp_pct": 2.5,
  "sl_pct": 1.0
}
```

Questo darebbe **visibilità end-to-end** sul flusso di decisione del LLM.
