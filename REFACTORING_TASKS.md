# HLQuantBot - Piano di Refactoring Completo

> **Obiettivo**: Sistema "boring but scalable" su 2-3 asset liquidi, pochi trade di qualità, rischio controllato, LLM come filtro.

---

## Obiettivi e KPI Target

| Metrica | Target |
|---------|--------|
| Asset | BTC, ETH (opz. SOL) |
| Timeframe | 4h (opz. 1h) |
| Trade/mese | 5-15 |
| Rischio/trade | 0.5% (max 1%) |
| Max Drawdown | 10-15% |
| Profit Factor | > 1.2 |
| Target mensile | 1-3% medio |

---

## FASE 0: Preparazione ✅ COMPLETATA

### 0.1 Setup branch e cleanup
- [x] Creare branch `feature/conservative-refactor`
- [x] Documentare stato attuale del sistema
- [x] Backup configurazioni esistenti
- [x] Definire struttura directory finale

### 0.2 Definizione contratti dati
- [x] Definire schema `MarketState` (OHLCV + indicatori)
- [x] Definire schema `Setup` (candidato trade)
- [x] Definire schema `TradeIntent` (long/short/flat + livelli)
- [x] Definire schema `RiskParams` (size, stop, trail)
- [x] Validazione con Pydantic v2

---

## FASE 1: Core Backend - Market State ✅ COMPLETATA

### 1.1 Nuovo config `config/trading.yaml`
- [x] Sezione `universe`: [BTC, ETH]
- [x] Sezione `timeframes`: [4h] (primario), [1h] (secondario)
- [x] Sezione `risk`:
  ```yaml
  risk:
    per_trade_pct: 0.5
    max_positions: 2
    max_exposure_pct: 100
    leverage: 1
  ```
- [x] Sezione `stops`:
  ```yaml
  stops:
    initial_atr_mult: 2.5
    trailing_atr_mult: 2.5
  ```
- [x] Sezione `kill_switch`:
  ```yaml
  kill_switch:
    daily_loss_pct: 2.0
    weekly_loss_pct: 5.0
    max_drawdown_pct: 15.0
  ```

### 1.2 Servizio `MarketStateService`
- [x] Sostituisce `MarketScannerService`
- [x] Fetch OHLCV solo per BTC/ETH (200 barre, 4h)
- [x] Calcolo indicatori:
  - [x] ATR(14)
  - [x] EMA(50), EMA(200)
  - [x] ADX(14)
  - [x] RSI(14)
  - [x] Slope EMA200
  - [x] Choppiness Index (opzionale)
- [x] Pubblica su topic `MARKET_STATE` ogni 4h
- [x] Cache locale per evitare chiamate ridondanti

### 1.3 Servizio `RegimeDetectorService`
- [x] Input: `MarketState`
- [x] Output: `TREND` | `RANGE` | `CHAOS`
- [x] Logica:
  ```python
  if adx > 25 and ema200_slope > 0:
      regime = TREND
  elif adx < 20 and choppiness > 60:
      regime = RANGE
  else:
      regime = CHAOS
  ```
- [x] Isteresi: regime cambia solo dopo N barre conferma
- [x] Pubblica su topic `REGIME`

---

## FASE 2: Core Backend - Strategy ✅ COMPLETATA

### 2.1 Strategia Core: `TrendFollowStrategy`
- [x] Attiva SOLO se regime == TREND
- [x] Entry conditions:
  - [x] Prezzo > EMA200
  - [x] Breakout massimo N barre (es. 20)
  - [x] ATR > media ATR (regime "active")
  - [x] Volume conferma (opzionale)
- [x] Exit:
  - [x] Stop iniziale: 2.5 ATR
  - [x] Trailing stop: 2.5 ATR dal max favorevole
  - [x] NO take profit fisso
- [x] Output: `Setup` con entry_price, stop_price, direction

### 2.2 Strategia Secondaria: `MeanReversionStrategy` (opzionale)
- [x] Attiva SOLO se regime == RANGE
- [x] Entry: estremi Bollinger + RSI oversold/overbought
- [x] Exit: mid-band
- [x] Stop: ATR-based
- [x] **Disabilitata di default** fino a validazione

### 2.3 Strategy Selector (semplificato)
- [x] NON usa LLM per scegliere
- [x] Logica deterministica:
  ```python
  if regime == TREND:
      strategy = TrendFollowStrategy
  elif regime == RANGE and mean_reversion_enabled:
      strategy = MeanReversionStrategy
  else:
      strategy = None  # FLAT
  ```
- [x] Pubblica `Setup` su topic `SETUPS`

---

## FASE 3: Core Backend - Risk Management ✅ COMPLETATA

### 3.1 Servizio `RiskManagerService`
- [x] Calcolo size risk-based:
  ```python
  risk_amount = equity * risk_per_trade_pct
  stop_distance = abs(entry_price - stop_price) / entry_price
  position_size = risk_amount / stop_distance
  ```
- [x] Controlli:
  - [x] Max posizioni totali
  - [x] Max exposure notional
  - [x] Correlazione (se BTC e ETH insieme)
- [x] Output: `TradeIntent` con size calcolata

### 3.2 Kill-Switch Service
- [x] Monitor continuo equity curve
- [x] Trigger levels:
  - [x] Daily loss > 2% → pause fino domani
  - [x] Weekly loss > 5% → pause 3 giorni
  - [x] Max DD > 15% → STOP TOTALE + alert
- [x] Stato persistito in DB
- [x] Alert Telegram su trigger

### 3.3 Database: tabelle risk
- [x] `equity_snapshots` (timestamp, equity, drawdown)
- [x] `kill_switch_events` (timestamp, trigger_type, action)
- [x] `daily_pnl` (date, pnl, trades_count)

---

## FASE 4: Core Backend - Execution ✅ COMPLETATA

### 4.1 Refactoring `ExecutionEngine`
- [x] Entry preferisce limit post-only se spread ok
- [x] Fallback market con max slippage check
- [x] Stop server-side IMMEDIATO dopo fill
- [x] Trailing stop gestito da bot con fallback safety

### 4.2 Order types supportati
- [x] `LIMIT_POST_ONLY` - entry preferita
- [x] `MARKET` - fallback con slippage check
- [x] `STOP_MARKET` - stop loss server-side
- [x] `TRAILING_STOP` - gestito bot-side con monitor

### 4.3 Slippage protection
- [x] Max slippage tollerato: 0.1%
- [x] Se superato: log + retry con size ridotta
- [x] Metriche slippage salvate per analisi

---

## FASE 5: LLM Integration (Veto Mode) ✅ COMPLETATA

### 5.1 Servizio `LLMVetoService`
- [x] Input: `MarketState` + `Setup` proposto
- [x] Prompt template rigido:
  ```
  Analizza questo setup:
  - Asset: {asset}
  - Regime calcolato: {regime}
  - Setup: {setup_type} @ {entry_price}
  - Indicatori: ADX={adx}, RSI={rsi}, ATR={atr}

  Rispondi SOLO con JSON:
  {"decision": "ALLOW|DENY", "confidence": 0.0-1.0, "reason": "..."}
  ```
- [x] Validazione JSON output (fallback = DENY)
- [x] Rate limit: max 6 chiamate/giorno (1 per 4h window)

### 5.2 Integrazione nel flow
- [x] Setup passa a LLM SOLO se regime != CHAOS
- [x] Se DENY → log reason, no trade
- [x] Se ALLOW → procedi a sizing
- [x] Metriche: track accuracy veto vs outcome

### 5.3 Fallback senza LLM
- [x] Se LLM non disponibile → trade permesso (regole deterministiche già filtrano)
- [x] Log warning "LLM unavailable, using rules-only"

---

## FASE 6: Learning Module (Offline) ⏸️ POSTICIPATA

> Nota: Il learning offline sarà implementato in una fase successiva dopo validazione paper trading.

### 6.1 Refactoring Learning
- [ ] RIMUOVERE ottimizzazione live dei parametri
- [x] Solo calcolo metriche in produzione:
  - [x] Win rate per strategia
  - [x] Profit factor
  - [x] Average R (reward/risk)
  - [x] Slippage medio
  - [x] Fee + funding pagati

### 6.2 Tool offline `tools/walk_forward.py`
- [ ] Input: storico trades + OHLCV
- [ ] Walk-forward validation:
  - [ ] Train su 3 mesi, test su 1 mese
  - [ ] Rolling window
- [ ] Parametri testati:
  - [ ] Breakout period: [10, 20, 30]
  - [ ] ATR multiplier: [2.0, 2.5, 3.0]
  - [ ] ADX threshold: [20, 25, 30]
- [ ] Output: parametri più stabili (non più profittevoli)

### 6.3 Processo update parametri
- [ ] Run walk-forward settimanale (manuale o cron)
- [ ] Review umano dei risultati
- [ ] Update config SOLO se miglioramento significativo
- [ ] Versioning config in git

---

## FASE 7: Message Bus Refactoring ✅ COMPLETATA

### 7.1 Nuovi Topic
```python
class Topic(Enum):
    MARKET_STATE = "market_state"      # OHLCV + indicatori
    REGIME = "regime"                   # TREND/RANGE/CHAOS
    SETUPS = "setups"                   # Candidati trade
    TRADE_INTENT = "trade_intent"       # Trade sized e approvato
    ORDERS = "orders"                   # Ordini inviati
    FILLS = "fills"                     # Ordini eseguiti
    RISK_ALERTS = "risk_alerts"         # Kill-switch, warnings
    METRICS = "metrics"                 # Performance data
```

### 7.2 Rimozione topic obsoleti
- [x] Mantenuti per backward compatibility (deprecati)
- [x] Nuovi topic aggiunti all'enum
- [x] Documentazione aggiornata

---

## FASE 8: Database Refactoring ✅ COMPLETATA

### 8.1 Nuove tabelle
```sql
-- Stato mercato storico
CREATE TABLE market_states (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    timeframe VARCHAR(10) NOT NULL,
    open DECIMAL(20,8),
    high DECIMAL(20,8),
    low DECIMAL(20,8),
    close DECIMAL(20,8),
    volume DECIMAL(20,8),
    atr DECIMAL(20,8),
    adx DECIMAL(10,4),
    rsi DECIMAL(10,4),
    ema50 DECIMAL(20,8),
    ema200 DECIMAL(20,8),
    regime VARCHAR(10)
);

-- Equity curve
CREATE TABLE equity_curve (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    equity DECIMAL(20,2) NOT NULL,
    drawdown_pct DECIMAL(10,4),
    positions_count INT
);

-- Kill switch log
CREATE TABLE kill_switch_log (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    trigger_type VARCHAR(20) NOT NULL,
    trigger_value DECIMAL(10,4),
    action_taken VARCHAR(50)
);
```

### 8.2 Migrazione dati esistenti
- [x] Backup tabelle esistenti
- [x] Migrazione 005_conservative_refactor.sql creata
- [x] Schema aggiornato

---

## FASE 9: Frontend Refactoring ✅ COMPLETATA

### 9.1 Dashboard principale - Layout nuovo
- [x] Header con:
  - [x] Equity totale
  - [x] Daily P&L (con colore)
  - [x] Drawdown corrente
  - [x] Kill-switch status (OK/WARNING/STOPPED)
- [x] Sidebar con navigazione

### 9.2 Pagina "Market State"
- [x] Card per ogni asset (BTC, ETH)
- [x] Indicatori live:
  - [x] Prezzo corrente
  - [x] Regime (badge colorato: TREND=verde, RANGE=giallo, CHAOS=rosso)
  - [x] ADX, RSI, ATR
  - [x] EMA50/200 con trend arrow
- [x] HTMX polling ogni 5 min

### 9.3 Pagina "Active Trades"
- [x] Tabella posizioni aperte:
  - [x] Symbol, Direction, Size
  - [x] Entry price, Current price
  - [x] Stop price (con distanza %)
  - [x] Unrealized P&L
  - [x] Time in trade
- [x] Pulsante "Close All" (con conferma)

### 9.4 Pagina "Trade History"
- [x] Filtri: asset, date range, strategy
- [x] Tabella trades chiusi:
  - [x] Entry/Exit datetime
  - [x] P&L (lordo e netto)
  - [x] R multiple (reward/risk)
  - [x] Holding time
  - [x] Strategy usata
- [x] Statistiche aggregate in header

### 9.5 Pagina "Risk Monitor"
- [x] Drawdown chart
- [x] Kill-switch panel:
  - [x] Daily loss: barra progresso
  - [x] Weekly loss: barra progresso
  - [x] Max DD: barra progresso
  - [x] Status attuale
- [x] Log ultimi eventi risk

### 9.6 Pagina "LLM Decisions"
- [x] Log decisioni LLM:
  - [x] Timestamp
  - [x] Setup proposto
  - [x] Decision (ALLOW/DENY)
  - [x] Confidence
  - [x] Reason
- [x] Statistiche:
  - [x] ALLOW vs DENY ratio
  - [x] Accuracy (trades ALLOWED che erano profittevoli)

### 9.7 Pagina "Settings"
- [x] Vista config corrente (read-only)
- [x] Status servizi

### 9.8 Componenti UI riutilizzabili
- [x] `partials/market_state_*.html` - cards mercato
- [x] `partials/kill_switch_*.html` - stato kill-switch
- [x] `partials/trade_history_table.html` - tabella trade
- [x] `partials/llm_decisions_table.html` - log decisioni

### 9.9 Stile e UX
- [x] Dark mode default (trading style)
- [x] Colori consistenti:
  - [x] Verde: profit, TREND, OK
  - [x] Rosso: loss, stop, WARNING
  - [x] Giallo: RANGE, caution
  - [x] Grigio: CHAOS, disabled
- [x] Loading states con skeleton

---

## FASE 10: Testing ✅ COMPLETATA

### 10.1 Unit tests
- [x] Test `RegimeDetector` con casi edge
- [x] Test `RiskManager` sizing calculation
- [x] Test `KillSwitch` trigger logic
- [x] Test `LLMVeto` JSON parsing
- [x] Test file `test_conservative.py` creato

### 10.2 Integration tests
- [x] Flow completo: MarketState → Setup → Trade
- [x] Kill-switch integration
- [x] Database persistence

### 10.3 Paper trading
- [ ] 2 settimane su testnet (PROSSIMO STEP)
- [ ] Verifica metriche corrette
- [ ] Verifica kill-switch funziona
- [ ] Stress test con volatilità simulata

---

## FASE 11: Deployment ⏳ IN CORSO

### 11.1 Preparazione produzione
- [x] Main orchestrator `main_conservative.py` creato
- [x] Documentazione `CONSERVATIVE_SYSTEM.md` creata
- [ ] Review sicurezza .env
- [ ] Backup strategy
- [ ] Monitoring setup (logs, alerts)
- [ ] Telegram alerts configurati

### 11.2 Rollout graduale
- [ ] Week 1-2: Paper trading
- [ ] Week 3-4: Live con $1k
- [ ] Week 5-8: Scale a $5k se DD ok
- [ ] Week 9+: Scale graduale

### 11.3 Documentazione
- [x] CONSERVATIVE_SYSTEM.md creato
- [ ] Runbook operativo
- [ ] Troubleshooting guide

---

## Stato Attuale

| Fase | Status |
|------|--------|
| Fase 0: Preparazione | ✅ COMPLETATA |
| Fase 1: Market State | ✅ COMPLETATA |
| Fase 2: Strategy | ✅ COMPLETATA |
| Fase 3: Risk Management | ✅ COMPLETATA |
| Fase 4: Execution | ✅ COMPLETATA |
| Fase 5: LLM Veto | ✅ COMPLETATA |
| Fase 6: Learning Offline | ⏸️ POSTICIPATA |
| Fase 7: Message Bus | ✅ COMPLETATA |
| Fase 8: Database | ✅ COMPLETATA |
| Fase 9: Frontend | ✅ COMPLETATA |
| Fase 10: Testing | ✅ COMPLETATA |
| Fase 11: Deployment | ⏳ IN CORSO |

**Completamento sviluppo**: 95%
**Prossimo step**: Paper trading su testnet

---

## Note Importanti

1. **NON ottimizzare parametri in produzione** - Solo metriche, learning offline
2. **Kill-switch NON negoziabile** - Deve funzionare SEMPRE
3. **LLM è opzionale** - Sistema deve funzionare senza
4. **Meno trade = meglio** - Qualità > Quantità
5. **Testare TUTTO su paper** - Mai live senza validazione

---

## Checklist Pre-Go-Live

- [x] Kill-switch implementato e testato
- [x] Stop loss server-side implementato
- [ ] Paper trading 2+ settimane completato
- [ ] Max DD mai superato in paper
- [ ] Telegram alerts funzionanti
- [x] Backup config in git (REFACTORING_TASKS.md)
- [ ] Monitoring attivo
- [ ] Piano di emergenza documentato

---

*Ultimo aggiornamento: 2026-01-01*
*Versione: 2.0 - Conservative Refactor Complete*
