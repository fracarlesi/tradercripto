-- =============================================================================
-- MIGRATION: OPTIMIZATION TABLES
-- Tabelle per il sistema di auto-ottimizzazione con DeepSeek
-- =============================================================================

-- -----------------------------------------------------------------------------
-- parameter_versions: Storia di tutte le versioni dei parametri
-- Ogni modifica (LLM, manuale, rollback) crea una nuova versione
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS parameter_versions (
    version_id              SERIAL PRIMARY KEY,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_at              TIMESTAMPTZ,                    -- Quando applicata al bot
    reverted_at             TIMESTAMPTZ,                    -- Se rollback effettuato
    source                  VARCHAR(20) NOT NULL,           -- 'initial', 'llm', 'manual', 'rollback'
    llm_reasoning           TEXT,                           -- Ragionamento DeepSeek

    -- Parametri globali
    tp_pct                  NUMERIC(10, 6) NOT NULL,        -- Take profit %
    sl_pct                  NUMERIC(10, 6) NOT NULL,        -- Stop loss %
    position_size_usd       NUMERIC(10, 2) NOT NULL,        -- Dimensione posizione USD
    leverage                INTEGER NOT NULL,               -- Leva

    -- Parametri Momentum
    momentum_enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    momentum_ema_fast       INTEGER NOT NULL,
    momentum_ema_slow       INTEGER NOT NULL,
    momentum_rsi_period     INTEGER NOT NULL,
    momentum_rsi_long       INTEGER NOT NULL,               -- Threshold long
    momentum_rsi_short      INTEGER NOT NULL,               -- Threshold short

    -- Parametri Mean Reversion
    meanrev_enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    meanrev_rsi_oversold    INTEGER NOT NULL,
    meanrev_rsi_overbought  INTEGER NOT NULL,
    meanrev_bb_period       INTEGER NOT NULL,
    meanrev_bb_std          NUMERIC(4, 2) NOT NULL,

    -- Parametri Breakout
    breakout_enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    breakout_lookback       INTEGER NOT NULL,               -- Barre lookback
    breakout_min_pct        NUMERIC(10, 6) NOT NULL,        -- Min breakout %

    is_active               BOOLEAN NOT NULL DEFAULT FALSE,

    CONSTRAINT valid_source CHECK (source IN ('initial', 'llm', 'manual', 'rollback'))
);

CREATE INDEX idx_param_versions_active ON parameter_versions(is_active) WHERE is_active;
CREATE INDEX idx_param_versions_created ON parameter_versions(created_at DESC);
CREATE INDEX idx_param_versions_source ON parameter_versions(source);

COMMENT ON TABLE parameter_versions IS 'Storia versioni parametri - ogni modifica crea nuova versione';

-- -----------------------------------------------------------------------------
-- hourly_metrics: Snapshot performance orarie
-- Usate per costruire context LLM con tiered summarization
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hourly_metrics (
    hour_start              TIMESTAMPTZ PRIMARY KEY,        -- Inizio ora (es: 2024-01-15 14:00:00)
    parameter_version       INTEGER REFERENCES parameter_versions(version_id),

    -- Metriche trade
    trades_count            INTEGER NOT NULL DEFAULT 0,
    win_count               INTEGER NOT NULL DEFAULT 0,
    loss_count              INTEGER NOT NULL DEFAULT 0,

    -- P&L
    gross_pnl               NUMERIC(18, 8) NOT NULL DEFAULT 0,
    net_pnl                 NUMERIC(18, 8) NOT NULL DEFAULT 0,
    fees                    NUMERIC(18, 8) NOT NULL DEFAULT 0,

    -- Breakdown per strategia (JSONB per flessibilita)
    pnl_by_strategy         JSONB,                          -- {"momentum": 10.5, "mean_reversion": -5.2}
    trades_by_strategy      JSONB,                          -- {"momentum": 3, "mean_reversion": 2}

    -- Risk metrics
    max_drawdown_pct        NUMERIC(10, 4),                 -- Max drawdown nell'ora
    avg_trade_duration      INTEGER,                        -- Durata media trade (secondi)
    largest_win             NUMERIC(18, 8),
    largest_loss            NUMERIC(18, 8),

    -- Contesto mercato
    btc_price_start         NUMERIC(18, 2),                 -- Prezzo BTC inizio ora
    btc_price_end           NUMERIC(18, 2),                 -- Prezzo BTC fine ora
    volatility_index        NUMERIC(10, 6),                 -- Indice volatilita calcolato

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_hourly_metrics_time ON hourly_metrics(hour_start DESC);
CREATE INDEX idx_hourly_metrics_version ON hourly_metrics(parameter_version);

COMMENT ON TABLE hourly_metrics IS 'Metriche orarie per analisi LLM - una riga per ora';

-- -----------------------------------------------------------------------------
-- optimization_runs: Log delle esecuzioni ottimizzazione
-- Traccia ogni chiamata a DeepSeek e il risultato
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS optimization_runs (
    run_id                  SERIAL PRIMARY KEY,
    started_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at            TIMESTAMPTZ,

    -- Context inviato a LLM
    context_hours           INTEGER NOT NULL,               -- Ore di dettaglio inviate
    context_days            INTEGER NOT NULL,               -- Giorni di aggregati inviati
    prompt_tokens           INTEGER,                        -- Token usati nel prompt
    completion_tokens       INTEGER,                        -- Token nella risposta

    -- Risposta LLM
    raw_response            TEXT,                           -- Risposta completa
    parsed_params           JSONB,                          -- Parametri estratti (JSON)
    reasoning_summary       TEXT,                           -- Ragionamento LLM
    confidence_score        NUMERIC(3, 2),                  -- Score 0.00 - 1.00

    -- Risultato
    applied_version         INTEGER REFERENCES parameter_versions(version_id),
    status                  VARCHAR(20) NOT NULL DEFAULT 'pending',
    error_message           TEXT,

    CONSTRAINT valid_status CHECK (status IN ('pending', 'success', 'failed', 'skipped', 'rolled_back'))
);

CREATE INDEX idx_optim_runs_started ON optimization_runs(started_at DESC);
CREATE INDEX idx_optim_runs_status ON optimization_runs(status);
CREATE INDEX idx_optim_runs_version ON optimization_runs(applied_version);

COMMENT ON TABLE optimization_runs IS 'Log esecuzioni ottimizzazione DeepSeek';

-- -----------------------------------------------------------------------------
-- VISTA: parameter_performance
-- Correla versioni parametri con performance
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW parameter_performance AS
SELECT
    pv.version_id,
    pv.created_at,
    pv.applied_at,
    pv.source,
    pv.tp_pct,
    pv.sl_pct,
    pv.position_size_usd,
    pv.leverage,
    pv.is_active,
    pv.reverted_at IS NOT NULL as was_reverted,
    COUNT(hm.hour_start) as hours_active,
    COALESCE(SUM(hm.trades_count), 0) as total_trades,
    COALESCE(SUM(hm.win_count), 0) as total_wins,
    COALESCE(SUM(hm.loss_count), 0) as total_losses,
    COALESCE(SUM(hm.net_pnl), 0) as total_pnl,
    COALESCE(AVG(hm.max_drawdown_pct), 0) as avg_drawdown,
    CASE WHEN COALESCE(SUM(hm.trades_count), 0) > 0
         THEN ROUND(COALESCE(SUM(hm.win_count), 0)::NUMERIC / SUM(hm.trades_count) * 100, 2)
         ELSE 0
    END as win_rate,
    CASE WHEN COUNT(hm.hour_start) > 0
         THEN ROUND(COALESCE(SUM(hm.net_pnl), 0) / COUNT(hm.hour_start), 4)
         ELSE 0
    END as hourly_pnl_avg
FROM parameter_versions pv
LEFT JOIN hourly_metrics hm ON hm.parameter_version = pv.version_id
GROUP BY pv.version_id
ORDER BY pv.created_at DESC;

COMMENT ON VIEW parameter_performance IS 'Performance aggregata per versione parametri';

-- -----------------------------------------------------------------------------
-- FUNZIONE: get_active_parameters()
-- Ritorna parametri attivi correnti in formato JSON
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION get_active_parameters()
RETURNS JSONB AS $$
DECLARE
    result JSONB;
BEGIN
    SELECT jsonb_build_object(
        'version_id', version_id,
        'global', jsonb_build_object(
            'tp_pct', tp_pct,
            'sl_pct', sl_pct,
            'position_size_usd', position_size_usd,
            'leverage', leverage
        ),
        'momentum', jsonb_build_object(
            'enabled', momentum_enabled,
            'ema_fast', momentum_ema_fast,
            'ema_slow', momentum_ema_slow,
            'rsi_period', momentum_rsi_period,
            'rsi_long_threshold', momentum_rsi_long,
            'rsi_short_threshold', momentum_rsi_short
        ),
        'mean_reversion', jsonb_build_object(
            'enabled', meanrev_enabled,
            'rsi_oversold', meanrev_rsi_oversold,
            'rsi_overbought', meanrev_rsi_overbought,
            'bb_period', meanrev_bb_period,
            'bb_std', meanrev_bb_std
        ),
        'breakout', jsonb_build_object(
            'enabled', breakout_enabled,
            'lookback_bars', breakout_lookback,
            'min_breakout_pct', breakout_min_pct
        ),
        'applied_at', applied_at,
        'source', source
    ) INTO result
    FROM parameter_versions
    WHERE is_active = TRUE
    LIMIT 1;

    RETURN COALESCE(result, '{}'::JSONB);
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION get_active_parameters() IS 'Ritorna parametri attivi in formato JSON';

-- -----------------------------------------------------------------------------
-- FUNZIONE: activate_parameter_version(version_id)
-- Attiva una versione specifica, disattivando le altre
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION activate_parameter_version(p_version_id INTEGER)
RETURNS BOOLEAN AS $$
BEGIN
    -- Disattiva tutte le versioni
    UPDATE parameter_versions SET is_active = FALSE WHERE is_active = TRUE;

    -- Attiva la versione richiesta
    UPDATE parameter_versions
    SET is_active = TRUE, applied_at = NOW()
    WHERE version_id = p_version_id;

    RETURN FOUND;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION activate_parameter_version(INTEGER) IS 'Attiva una versione parametri specifica';
