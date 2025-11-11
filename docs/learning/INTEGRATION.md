# Sistema di Apprendimento Controfattuale - Guida Integrazione

## 📋 STATO IMPLEMENTAZIONE

### ✅ COMPLETATO (1200+ righe):
1. ✅ Database migration `005_add_decision_snapshots.sql`
2. ✅ Modello `DecisionSnapshot` in `database/models.py`
3. ✅ `services/learning/decision_snapshot_service.py` (350 righe)
4. ✅ `services/learning/deepseek_self_analysis_service.py` (350 righe)
5. ✅ `api/learning_routes.py` (200 righe)
6. ✅ Router registrato in `main.py`

### 🚧 DA COMPLETARE (3 modifiche):

---

## 🔧 MODIFICA 1: Integra snapshot in `ai_decision_service.py`

**File**: `backend/services/ai_decision_service.py`

**Cosa fare**: Aggiungi import e salva snapshot dopo ogni decisione AI.

### Step 1.1: Aggiungi import (all'inizio del file, dopo gli altri import)

```python
# Import learning services for counterfactual analysis
from services.learning import save_decision_snapshot
```

### Step 1.2: Modifica `call_ai_for_decision()` (cerca la riga ~950 dove ritorna la risposta)

Cerca questa sezione (alla fine della funzione):

```python
    # Return parsed decision
    return decision
```

**SOSTITUISCI CON**:

```python
    # Save decision snapshot for counterfactual learning
    try:
        # Build indicators snapshot for analysis
        indicators_snapshot = {
            "weights": weights,
            "prophet": {
                "forecast_24h": None,  # Extract from prophet_section if available
                "trend": None,
                "confidence": None,
            },
            "pivot_points": {
                "signal": None,  # Extract from pivot_section if available
            },
            "rsi_macd": {
                "rsi": None,  # Extract from technical_section if available
                "macd": None,
            },
            "whale_alerts": {
                "alerts_count": 0,  # Extract from whale_section if available
            },
            "sentiment": {
                "fear_greed_index": None,  # Extract from sentiment_section if available
            },
            "news": {
                "news_count": 0,  # Extract from news_section if available
            },
            "prices": prices,
            "portfolio_value": portfolio.get("total_assets", 0),
        }

        # Get current price for symbol from decision
        symbol = decision.get("symbol", "BTC")  # Default to BTC
        entry_price = prices.get(symbol, 0.0)

        # Save snapshot asynchronously (don't block on errors)
        await save_decision_snapshot(
            account_id=account.id,
            symbol=symbol,
            indicators_snapshot=indicators_snapshot,
            deepseek_reasoning=reasoning,  # Full reasoning text from AI
            actual_decision=decision.get("action", "HOLD"),  # LONG, SHORT, HOLD
            actual_size_pct=decision.get("size", 0.0),
            entry_price=entry_price,
        )

        logger.info(
            f"Decision snapshot saved for {symbol}: {decision.get('action')} "
            f"(account_id={account.id})"
        )

    except Exception as e:
        # Don't fail the trade if snapshot save fails
        logger.error(
            f"Failed to save decision snapshot: {e}",
            extra={"context": {"account_id": account.id, "error": str(e)}},
            exc_info=True,
        )

    # Return parsed decision
    return decision
```

**NOTA**: Questa modifica salva OGNI decisione (anche HOLD) con il reasoning completo.

---

## 🔧 MODIFICA 2: Schedula batch jobs in `startup.py`

**File**: `backend/services/startup.py`

### Step 2.1: Aggiungi import (all'inizio del file)

```python
from services.learning import calculate_counterfactuals_batch, run_self_analysis
```

### Step 2.2: Aggiungi funzioni wrapper (dopo le funzioni esistenti)

Aggiungi PRIMA di `initialize_services()`:

```python
# ============================================================================
# Counterfactual Learning Jobs
# ============================================================================


async def calculate_counterfactuals_wrapper():
    """
    Wrapper for counterfactual calculation batch job.

    Calculates counterfactual P&L for decision snapshots older than 24h.
    Runs every hour to keep counterfactuals up-to-date.
    """
    try:
        processed = await calculate_counterfactuals_batch(limit=100)
        if processed > 0:
            logger.info(f"✅ Calculated counterfactuals for {processed} snapshots")
    except Exception as e:
        logger.error(f"Counterfactual calculation failed: {e}", exc_info=True)


async def auto_self_analysis_wrapper():
    """
    Wrapper for automatic self-analysis.

    Runs DeepSeek self-analysis every 50 new decisions with counterfactuals.
    Updates indicator weights based on actual performance.
    """
    try:
        from database.connection import SessionLocal
        from database.models import DecisionSnapshot, Account
        from sqlalchemy import select, and_

        async with SessionLocal() as db:
            # Get all active AI accounts
            stmt = select(Account).where(
                and_(Account.is_active == True, Account.account_type == "AI")
            )
            result = await db.execute(stmt)
            accounts = result.scalars().all()

            for account in accounts:
                # Count snapshots with counterfactuals
                count_stmt = select(DecisionSnapshot).where(
                    and_(
                        DecisionSnapshot.account_id == account.id,
                        DecisionSnapshot.regret.isnot(None),
                    )
                )
                count_result = await db.execute(count_stmt)
                total_snapshots = len(count_result.scalars().all())

                # Run analysis if we have enough data (50+ snapshots)
                if total_snapshots >= 50:
                    logger.info(
                        f"Running auto self-analysis for account {account.id} "
                        f"({total_snapshots} snapshots)"
                    )

                    analysis = await run_self_analysis(
                        account_id=account.id, limit=100, min_regret=None
                    )

                    # Log summary
                    if "error" not in analysis:
                        logger.info(
                            f"✅ Self-analysis complete for account {account.id}: "
                            f"Regret=${analysis.get('total_regret_usd', 0):.2f}, "
                            f"Accuracy={analysis.get('accuracy_rate', 0):.1%}"
                        )

                        # TODO: Auto-apply suggested weights if accuracy improvement >5%
                        # For now, just log the suggestions
                        suggested_weights = analysis.get("suggested_weights", {})
                        logger.info(
                            f"Suggested weights for account {account.id}: {suggested_weights}"
                        )

    except Exception as e:
        logger.error(f"Auto self-analysis failed: {e}", exc_info=True)
```

### Step 2.3: Aggiungi scheduling (alla fine di `initialize_services()`)

Aggiungi DOPO `task_scheduler.add_interval_task(capture_snapshots_wrapper, 300)`:

```python
    # Counterfactual learning jobs
    task_scheduler.add_interval_task(
        calculate_counterfactuals_wrapper, 3600
    )  # Every 1 hour
    task_scheduler.add_interval_task(
        auto_self_analysis_wrapper, 43200
    )  # Every 12 hours

    logger.info("✅ Counterfactual learning jobs scheduled")
```

---

## 🔧 MODIFICA 3: Documenta in `CLAUDE.md`

**File**: `backend/CLAUDE.md`

Aggiungi questa sezione dopo la sezione "Source of Truth":

```markdown
## 🧠 SISTEMA DI APPRENDIMENTO CONTROFATTUALE

**Status**: Implementato e attivo (2025-11-07)

Il sistema permette a DeepSeek di analizzare le proprie decisioni passate e migliorare nel tempo.

### Come Funziona:

1. **Ad ogni decisione AI** → `save_decision_snapshot()` salva:
   - Reasoning completo di DeepSeek
   - Tutti gli indicatori (Prophet, Pivot, RSI, Whale, Sentiment, News)
   - Decisione presa (LONG/SHORT/HOLD)
   - Prezzo entry

2. **Batch job ogni ora** → `calculate_counterfactuals_batch()` calcola:
   - Prezzo 24h dopo
   - P&L SE avessi fatto LONG
   - P&L SE avessi fatto SHORT
   - P&L SE avessi fatto HOLD
   - Decisione OTTIMALE (quella con max P&L)
   - REGRET (quanto perso per non aver scelto l'ottimale)

3. **Auto-analysis ogni 12h** → `run_self_analysis()` analizza pattern:
   - "Ignorato Prophet 12 volte quando RSI >70 → perso $145"
   - "HOLD quando Sentiment >80 + Whale sell → evitato -$230"
   - Calcola win rate per ogni indicatore
   - Suggerisce nuovi pesi basati su performance reale

### File Principali:

- `services/learning/decision_snapshot_service.py` - Salvataggio e counterfactuals
- `services/learning/deepseek_self_analysis_service.py` - Analisi e suggerimenti
- `api/learning_routes.py` - API endpoints (opzionali, sistema automatico)
- `database/models.py:DecisionSnapshot` - Modello dati

### API Endpoints (opzionali - sistema funziona automaticamente):

```bash
# Visualizza ultimi snapshots
GET /api/learning/snapshots/1?limit=20

# Trigger manuale counterfactuals
POST /api/learning/counterfactuals/calculate

# Trigger manuale analisi
POST /api/learning/analyze/1?limit=100
```

### Monitoring:

Check logs per vedere il sistema in azione:
```bash
grep "Decision snapshot saved" logs.json
grep "Calculated counterfactuals" logs.json
grep "Self-analysis complete" logs.json
```

### Note Importanti:

- Il sistema è completamente automatico - non serve interazione manuale
- Primi risultati dopo 24h (quando primi counterfactuals calcolati)
- Prima analisi dopo 50 decisioni con counterfactuals
- Suggerimenti pesi loggati ma NON applicati automaticamente (safety)
```

---

## ✅ CHECKLIST COMPLETA IMPLEMENTAZIONE:

- [ ] Modifica 1: Integra `save_decision_snapshot()` in `ai_decision_service.py:call_ai_for_decision()`
- [ ] Modifica 2: Aggiungi batch jobs in `startup.py:initialize_services()`
- [ ] Modifica 3: Documenta in `CLAUDE.md`
- [ ] Test: Lancia backend e verifica logs
- [ ] Test: Dopo 24h verifica counterfactuals calcolati
- [ ] Test: Dopo 50 decisioni verifica auto-analysis eseguita

---

## 🐛 TROUBLESHOOTING:

### Se decision snapshots non vengono salvati:
```bash
# Check logs
grep "Decision snapshot saved" logs.json
grep "Failed to save decision snapshot" logs.json
```

### Se counterfactuals non vengono calcolati:
```bash
# Check batch job running
grep "calculate_counterfactuals_wrapper" logs.json

# Check manual trigger
curl -X POST "http://localhost:8000/api/learning/counterfactuals/calculate?limit=10"
```

### Se self-analysis non esegue:
```bash
# Check if enough snapshots (need 50+)
sqlite3 backend/data.db "SELECT COUNT(*) FROM decision_snapshots WHERE regret IS NOT NULL;"

# Check logs
grep "auto_self_analysis_wrapper" logs.json
```

---

## 📊 ESEMPIO OUTPUT ATTESO:

Dopo 50+ decisioni, nei logs vedrai:

```
[INFO] Running auto self-analysis for account 1 (67 snapshots)
[INFO] ✅ Self-analysis complete for account 1: Regret=$145.50, Accuracy=58.0%
[INFO] Suggested weights for account 1: {'prophet': 0.65, 'pivot_points': 0.75, 'rsi_macd': 0.40, 'sentiment': 0.20}
```

🎉 **Sistema pronto per essere completato con queste 3 modifiche!**
