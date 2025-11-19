-- Migration: Add pending_strategy_suggestions table
-- Date: 2025-11-19
-- Description: Table to store strategy suggestions for manual review

CREATE TABLE IF NOT EXISTS pending_strategy_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- When and where it came from
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    source VARCHAR(50) NOT NULL,  -- 'hourly_retrospective', 'self_analysis', 'counterfactual'

    -- Suggestion details
    suggestion_type VARCHAR(50) NOT NULL,  -- 'threshold_adjustment', 'score_boost', 'weight_change'
    symbol VARCHAR(20),  -- If specific to a symbol (nullable)

    -- The actual suggestion (JSON)
    suggestion_data JSON NOT NULL,

    -- Why this suggestion was made
    reason TEXT NOT NULL,

    -- Evidence/context (JSON)
    evidence JSON,

    -- Status tracking
    status VARCHAR(20) NOT NULL DEFAULT 'pending',  -- 'pending', 'applied', 'dismissed', 'expired'
    reviewed_at TIMESTAMP,
    review_notes TEXT
);

-- Indexes for efficient queries
CREATE INDEX IF NOT EXISTS idx_suggestions_created_at ON pending_strategy_suggestions(created_at);
CREATE INDEX IF NOT EXISTS idx_suggestions_status ON pending_strategy_suggestions(status);
CREATE INDEX IF NOT EXISTS idx_suggestions_source ON pending_strategy_suggestions(source);
CREATE INDEX IF NOT EXISTS idx_suggestions_type ON pending_strategy_suggestions(suggestion_type);
