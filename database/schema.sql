-- =============================================================================
-- HLQUANTBOT DATABASE SCHEMA
-- =============================================================================
-- Tables: cooldowns, protections, llm_decisions (TimescaleDB hypertable)
-- =============================================================================

-- Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- -----------------------------------------------------------------------------
-- cooldowns: Cooldown records for loss-streak protection
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cooldowns (
    id                  SERIAL PRIMARY KEY,
    reason              VARCHAR(50) NOT NULL,           -- StoplossStreak, DailyDrawdown, LowPerformance
    triggered_at        TIMESTAMPTZ NOT NULL,
    cooldown_until      TIMESTAMPTZ NOT NULL,
    details             TEXT,                           -- JSON details
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cooldowns_until ON cooldowns(cooldown_until DESC);

COMMENT ON TABLE cooldowns IS 'Cooldown records - persisted across bot restarts';

-- -----------------------------------------------------------------------------
-- protections: Protection triggers for proactive risk management
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS protections (
    id                  SERIAL PRIMARY KEY,
    protection_name     VARCHAR(100) NOT NULL,          -- StoplossGuard, MaxDrawdown, etc.
    protected_until     TIMESTAMPTZ NOT NULL,
    trigger_details     TEXT,                           -- JSON details
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_protections_name ON protections(protection_name);
CREATE INDEX IF NOT EXISTS idx_protections_until ON protections(protected_until DESC);

COMMENT ON TABLE protections IS 'Protection triggers - persisted across bot restarts';

-- -----------------------------------------------------------------------------
-- llm_decisions: LLM veto decision tracker (TimescaleDB hypertable)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS llm_decisions (
    id                  BIGSERIAL,
    decided_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Decision context
    symbol              VARCHAR(20) NOT NULL,
    direction           VARCHAR(5) NOT NULL,
    regime              VARCHAR(10) NOT NULL,
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
    decision            VARCHAR(5) NOT NULL,
    confidence          NUMERIC(5,4),
    reason              TEXT,
    latency_ms          INTEGER,

    -- Price checkpoints
    price_5m            NUMERIC(20,8),
    price_15m           NUMERIC(20,8),
    price_30m           NUMERIC(20,8),
    price_1h            NUMERIC(20,8),
    price_2h            NUMERIC(20,8),
    price_4h            NUMERIC(20,8),

    -- MFE / MAE
    max_favorable_pct   NUMERIC(8,4) NOT NULL DEFAULT 0,
    max_adverse_pct     NUMERIC(8,4) NOT NULL DEFAULT 0,

    -- Outcome
    first_hit           VARCHAR(7),
    time_to_hit_min     INTEGER,
    was_correct         BOOLEAN,
    resolved_at         TIMESTAMPTZ,

    PRIMARY KEY (id, decided_at)
);

SELECT create_hypertable(
    'llm_decisions', 'decided_at',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_llm_decisions_pending
    ON llm_decisions (decided_at DESC) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_llm_decisions_symbol
    ON llm_decisions (symbol, decided_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_decisions_decision
    ON llm_decisions (decision, decided_at DESC);

COMMENT ON TABLE llm_decisions IS 'LLM veto decision tracker with price checkpoints and MFE/MAE';
