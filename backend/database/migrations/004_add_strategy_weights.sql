-- Migration: Add strategy_weights and max_position_ratio to Account
-- Date: 2025-11-07
-- Purpose: RIZZO VIDEO INTEGRATION - Feature Weights system

-- Add strategy_weights JSON column (nullable, default NULL)
ALTER TABLE accounts ADD COLUMN strategy_weights JSON;

-- Add max_position_ratio column (default 5% as recommended in video)
ALTER TABLE accounts ADD COLUMN max_position_ratio DECIMAL(5, 4) DEFAULT 0.0500 NOT NULL;

-- Set default strategy weights for existing accounts (NULL = use default weights)
-- The system will use default weights when strategy_weights IS NULL:
-- {
--   "pivot_points": 0.8,
--   "sentiment": 0.3,
--   "whale_alerts": 0.4,
--   "rsi_macd": 0.5,
--   "news": 0.2
-- }

-- Note: SQLite doesn't support COMMENT ON COLUMN
-- Column descriptions:
--   strategy_weights: JSON dict of feature importance weights (0.0-1.0). NULL = use default weights.
--   max_position_ratio: Maximum position size as ratio of balance (default 0.05 = 5%)
