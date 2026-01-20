-- =============================================================================
-- Migration 008: Add SMA Indicators
-- =============================================================================
-- Adds SMA20 and SMA50 columns to market_states table for the new
-- SMA crossover strategy.
-- =============================================================================

-- Add SMA20 column
ALTER TABLE market_states
ADD COLUMN IF NOT EXISTS sma20 DECIMAL(20, 8);

-- Add SMA50 column
ALTER TABLE market_states
ADD COLUMN IF NOT EXISTS sma50 DECIMAL(20, 8);

-- =============================================================================
-- Comments
-- =============================================================================

COMMENT ON COLUMN market_states.sma20 IS 'Simple Moving Average (20 periods)';
COMMENT ON COLUMN market_states.sma50 IS 'Simple Moving Average (50 periods)';
