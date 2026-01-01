-- =============================================================================
-- MIGRATION: ADD PERFORMANCE METRICS
-- Add Sharpe Ratio and Profit Factor columns to hourly_metrics
-- for walk-forward validation and risk-adjusted optimization
-- =============================================================================

-- Add Sharpe Ratio column
-- Annualized Sharpe Ratio calculated from hourly trade returns
ALTER TABLE hourly_metrics 
ADD COLUMN IF NOT EXISTS sharpe_ratio NUMERIC(10, 4) DEFAULT 0;

COMMENT ON COLUMN hourly_metrics.sharpe_ratio IS 'Annualized Sharpe Ratio for the hour (risk-adjusted return metric)';

-- Add Profit Factor column  
-- Gross Profits / Gross Losses (>1.5 is considered good)
ALTER TABLE hourly_metrics
ADD COLUMN IF NOT EXISTS profit_factor NUMERIC(10, 4) DEFAULT 0;

COMMENT ON COLUMN hourly_metrics.profit_factor IS 'Profit Factor for the hour (gross profits / gross losses)';

-- Update the parameter_performance view to include average Sharpe and PF
DROP VIEW IF EXISTS parameter_performance;

CREATE OR REPLACE VIEW parameter_performance AS
SELECT
    pv.version_id,
    pv.created_at,
    pv.applied_at,
    pv.source,
    pv.tp_pct,
    pv.sl_pct,
    pv.position_size_usd,
    pv.leverage,
    pv.is_active,
    pv.reverted_at IS NOT NULL as was_reverted,
    COUNT(hm.hour_start) as hours_active,
    COALESCE(SUM(hm.trades_count), 0) as total_trades,
    COALESCE(SUM(hm.win_count), 0) as total_wins,
    COALESCE(SUM(hm.loss_count), 0) as total_losses,
    COALESCE(SUM(hm.net_pnl), 0) as total_pnl,
    COALESCE(AVG(hm.max_drawdown_pct), 0) as avg_drawdown,
    -- New metrics
    COALESCE(AVG(hm.sharpe_ratio), 0) as avg_sharpe_ratio,
    COALESCE(AVG(hm.profit_factor), 0) as avg_profit_factor,
    CASE WHEN COALESCE(SUM(hm.trades_count), 0) > 0
         THEN ROUND(COALESCE(SUM(hm.win_count), 0)::NUMERIC / SUM(hm.trades_count) * 100, 2)
         ELSE 0
    END as win_rate,
    CASE WHEN COUNT(hm.hour_start) > 0
         THEN ROUND(COALESCE(SUM(hm.net_pnl), 0) / COUNT(hm.hour_start), 4)
         ELSE 0
    END as hourly_pnl_avg
FROM parameter_versions pv
LEFT JOIN hourly_metrics hm ON hm.parameter_version = pv.version_id
GROUP BY pv.version_id
ORDER BY pv.created_at DESC;

COMMENT ON VIEW parameter_performance IS 'Performance aggregata per versione parametri (includes Sharpe Ratio and Profit Factor)';

-- Create index on new columns for faster aggregation queries
CREATE INDEX IF NOT EXISTS idx_hourly_metrics_sharpe ON hourly_metrics(sharpe_ratio) WHERE sharpe_ratio > 0;
CREATE INDEX IF NOT EXISTS idx_hourly_metrics_pf ON hourly_metrics(profit_factor) WHERE profit_factor > 0;
