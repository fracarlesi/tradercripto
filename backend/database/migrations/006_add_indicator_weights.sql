-- Migration: Add indicator weights tracking for auto-learning system
-- Date: 2025-11-12
-- Description: Stores indicator weights history and enables gradual auto-adjustment

-- Add indicator_weights JSON column to accounts table
ALTER TABLE accounts ADD COLUMN indicator_weights TEXT;

-- Create indicator_weights_history table for tracking changes over time
CREATE TABLE IF NOT EXISTS indicator_weights_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    old_weights TEXT,
    new_weights TEXT NOT NULL,
    source VARCHAR(50) NOT NULL,
    applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

-- Create indexes for efficient queries
CREATE INDEX IF NOT EXISTS idx_weights_history_account_applied ON indicator_weights_history(account_id, applied_at DESC);
CREATE INDEX IF NOT EXISTS idx_weights_history_source ON indicator_weights_history(source);

-- Set initial indicator_weights for all AI accounts (default: all 0.5)
UPDATE accounts
SET indicator_weights = '{"prophet": 0.5, "pivot": 0.5, "rsi_macd": 0.5, "sentiment": 0.5, "whale": 0.5}'
WHERE account_type = 'AI' AND indicator_weights IS NULL;
