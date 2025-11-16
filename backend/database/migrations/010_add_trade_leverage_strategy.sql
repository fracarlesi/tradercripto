-- Migration: Add leverage and strategy fields to trades table
-- Date: 2025-11-16
-- Description: Add leverage and strategy columns to capture trading context for complete trade history

ALTER TABLE trades ADD COLUMN leverage NUMERIC(5, 2);
ALTER TABLE trades ADD COLUMN strategy VARCHAR(50);

-- Create indexes for common queries
CREATE INDEX IF NOT EXISTS idx_trades_leverage ON trades(leverage);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
