-- =============================================================================
-- Migration 004: Intelligent Bot Tables
-- =============================================================================
-- Adds tables for HLQuantBot v2.0 intelligent features:
-- - market_snapshots: Full market state snapshots (200+ coins)
-- - opportunity_rankings: Top 20 scored coins for trading
-- - strategy_decisions: LLM-driven strategy selection with outcomes
-- - correlation_matrix: Daily symbol correlation tracking
-- - service_health: Service heartbeat and health monitoring
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- market_snapshots: Snapshots completi del mercato
-- Contiene metriche per tutti i 200+ coins disponibili su Hyperliquid
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market_snapshots (
    snapshot_id         SERIAL PRIMARY KEY,
    timestamp           TIMESTAMPTZ NOT NULL,
    data                JSONB NOT NULL,                  -- {symbol: {price, volume_24h, change_24h, ...}}
    coins_count         INTEGER,                         -- Numero di coins nel snapshot
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_market_snapshots_timestamp 
    ON market_snapshots(timestamp DESC);

COMMENT ON TABLE market_snapshots IS 'Snapshots completi del mercato - tutti i coins con metriche';
COMMENT ON COLUMN market_snapshots.data IS 'JSONB con dati per ogni coin: {symbol: {price, volume_24h, change_24h, funding_rate, open_interest, ...}}';

-- -----------------------------------------------------------------------------
-- opportunity_rankings: Ranking opportunita di trading
-- Top 20 coins scored per potenziale trading
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS opportunity_rankings (
    ranking_id          SERIAL PRIMARY KEY,
    timestamp           TIMESTAMPTZ NOT NULL,
    rankings            JSONB NOT NULL,                  -- [{symbol, score, factors, ...}]
    market_regime       VARCHAR(50),                     -- bullish, bearish, neutral, volatile
    btc_price           NUMERIC(18, 2),                  -- Prezzo BTC al momento del ranking
    total_volume_24h    NUMERIC(18, 2),                  -- Volume totale mercato 24h
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_opportunity_rankings_timestamp 
    ON opportunity_rankings(timestamp DESC);

COMMENT ON TABLE opportunity_rankings IS 'Ranking top 20 opportunita di trading - aggiornato periodicamente';
COMMENT ON COLUMN opportunity_rankings.rankings IS 'Array JSONB: [{symbol, score, momentum_score, volatility_score, volume_score, ...}]';

-- -----------------------------------------------------------------------------
-- strategy_decisions: Decisioni strategiche LLM-driven
-- Traccia quale strategia e stata selezionata per ogni trade e perche
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS strategy_decisions (
    decision_id         SERIAL PRIMARY KEY,
    timestamp           TIMESTAMPTZ NOT NULL,
    symbol              VARCHAR(20) NOT NULL,
    selected_strategy   VARCHAR(50) NOT NULL,            -- momentum, mean_reversion, breakout, etc.
    confidence          NUMERIC(5, 4),                   -- 0.0000 - 1.0000
    llm_reasoning       TEXT,                            -- Spiegazione LLM della scelta
    input_context       JSONB,                           -- Contesto fornito al LLM
    trade_id            UUID,                            -- FK a trades (nullable se non eseguito)
    outcome             VARCHAR(20) DEFAULT 'pending',   -- pending, win, loss, cancelled
    pnl                 NUMERIC(18, 8),                  -- PnL risultante
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT valid_outcome CHECK (outcome IN ('pending', 'win', 'loss', 'cancelled')),
    CONSTRAINT valid_strategy_confidence CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
);

CREATE INDEX IF NOT EXISTS idx_strategy_decisions_symbol 
    ON strategy_decisions(symbol);
CREATE INDEX IF NOT EXISTS idx_strategy_decisions_timestamp 
    ON strategy_decisions(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_decisions_symbol_timestamp 
    ON strategy_decisions(symbol, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_decisions_strategy 
    ON strategy_decisions(selected_strategy);
CREATE INDEX IF NOT EXISTS idx_strategy_decisions_outcome 
    ON strategy_decisions(outcome);

COMMENT ON TABLE strategy_decisions IS 'Decisioni strategiche LLM-driven - traccia reasoning e outcome';
COMMENT ON COLUMN strategy_decisions.llm_reasoning IS 'Spiegazione testuale del LLM per la scelta della strategia';
COMMENT ON COLUMN strategy_decisions.input_context IS 'JSONB con tutto il contesto fornito al LLM: {market_data, indicators, regime, ...}';

-- -----------------------------------------------------------------------------
-- correlation_matrix: Matrice correlazioni giornaliera
-- Correlazioni tra simboli per diversificazione e pair trading
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS correlation_matrix (
    date                DATE PRIMARY KEY,
    matrix              JSONB NOT NULL,                  -- {symbol1: {symbol2: correlation, ...}, ...}
    symbols_count       INTEGER,                         -- Numero di simboli nella matrice
    avg_correlation     NUMERIC(5, 4),                   -- Correlazione media del mercato
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE correlation_matrix IS 'Matrice correlazioni giornaliera tra simboli';
COMMENT ON COLUMN correlation_matrix.matrix IS 'JSONB nested: {BTC: {ETH: 0.85, SOL: 0.72, ...}, ETH: {...}, ...}';

-- -----------------------------------------------------------------------------
-- service_health: Health monitoring dei servizi
-- Stato e heartbeat di ogni servizio/agente
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS service_health (
    service_name        VARCHAR(50) PRIMARY KEY,
    status              VARCHAR(20) NOT NULL,            -- healthy, degraded, unhealthy, starting
    last_heartbeat      TIMESTAMPTZ,
    messages_processed  INTEGER NOT NULL DEFAULT 0,
    errors_count        INTEGER NOT NULL DEFAULT 0,
    metadata            JSONB,                           -- Dati aggiuntivi specifici del servizio
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT valid_service_status CHECK (status IN ('healthy', 'degraded', 'unhealthy', 'starting'))
);

COMMENT ON TABLE service_health IS 'Health monitoring dei servizi - stato e metriche';
COMMENT ON COLUMN service_health.metadata IS 'JSONB con metriche specifiche: {avg_latency_ms, queue_depth, memory_mb, ...}';

-- -----------------------------------------------------------------------------
-- Trigger per aggiornare updated_at su service_health
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION update_service_health_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS update_service_health_updated_at ON service_health;
CREATE TRIGGER update_service_health_updated_at
    BEFORE UPDATE ON service_health
    FOR EACH ROW
    EXECUTE FUNCTION update_service_health_updated_at();

COMMIT;
