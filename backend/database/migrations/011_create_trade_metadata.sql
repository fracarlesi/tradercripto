-- Migration: Create trade_metadata table for persistent leverage/strategy tracking
-- Date: 2025-11-16
-- Description: Store leverage and strategy at order execution time (before position closes)

CREATE TABLE IF NOT EXISTS trade_metadata (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    leverage NUMERIC(5, 2),
    strategy VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

-- Index for fast lookups during sync
CREATE INDEX IF NOT EXISTS idx_trade_metadata_account_symbol ON trade_metadata(account_id, symbol);
CREATE INDEX IF NOT EXISTS idx_trade_metadata_created_at ON trade_metadata(created_at DESC);

-- Note: This table stores metadata at order placement time
-- sync_fills() will read the MOST RECENT entry for each symbol to get leverage/strategy
