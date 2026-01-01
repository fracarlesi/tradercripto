-- =============================================================================
-- Migration 003: Add Multi-Agent Tables
-- =============================================================================
-- Adds tables required for the multi-agent architecture:
-- - realtime_account: Extended account state for frontend
-- - agent_activity: Activity log for all agents
-- - agent_decisions: AI decision tracking for regime/strategy agents
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- realtime_account: Extended account state for frontend compatibility
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS realtime_account (
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

-- Insert initial row if not exists
INSERT INTO realtime_account (id, equity, available_balance, margin_used, unrealized_pnl, position_count, current_leverage)
VALUES (1, 0, 0, 0, 0, 0, 0)
ON CONFLICT (id) DO NOTHING;

COMMENT ON TABLE realtime_account IS 'Stato account real-time per frontend - include position_count e current_leverage';

-- -----------------------------------------------------------------------------
-- agent_activity: Log delle attività degli agenti
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_activity (
    id                  SERIAL PRIMARY KEY,
    agent_id            VARCHAR(50) NOT NULL,
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    activity_type       VARCHAR(50) NOT NULL,
    status              VARCHAR(20) DEFAULT 'success',
    message             TEXT,
    details             JSONB,
    symbol              VARCHAR(20),

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_activity_agent_id ON agent_activity(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_activity_timestamp ON agent_activity(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_agent_activity_type ON agent_activity(activity_type);
CREATE INDEX IF NOT EXISTS idx_agent_activity_agent_time ON agent_activity(agent_id, timestamp DESC);

COMMENT ON TABLE agent_activity IS 'Log attività agenti - append-only per audit trail';

-- -----------------------------------------------------------------------------
-- agent_decisions: Decisioni prese dagli agenti AI
-- -----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS agent_decisions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_id            VARCHAR(50) NOT NULL,
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decision_type       VARCHAR(50) NOT NULL,
    symbol              VARCHAR(20),

    -- AI Decision data
    input_data          JSONB,
    reasoning           TEXT,
    output_data         JSONB,

    -- Execution
    action_taken        VARCHAR(100),
    confidence          NUMERIC(5, 4),

    -- Performance
    duration_ms         INTEGER,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT valid_decision_confidence CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
);

CREATE INDEX IF NOT EXISTS idx_agent_decisions_agent_id ON agent_decisions(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_decisions_timestamp ON agent_decisions(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_agent_decisions_symbol ON agent_decisions(symbol);
CREATE INDEX IF NOT EXISTS idx_agent_decisions_agent_time ON agent_decisions(agent_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_agent_decisions_agent_symbol ON agent_decisions(agent_id, symbol, timestamp DESC);

COMMENT ON TABLE agent_decisions IS 'Decisioni AI degli agenti - traccia reasoning e output per analisi';

COMMIT;
