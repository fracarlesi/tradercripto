-- =============================================================================
-- Migration 009: Add LLM Decisions tracking table (TimescaleDB hypertable)
-- =============================================================================
-- Tracks every LLM ALLOW/DENY decision with price checkpoints,
-- MFE/MAE analysis, and outcome resolution (TP/SL/neither).
-- =============================================================================

-- Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- LLM decisions table
CREATE TABLE IF NOT EXISTS llm_decisions (
    id                  BIGSERIAL,
    decided_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Decision context
    symbol              VARCHAR(20) NOT NULL,
    direction           VARCHAR(5) NOT NULL,        -- long/short
    regime              VARCHAR(10) NOT NULL,        -- trend/range/chaos
    entry_price         NUMERIC(20,8) NOT NULL,
    stop_price          NUMERIC(20,8) NOT NULL,
    tp_price            NUMERIC(20,8) NOT NULL,

    -- Indicators snapshot
    adx                 NUMERIC(8,2),
    rsi                 NUMERIC(8,2),
    atr                 NUMERIC(20,8),
    ema9                NUMERIC(20,8),
    ema21               NUMERIC(20,8),
    volume_ratio        NUMERIC(8,2),

    -- LLM decision
    decision            VARCHAR(5) NOT NULL,         -- ALLOW/DENY
    confidence          NUMERIC(5,4),
    reason              TEXT,
    latency_ms          INTEGER,

    -- Price checkpoints (filled over time)
    price_5m            NUMERIC(20,8),
    price_15m           NUMERIC(20,8),
    price_30m           NUMERIC(20,8),
    price_1h            NUMERIC(20,8),
    price_2h            NUMERIC(20,8),
    price_4h            NUMERIC(20,8),

    -- MFE / MAE (running maximums)
    max_favorable_pct   NUMERIC(8,4) NOT NULL DEFAULT 0,
    max_adverse_pct     NUMERIC(8,4) NOT NULL DEFAULT 0,

    -- Outcome
    first_hit           VARCHAR(7),                  -- tp/sl/neither
    time_to_hit_min     INTEGER,
    was_correct         BOOLEAN,
    resolved_at         TIMESTAMPTZ,

    PRIMARY KEY (id, decided_at)
);

-- Convert to TimescaleDB hypertable (weekly chunks)
SELECT create_hypertable(
    'llm_decisions',
    'decided_at',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_llm_decisions_pending
    ON llm_decisions (decided_at DESC)
    WHERE resolved_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_llm_decisions_symbol
    ON llm_decisions (symbol, decided_at DESC);

CREATE INDEX IF NOT EXISTS idx_llm_decisions_decision
    ON llm_decisions (decision, decided_at DESC);

COMMENT ON TABLE llm_decisions IS 'LLM veto decision tracker with price checkpoints and MFE/MAE';
