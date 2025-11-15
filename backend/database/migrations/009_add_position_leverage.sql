-- Migration: Add leverage field to positions table
-- Date: 2025-11-15
-- Description: Add leverage column to track position leverage from Hyperliquid

ALTER TABLE positions ADD COLUMN leverage NUMERIC(5, 2);

-- Create index for leverage lookups (useful for finding high-leverage positions)
CREATE INDEX IF NOT EXISTS idx_positions_leverage ON positions(leverage);
