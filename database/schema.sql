-- =============================================================================
-- HLQUANTBOT DATABASE SCHEMA (Minimal)
-- =============================================================================
-- Only cooldowns and protections tables are needed.
-- All analytics/trade/dashboard tables have been removed.
-- =============================================================================

-- Estensione per UUID (opzionale)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

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
