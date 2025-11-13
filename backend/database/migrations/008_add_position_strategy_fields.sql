-- Migration: Add strategy tracking fields to positions table
-- Date: 2025-11-13
-- Description: Add fields to track trading strategy type and exit rules for each position

ALTER TABLE positions ADD COLUMN strategy_type VARCHAR(50);
ALTER TABLE positions ADD COLUMN take_profit_pct NUMERIC(5, 4);
ALTER TABLE positions ADD COLUMN stop_loss_pct NUMERIC(5, 4);
ALTER TABLE positions ADD COLUMN max_hold_minutes INTEGER;

-- Create index for strategy type lookups
CREATE INDEX IF NOT EXISTS idx_positions_strategy_type ON positions(strategy_type);
