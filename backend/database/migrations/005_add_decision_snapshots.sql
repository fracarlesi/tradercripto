-- Migration 005: Add decision_snapshots table for counterfactual analysis
-- Created: 2025-11-07
-- Purpose: Enable learning from both executed trades AND missed opportunities

CREATE TABLE IF NOT EXISTS decision_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Decision context
    timestamp TIMESTAMP NOT NULL,
    account_id INTEGER NOT NULL,
    symbol VARCHAR(10) NOT NULL,  -- BTC, ETH, etc.

    -- Snapshot of all indicators at decision time
    indicators_snapshot TEXT NOT NULL,  -- JSON with all indicator values
    deepseek_reasoning TEXT NOT NULL,   -- Complete reasoning from DeepSeek

    -- Actual decision taken
    actual_decision VARCHAR(10) NOT NULL,  -- LONG, SHORT, HOLD
    actual_size_pct REAL,                  -- % of portfolio (0.0-1.0)

    -- Prices for counterfactual calculation
    entry_price REAL NOT NULL,
    exit_price_24h REAL,  -- Fetched 24h later by batch job

    -- P&L calculations (filled by batch job after 24h)
    actual_pnl REAL,                -- P&L from actual decision
    counterfactual_long_pnl REAL,   -- P&L if had gone LONG
    counterfactual_short_pnl REAL,  -- P&L if had gone SHORT
    counterfactual_hold_pnl REAL,   -- P&L if had HOLD (always 0)

    -- Analysis results
    optimal_decision VARCHAR(10),  -- Decision with highest P&L (retrospective)
    regret REAL,                   -- Difference between optimal and actual P&L

    -- Metadata
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    counterfactuals_calculated_at TIMESTAMP,  -- When batch job ran

    FOREIGN KEY (account_id) REFERENCES accounts(id)
);

-- Indexes for efficient queries
CREATE INDEX IF NOT EXISTS idx_decision_snapshots_timestamp ON decision_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_decision_snapshots_account ON decision_snapshots(account_id);
CREATE INDEX IF NOT EXISTS idx_decision_snapshots_symbol ON decision_snapshots(symbol);
CREATE INDEX IF NOT EXISTS idx_decision_snapshots_regret ON decision_snapshots(regret DESC);
CREATE INDEX IF NOT EXISTS idx_decision_snapshots_pending ON decision_snapshots(exit_price_24h)
    WHERE exit_price_24h IS NULL;  -- Find snapshots needing counterfactual calculation
