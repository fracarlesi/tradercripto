-- =============================================================================
-- Migration 007: Add Protections Table
-- =============================================================================
-- Adds table for tracking protection triggers from the modular protection system.
-- 
-- Protections are PROACTIVE (prevent disaster) vs Cooldowns which are REACTIVE.
-- Each protection type can independently block trading when triggered.
--
-- Protection types:
-- - StoplossGuard: Too many stoplosses in short period
-- - MaxDrawdownProtection: Drawdown exceeds threshold
-- - CooldownPeriodProtection: Minimum time between trades
-- - LowPerformanceProtection: Win rate below threshold
-- =============================================================================

-- Protections table for tracking protection triggers
CREATE TABLE IF NOT EXISTS protections (
    id SERIAL PRIMARY KEY,
    protection_name VARCHAR(100) NOT NULL,     -- 'StoplossGuard', 'MaxDrawdownProtection', etc.
    protected_until TIMESTAMPTZ NOT NULL,      -- When protection expires
    trigger_details JSONB,                     -- Context about what triggered the protection
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for quickly finding active protections by name
CREATE INDEX IF NOT EXISTS idx_protections_name_until ON protections(protection_name, protected_until);

-- Index for recent protection history
CREATE INDEX IF NOT EXISTS idx_protections_created ON protections(created_at DESC);

-- Index for finding currently active protections (expires in future)
CREATE INDEX IF NOT EXISTS idx_protections_active ON protections(protected_until) WHERE protected_until > NOW();

-- Comment
COMMENT ON TABLE protections IS 'Modular protection system triggers - blocks trading in adverse conditions';
COMMENT ON COLUMN protections.protection_name IS 'Name of the protection class that triggered';
COMMENT ON COLUMN protections.protected_until IS 'UTC timestamp when this protection expires';
COMMENT ON COLUMN protections.trigger_details IS 'JSON with details about what triggered the protection';
