-- =============================================================================
-- Migration 005: Conservative Refactor Tables
-- =============================================================================
-- Adds tables for the conservative trading refactor:
-- - market_states: OHLCV + indicators history
-- - equity_curve: Equity snapshots for risk monitoring
-- - kill_switch_log: Kill switch events
-- - llm_decisions: LLM veto decisions for accuracy tracking
-- - trade_setups: Trade setup history
-- =============================================================================

-- Market States (OHLCV + indicators per asset)
CREATE TABLE IF NOT EXISTS market_states (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    timeframe VARCHAR(10) NOT NULL DEFAULT '4h',

    -- OHLCV
    open DECIMAL(20, 8) NOT NULL,
    high DECIMAL(20, 8) NOT NULL,
    low DECIMAL(20, 8) NOT NULL,
    close DECIMAL(20, 8) NOT NULL,
    volume DECIMAL(20, 8) NOT NULL,

    -- Technical Indicators
    atr DECIMAL(20, 8),
    atr_pct DECIMAL(10, 4),
    adx DECIMAL(10, 4),
    rsi DECIMAL(10, 4),
    ema50 DECIMAL(20, 8),
    ema200 DECIMAL(20, 8),
    ema200_slope DECIMAL(10, 6),
    choppiness DECIMAL(10, 4),

    -- Bollinger Bands
    bb_lower DECIMAL(20, 8),
    bb_mid DECIMAL(20, 8),
    bb_upper DECIMAL(20, 8),

    -- Regime
    regime VARCHAR(10) NOT NULL,  -- 'trend', 'range', 'chaos'
    trend_direction VARCHAR(10),   -- 'long', 'short', 'flat'

    -- Metadata
    bars_count INT DEFAULT 200,

    CONSTRAINT market_states_unique UNIQUE (timestamp, symbol, timeframe)
);

CREATE INDEX IF NOT EXISTS idx_market_states_symbol_time
    ON market_states (symbol, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_market_states_regime
    ON market_states (regime, timestamp DESC);


-- Equity Curve (snapshots for risk monitoring)
CREATE TABLE IF NOT EXISTS equity_curve (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    equity DECIMAL(20, 2) NOT NULL,

    -- Drawdown tracking
    peak_equity DECIMAL(20, 2) NOT NULL,
    drawdown_pct DECIMAL(10, 4) NOT NULL DEFAULT 0,

    -- Daily/Weekly P&L
    daily_pnl DECIMAL(20, 2) DEFAULT 0,
    daily_pnl_pct DECIMAL(10, 4) DEFAULT 0,
    weekly_pnl DECIMAL(20, 2) DEFAULT 0,
    weekly_pnl_pct DECIMAL(10, 4) DEFAULT 0,

    -- Position info
    positions_count INT DEFAULT 0,
    total_exposure DECIMAL(20, 2) DEFAULT 0,

    -- Kill switch status
    kill_switch_status VARCHAR(20) DEFAULT 'ok'
);

CREATE INDEX IF NOT EXISTS idx_equity_curve_time
    ON equity_curve (timestamp DESC);


-- Kill Switch Log
CREATE TABLE IF NOT EXISTS kill_switch_log (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    trigger_type VARCHAR(20) NOT NULL,  -- 'daily', 'weekly', 'max_drawdown'
    trigger_value DECIMAL(10, 4) NOT NULL,
    threshold DECIMAL(10, 4) NOT NULL,
    action_taken VARCHAR(50) NOT NULL,
    equity_at_trigger DECIMAL(20, 2),
    message TEXT,
    resume_time TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_kill_switch_log_time
    ON kill_switch_log (timestamp DESC);


-- LLM Decisions (for accuracy tracking)
CREATE TABLE IF NOT EXISTS llm_decisions (
    id SERIAL PRIMARY KEY,
    setup_id VARCHAR(50) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,

    -- Decision
    decision VARCHAR(10) NOT NULL,  -- 'ALLOW', 'DENY'
    confidence DECIMAL(5, 4) NOT NULL,
    reason TEXT,

    -- Context
    symbol VARCHAR(20) NOT NULL,
    regime VARCHAR(10) NOT NULL,
    setup_type VARCHAR(30) NOT NULL,

    -- Outcome tracking (filled after trade closes)
    trade_pnl DECIMAL(20, 2),
    was_correct BOOLEAN,
    outcome_recorded_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_llm_decisions_time
    ON llm_decisions (timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_llm_decisions_setup
    ON llm_decisions (setup_id);


-- Trade Setups (history of all setups generated)
CREATE TABLE IF NOT EXISTS trade_setups (
    id SERIAL PRIMARY KEY,
    setup_id VARCHAR(50) NOT NULL UNIQUE,
    timestamp TIMESTAMPTZ NOT NULL,

    -- Setup details
    symbol VARCHAR(20) NOT NULL,
    setup_type VARCHAR(30) NOT NULL,
    direction VARCHAR(10) NOT NULL,
    regime VARCHAR(10) NOT NULL,

    -- Price levels
    entry_price DECIMAL(20, 8) NOT NULL,
    stop_price DECIMAL(20, 8) NOT NULL,
    stop_distance_pct DECIMAL(10, 4) NOT NULL,

    -- Indicators at setup time
    atr DECIMAL(20, 8),
    adx DECIMAL(10, 4),
    rsi DECIMAL(10, 4),

    -- Quality metrics
    setup_quality DECIMAL(5, 4),
    confidence DECIMAL(5, 4),

    -- LLM veto
    llm_approved BOOLEAN,
    llm_confidence DECIMAL(5, 4),
    llm_reason TEXT,

    -- Execution outcome
    was_executed BOOLEAN DEFAULT FALSE,
    trade_id VARCHAR(50),
    final_pnl DECIMAL(20, 2),
    final_r_multiple DECIMAL(10, 4)
);

CREATE INDEX IF NOT EXISTS idx_trade_setups_time
    ON trade_setups (timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_trade_setups_symbol
    ON trade_setups (symbol, timestamp DESC);


-- Trade Intents (sized trades ready for execution)
CREATE TABLE IF NOT EXISTS trade_intents (
    id SERIAL PRIMARY KEY,
    intent_id VARCHAR(50) NOT NULL UNIQUE,
    setup_id VARCHAR(50) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,

    -- Trade parameters
    symbol VARCHAR(20) NOT NULL,
    direction VARCHAR(10) NOT NULL,
    setup_type VARCHAR(30) NOT NULL,

    -- Execution parameters
    entry_price DECIMAL(20, 8) NOT NULL,
    position_size DECIMAL(20, 8) NOT NULL,
    notional_value DECIMAL(20, 2) NOT NULL,

    -- Stop parameters
    stop_price DECIMAL(20, 8) NOT NULL,
    trailing_atr_mult DECIMAL(10, 4) DEFAULT 2.5,

    -- Risk info
    risk_amount DECIMAL(20, 2) NOT NULL,
    risk_pct DECIMAL(10, 4) NOT NULL,

    -- Execution outcome
    status VARCHAR(20) DEFAULT 'pending',  -- 'pending', 'executed', 'cancelled', 'expired'
    executed_at TIMESTAMPTZ,
    executed_price DECIMAL(20, 8),
    slippage_pct DECIMAL(10, 4)
);

CREATE INDEX IF NOT EXISTS idx_trade_intents_time
    ON trade_intents (timestamp DESC);


-- Performance Metrics (daily aggregates)
CREATE TABLE IF NOT EXISTS performance_metrics (
    id SERIAL PRIMARY KEY,
    date DATE NOT NULL UNIQUE,

    -- Trade counts
    trades_total INT DEFAULT 0,
    trades_won INT DEFAULT 0,
    trades_lost INT DEFAULT 0,

    -- P&L
    gross_pnl DECIMAL(20, 2) DEFAULT 0,
    net_pnl DECIMAL(20, 2) DEFAULT 0,
    fees_paid DECIMAL(20, 2) DEFAULT 0,
    funding_paid DECIMAL(20, 2) DEFAULT 0,

    -- Metrics
    win_rate DECIMAL(5, 4),
    profit_factor DECIMAL(10, 4),
    avg_r_multiple DECIMAL(10, 4),
    max_drawdown_pct DECIMAL(10, 4),

    -- By strategy
    trend_follow_pnl DECIMAL(20, 2) DEFAULT 0,
    mean_reversion_pnl DECIMAL(20, 2) DEFAULT 0,

    -- By regime
    trend_regime_pnl DECIMAL(20, 2) DEFAULT 0,
    range_regime_pnl DECIMAL(20, 2) DEFAULT 0,

    -- Execution quality
    avg_slippage_pct DECIMAL(10, 4),
    orders_count INT DEFAULT 0,
    fills_count INT DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_performance_metrics_date
    ON performance_metrics (date DESC);


-- =============================================================================
-- Comments
-- =============================================================================

COMMENT ON TABLE market_states IS 'Historical market state with OHLCV and indicators';
COMMENT ON TABLE equity_curve IS 'Equity snapshots for drawdown and risk monitoring';
COMMENT ON TABLE kill_switch_log IS 'Kill switch trigger events';
COMMENT ON TABLE llm_decisions IS 'LLM veto decisions for accuracy tracking';
COMMENT ON TABLE trade_setups IS 'All trade setups generated by strategies';
COMMENT ON TABLE trade_intents IS 'Sized trade intents ready for execution';
COMMENT ON TABLE performance_metrics IS 'Daily performance aggregates';
