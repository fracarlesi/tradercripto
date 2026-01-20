-- =============================================================================
-- Migration 006: Add Cooldowns Table
-- =============================================================================
-- Adds table for tracking trading cooldown periods triggered by:
-- - Consecutive stoploss streaks
-- - Daily drawdown limits
-- - Low performance periods
-- 
-- Cooldowns persist across bot restarts for safety.
-- =============================================================================

-- Cooldowns table for tracking trading pauses
CREATE TABLE IF NOT EXISTS cooldowns (
    id SERIAL PRIMARY KEY,
    reason VARCHAR(50) NOT NULL,          -- 'StoplossStreak', 'DailyDrawdown', 'LowPerformance'
    triggered_at TIMESTAMPTZ NOT NULL,    -- When cooldown was triggered
    cooldown_until TIMESTAMPTZ NOT NULL,  -- When cooldown expires
    details JSONB,                         -- Extra context (e.g., num_losses, dd_pct)
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for quickly finding active cooldowns
CREATE INDEX IF NOT EXISTS idx_cooldowns_triggered_at ON cooldowns(triggered_at DESC);
CREATE INDEX IF NOT EXISTS idx_cooldowns_reason ON cooldowns(reason);
CREATE INDEX IF NOT EXISTS idx_cooldowns_until ON cooldowns(cooldown_until);

-- Comment
COMMENT ON TABLE cooldowns IS 'Trading cooldown periods triggered by loss streaks, drawdown, or low performance';
