-- Migration: Add missed opportunities reports table
-- Date: 2025-11-13
-- Description: Stores hourly missed opportunities analysis reports

CREATE TABLE IF NOT EXISTS missed_opportunities_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analyzed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    lookback_hours INTEGER NOT NULL,
    min_move_pct NUMERIC(5, 2) NOT NULL,
    
    -- Summary stats
    total_movers INTEGER NOT NULL,
    analyzed_movers INTEGER NOT NULL,
    gainers_missed INTEGER NOT NULL,
    losers_missed INTEGER NOT NULL,
    
    -- Detailed analysis (JSON)
    missed_opportunities TEXT NOT NULL,  -- JSON array of opportunities
    patterns_identified TEXT,             -- JSON object with patterns
    recommendations TEXT,                 -- JSON array of recommendations
    
    -- Full report text
    report_text TEXT NOT NULL,
    
    -- Status
    status VARCHAR(20) NOT NULL DEFAULT 'completed',
    
    CONSTRAINT check_status CHECK (status IN ('completed', 'no_movers', 'error'))
);

-- Index for efficient queries
CREATE INDEX IF NOT EXISTS idx_reports_analyzed_at ON missed_opportunities_reports(analyzed_at DESC);
CREATE INDEX IF NOT EXISTS idx_reports_status ON missed_opportunities_reports(status);
