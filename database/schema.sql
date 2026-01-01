-- =============================================================================
-- HLQUANTBOT DATABASE SCHEMA
-- =============================================================================
-- Trading bot per Hyperliquid DEX
-- Database PostgreSQL con tabelle LIVE (sync real-time) e STORICO (append-only)
-- =============================================================================

-- Estensione per UUID (opzionale, utile per trade_id)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- =============================================================================
-- TABELLE LIVE (aggiornate ogni pochi secondi da Hyperliquid API)
-- Queste tabelle vengono SOVRASCRITTE ad ogni sync
-- =============================================================================

-- -----------------------------------------------------------------------------
-- live_account: Stato complessivo dell'account Hyperliquid
-- Singola riga, aggiornata ad ogni sync
-- -----------------------------------------------------------------------------
CREATE TABLE live_account (
    id                  INTEGER PRIMARY KEY DEFAULT 1,  -- Sempre 1 (singola riga)
    equity              NUMERIC(18, 8) NOT NULL,        -- Equity totale
    available_balance   NUMERIC(18, 8) NOT NULL,        -- Disponibile per trading
    margin_used         NUMERIC(18, 8) NOT NULL,        -- Margine impegnato
    unrealized_pnl      NUMERIC(18, 8) NOT NULL,        -- PnL non realizzato
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT single_account CHECK (id = 1)
);

-- Inserisce riga iniziale
INSERT INTO live_account (equity, available_balance, margin_used, unrealized_pnl)
VALUES (0, 0, 0, 0);

COMMENT ON TABLE live_account IS 'Stato account Hyperliquid - singola riga aggiornata real-time';

-- -----------------------------------------------------------------------------
-- realtime_account: Alias/extended version for frontend compatibility
-- Contains additional computed fields like position_count and current_leverage
-- -----------------------------------------------------------------------------
CREATE TABLE realtime_account (
    id                  INTEGER PRIMARY KEY DEFAULT 1,
    equity              NUMERIC(18, 8) NOT NULL,
    available_balance   NUMERIC(18, 8) NOT NULL,
    margin_used         NUMERIC(18, 8) NOT NULL,
    unrealized_pnl      NUMERIC(18, 8) NOT NULL,
    position_count      INTEGER NOT NULL DEFAULT 0,
    current_leverage    NUMERIC(8, 4) NOT NULL DEFAULT 0,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT single_realtime_account CHECK (id = 1)
);

-- Inserisce riga iniziale
INSERT INTO realtime_account (equity, available_balance, margin_used, unrealized_pnl, position_count, current_leverage)
VALUES (0, 0, 0, 0, 0, 0);

COMMENT ON TABLE realtime_account IS 'Stato account real-time per frontend - include position_count e current_leverage';

-- -----------------------------------------------------------------------------
-- live_positions: Posizioni aperte correnti
-- Cancellate e ri-populate ad ogni sync
-- -----------------------------------------------------------------------------
CREATE TABLE live_positions (
    symbol              VARCHAR(20) PRIMARY KEY,        -- Es: BTC, ETH, SOL
    side                VARCHAR(5) NOT NULL,            -- LONG o SHORT
    size                NUMERIC(18, 8) NOT NULL,        -- Dimensione posizione
    entry_price         NUMERIC(18, 8) NOT NULL,        -- Prezzo medio di ingresso
    mark_price          NUMERIC(18, 8) NOT NULL,        -- Prezzo corrente di mercato
    unrealized_pnl      NUMERIC(18, 8) NOT NULL,        -- PnL non realizzato
    leverage            INTEGER NOT NULL,               -- Leva utilizzata
    liquidation_price   NUMERIC(18, 8),                 -- Prezzo liquidazione (NULL se lontano)
    margin_used         NUMERIC(18, 8) NOT NULL,        -- Margine impegnato
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT valid_side CHECK (side IN ('LONG', 'SHORT'))
);

COMMENT ON TABLE live_positions IS 'Posizioni aperte correnti - sovrascritte ad ogni sync';

-- -----------------------------------------------------------------------------
-- live_orders: Ordini aperti (non ancora eseguiti)
-- Cancellati e ri-populate ad ogni sync
-- -----------------------------------------------------------------------------
CREATE TABLE live_orders (
    order_id            BIGINT PRIMARY KEY,             -- oid da Hyperliquid
    symbol              VARCHAR(20) NOT NULL,           -- Es: BTC, ETH, SOL
    side                VARCHAR(4) NOT NULL,            -- BUY o SELL
    size                NUMERIC(18, 8) NOT NULL,        -- Dimensione ordine
    price               NUMERIC(18, 8) NOT NULL,        -- Prezzo limite
    order_type          VARCHAR(20) NOT NULL,           -- LIMIT, STOP_LIMIT, etc.
    reduce_only         BOOLEAN NOT NULL DEFAULT FALSE, -- Se true, solo riduce posizione
    created_at          TIMESTAMPTZ NOT NULL,           -- Quando creato su Hyperliquid
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT valid_order_side CHECK (side IN ('BUY', 'SELL'))
);

CREATE INDEX idx_live_orders_symbol ON live_orders(symbol);

COMMENT ON TABLE live_orders IS 'Ordini aperti su Hyperliquid - sovrascritti ad ogni sync';


-- =============================================================================
-- TABELLE STORICO (append-only, mai modificate dopo inserimento)
-- Queste tabelle crescono nel tempo e servono per analisi
-- =============================================================================

-- -----------------------------------------------------------------------------
-- fills: Ogni fill (esecuzione parziale/totale) ricevuto da Hyperliquid
-- Append-only: ogni fill viene inserito una sola volta
-- -----------------------------------------------------------------------------
CREATE TABLE fills (
    fill_id             BIGINT PRIMARY KEY,             -- tid da Hyperliquid (unique)
    order_id            BIGINT NOT NULL,                -- oid dell'ordine
    symbol              VARCHAR(20) NOT NULL,
    side                VARCHAR(4) NOT NULL,            -- BUY o SELL
    size                NUMERIC(18, 8) NOT NULL,        -- Quantita eseguita
    price               NUMERIC(18, 8) NOT NULL,        -- Prezzo esecuzione
    fee                 NUMERIC(18, 8) NOT NULL,        -- Fee pagata (in USD)
    fee_token           VARCHAR(10) DEFAULT 'USDC',     -- Token fee
    fill_time           TIMESTAMPTZ NOT NULL,           -- Timestamp esecuzione
    closed_pnl          NUMERIC(18, 8),                 -- PnL realizzato (se chiude posizione)
    is_maker            BOOLEAN,                        -- True se maker, False se taker
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT valid_fill_side CHECK (side IN ('BUY', 'SELL'))
);

CREATE INDEX idx_fills_symbol ON fills(symbol);
CREATE INDEX idx_fills_order_id ON fills(order_id);
CREATE INDEX idx_fills_fill_time ON fills(fill_time DESC);
CREATE INDEX idx_fills_symbol_time ON fills(symbol, fill_time DESC);

COMMENT ON TABLE fills IS 'Storico fills da Hyperliquid - append-only, ogni fill inserito una sola volta';

-- -----------------------------------------------------------------------------
-- trades: Trade completi (entry + exit aggregati)
-- Un trade rappresenta apertura e chiusura di una posizione
-- -----------------------------------------------------------------------------
CREATE TABLE trades (
    trade_id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    symbol              VARCHAR(20) NOT NULL,
    side                VARCHAR(5) NOT NULL,            -- LONG o SHORT (direzione trade)
    size                NUMERIC(18, 8) NOT NULL,        -- Dimensione trade
    
    -- Entry
    entry_price         NUMERIC(18, 8) NOT NULL,        -- Prezzo medio ingresso
    entry_time          TIMESTAMPTZ NOT NULL,           -- Primo fill di ingresso
    entry_fill_ids      BIGINT[] NOT NULL,              -- Array di fill_id di ingresso
    
    -- Exit (NULL se trade ancora aperto)
    exit_price          NUMERIC(18, 8),                 -- Prezzo medio uscita
    exit_time           TIMESTAMPTZ,                    -- Ultimo fill di uscita
    exit_fill_ids       BIGINT[],                       -- Array di fill_id di uscita
    
    -- PnL (calcolato alla chiusura)
    gross_pnl           NUMERIC(18, 8),                 -- PnL lordo
    fees                NUMERIC(18, 8),                 -- Totale fee pagate
    net_pnl             NUMERIC(18, 8),                 -- PnL netto (gross - fees)
    
    -- Metadata
    strategy            VARCHAR(50),                    -- Nome strategia che ha generato il trade
    duration_seconds    INTEGER,                        -- Durata trade in secondi
    is_closed           BOOLEAN NOT NULL DEFAULT FALSE, -- True quando trade chiuso
    notes               TEXT,                           -- Note opzionali
    
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT valid_trade_side CHECK (side IN ('LONG', 'SHORT'))
);

CREATE INDEX idx_trades_symbol ON trades(symbol);
CREATE INDEX idx_trades_strategy ON trades(strategy);
CREATE INDEX idx_trades_entry_time ON trades(entry_time DESC);
CREATE INDEX idx_trades_is_closed ON trades(is_closed);
CREATE INDEX idx_trades_symbol_closed ON trades(symbol, is_closed);

COMMENT ON TABLE trades IS 'Trade completi entry+exit - aggiornata quando trade viene chiuso';

-- -----------------------------------------------------------------------------
-- signals: Segnali generati dalle strategie
-- Ogni segnale viene loggato, eseguito o meno
-- -----------------------------------------------------------------------------
CREATE TABLE signals (
    signal_id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol              VARCHAR(20) NOT NULL,
    strategy            VARCHAR(50) NOT NULL,           -- Nome strategia
    side                VARCHAR(4) NOT NULL,            -- BUY o SELL
    signal_type         VARCHAR(20) NOT NULL,           -- ENTRY, EXIT, SCALE_IN, etc.
    confidence          NUMERIC(5, 4),                  -- Score 0.0000 - 1.0000
    reason              TEXT,                           -- Motivazione del segnale
    
    -- Esecuzione
    executed            BOOLEAN NOT NULL DEFAULT FALSE,
    order_id            BIGINT,                         -- oid se eseguito
    execution_price     NUMERIC(18, 8),                 -- Prezzo esecuzione
    rejected_reason     TEXT,                           -- Motivo se non eseguito
    
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT valid_signal_side CHECK (side IN ('BUY', 'SELL')),
    CONSTRAINT valid_confidence CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
);

CREATE INDEX idx_signals_symbol ON signals(symbol);
CREATE INDEX idx_signals_strategy ON signals(strategy);
CREATE INDEX idx_signals_timestamp ON signals(timestamp DESC);
CREATE INDEX idx_signals_executed ON signals(executed);

COMMENT ON TABLE signals IS 'Storico segnali generati dalle strategie - append-only';

-- =============================================================================
-- TABELLE MULTI-AGENT (per tracking attività e decisioni degli agenti)
-- =============================================================================

-- -----------------------------------------------------------------------------
-- agent_activity: Log delle attività degli agenti
-- Traccia ogni azione significativa degli agenti (ordini, eventi, errori)
-- -----------------------------------------------------------------------------
CREATE TABLE agent_activity (
    id                  SERIAL PRIMARY KEY,
    agent_id            VARCHAR(50) NOT NULL,           -- orchestrator, execution, regime, etc.
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    activity_type       VARCHAR(50) NOT NULL,           -- order_placed, position_opened, cycle_completed, etc.
    status              VARCHAR(20) DEFAULT 'success',  -- success, error, warning
    message             TEXT,                           -- Descrizione leggibile
    details             JSONB,                          -- Dettagli aggiuntivi (ordine, posizione, etc.)
    symbol              VARCHAR(20),                    -- Simbolo coinvolto (opzionale)

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_agent_activity_agent_id ON agent_activity(agent_id);
CREATE INDEX idx_agent_activity_timestamp ON agent_activity(timestamp DESC);
CREATE INDEX idx_agent_activity_type ON agent_activity(activity_type);
CREATE INDEX idx_agent_activity_agent_time ON agent_activity(agent_id, timestamp DESC);

COMMENT ON TABLE agent_activity IS 'Log attività agenti - append-only per audit trail';

-- -----------------------------------------------------------------------------
-- agent_decisions: Decisioni prese dagli agenti AI
-- Traccia input, reasoning e output di ogni decisione AI
-- -----------------------------------------------------------------------------
CREATE TABLE agent_decisions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_id            VARCHAR(50) NOT NULL,           -- regime, strategy_selector, etc.
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decision_type       VARCHAR(50) NOT NULL,           -- regime_detection, strategy_selection, etc.
    symbol              VARCHAR(20),                    -- Simbolo analizzato (opzionale)

    -- AI Decision data
    input_data          JSONB,                          -- Dati di input (prezzi, indicatori, etc.)
    reasoning           TEXT,                           -- Spiegazione della decisione
    output_data         JSONB,                          -- Output (regime, strategia, parametri, etc.)

    -- Execution
    action_taken        VARCHAR(100),                   -- Azione risultante
    confidence          NUMERIC(5, 4),                  -- Confidenza 0.0000 - 1.0000

    -- Performance
    duration_ms         INTEGER,                        -- Tempo di elaborazione in ms

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT valid_decision_confidence CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
);

CREATE INDEX idx_agent_decisions_agent_id ON agent_decisions(agent_id);
CREATE INDEX idx_agent_decisions_timestamp ON agent_decisions(timestamp DESC);
CREATE INDEX idx_agent_decisions_symbol ON agent_decisions(symbol);
CREATE INDEX idx_agent_decisions_agent_time ON agent_decisions(agent_id, timestamp DESC);
CREATE INDEX idx_agent_decisions_agent_symbol ON agent_decisions(agent_id, symbol, timestamp DESC);

COMMENT ON TABLE agent_decisions IS 'Decisioni AI degli agenti - traccia reasoning e output per analisi';

-- -----------------------------------------------------------------------------
-- daily_summary: Riepilogo giornaliero delle performance
-- Una riga per giorno, calcolata a fine giornata
-- -----------------------------------------------------------------------------
CREATE TABLE daily_summary (
    date                DATE PRIMARY KEY,               -- Giorno (senza ora)
    starting_equity     NUMERIC(18, 8) NOT NULL,        -- Equity inizio giornata
    ending_equity       NUMERIC(18, 8) NOT NULL,        -- Equity fine giornata
    
    -- Conteggio trade
    trades_count        INTEGER NOT NULL DEFAULT 0,     -- Trade chiusi nel giorno
    win_count           INTEGER NOT NULL DEFAULT 0,     -- Trade in profitto
    loss_count          INTEGER NOT NULL DEFAULT 0,     -- Trade in perdita
    
    -- PnL
    gross_pnl           NUMERIC(18, 8) NOT NULL DEFAULT 0,
    fees                NUMERIC(18, 8) NOT NULL DEFAULT 0,
    net_pnl             NUMERIC(18, 8) NOT NULL DEFAULT 0,
    
    -- Risk metrics
    max_drawdown        NUMERIC(18, 8),                 -- Max drawdown intraday
    max_equity          NUMERIC(18, 8),                 -- Picco equity del giorno
    min_equity          NUMERIC(18, 8),                 -- Minimo equity del giorno
    
    -- Breakdown per simbolo (JSON per flessibilita)
    pnl_by_symbol       JSONB,                          -- {"BTC": 100.5, "ETH": -20.3}
    pnl_by_strategy     JSONB,                          -- {"momentum": 50, "breakout": 30}
    
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_daily_summary_date ON daily_summary(date DESC);

COMMENT ON TABLE daily_summary IS 'Riepilogo giornaliero performance - una riga per giorno';


-- =============================================================================
-- VISTE UTILI
-- =============================================================================

-- Vista per trade aperti
CREATE VIEW open_trades AS
SELECT * FROM trades WHERE is_closed = FALSE;

-- Vista per performance recenti
CREATE VIEW recent_performance AS
SELECT 
    date,
    net_pnl,
    trades_count,
    win_count,
    loss_count,
    CASE WHEN trades_count > 0 
         THEN ROUND(win_count::NUMERIC / trades_count * 100, 2) 
         ELSE 0 
    END as win_rate_pct,
    ending_equity
FROM daily_summary
ORDER BY date DESC
LIMIT 30;

-- Vista per ultimo stato account
CREATE VIEW account_status AS
SELECT 
    la.equity,
    la.available_balance,
    la.margin_used,
    la.unrealized_pnl,
    la.updated_at,
    COALESCE(SUM(lp.unrealized_pnl), 0) as positions_pnl,
    COUNT(lp.symbol) as open_positions_count
FROM live_account la
LEFT JOIN live_positions lp ON true
GROUP BY la.id, la.equity, la.available_balance, la.margin_used, la.unrealized_pnl, la.updated_at;


-- =============================================================================
-- FUNZIONI HELPER
-- =============================================================================

-- Funzione per aggiornare updated_at automaticamente
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Trigger per trades
CREATE TRIGGER update_trades_updated_at
    BEFORE UPDATE ON trades
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Trigger per daily_summary
CREATE TRIGGER update_daily_summary_updated_at
    BEFORE UPDATE ON daily_summary
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();
