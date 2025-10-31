# Backup Pre-Refactoring

**Data**: 2025-10-31 14:16:51
**Motivo**: Backup prima di `/speckit.implement` - Refactoring 001-production-refactor

## Contenuto Backup

### File Critici Salvati

1. **`.env`** - Credentials funzionanti
   - HYPERLIQUID_PRIVATE_KEY
   - HYPERLIQUID_WALLET_ADDRESS
   - DEEPSEEK_API_KEY
   - MAX_CAPITAL_USD=52.0
   - Trading mode REAL

2. **`data.db`** (244 KB) - Database SQLite con storico
   - Ordini storici
   - AI decisions
   - Posizioni
   - Trades
   - Klines

3. **`news_feed.py`** (3.1 KB) - Logica news feed funzionante
   - Fetch da CoinJournal.net
   - HTML parsing
   - Error handling

4. **`settings.py`** (654 B) - Configurazioni trading
   - MarketConfig
   - Commission rates
   - Trading configs

5. **`backend_full/`** - Copia completa directory backend
   - Tutti i file Python
   - Tutta la struttura esistente

## Come Ripristinare

### Opzione 1: Ripristina Solo Credentials
```bash
cp .backup_pre_refactor/20251031_141651/.env backend/
```

### Opzione 2: Ripristina Database
```bash
cp .backup_pre_refactor/20251031_141651/data.db backend/
```

### Opzione 3: Ripristina Tutto
```bash
rm -rf backend/
cp -r .backup_pre_refactor/20251031_141651/backend_full/ backend/
```

## Note

- Backup creato PRIMA di refactoring async architecture
- Codice vecchio: synchronous SQLite-based
- Codice nuovo: async FastAPI + async SQLAlchemy + PostgreSQL
- Preservare .env per credentials funzionanti
- Migrare data.db → PostgreSQL con script in backend/scripts/maintenance/
