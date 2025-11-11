# Self-Learning AI Trading System - Design Document

## Executive Summary

This document describes the design and implementation of a **self-learning AI trading system** that enables DeepSeek to learn from past trading decisions and continuously improve profitability by optimizing its decision-making prompts.

**Status**: Design Phase
**Goal**: Create a system where AI learns from mistakes, identifies patterns in successful/unsuccessful trades, and evolves its trading prompt to become more profitable over time.

---

## 1. System Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    SELF-LEARNING LOOP                        │
│                                                              │
│  ┌──────────────┐    ┌─────────────┐    ┌────────────────┐ │
│  │   Trading    │───▶│  Enhanced   │───▶│   Performance  │ │
│  │   Decision   │    │  Logging    │    │    Analysis    │ │
│  │   (DeepSeek) │    │  (Context)  │    │   (Metrics)    │ │
│  └──────────────┘    └─────────────┘    └────────────────┘ │
│         ▲                                        │           │
│         │                                        │           │
│         │              ┌────────────────┐        │           │
│         └──────────────│  Prompt        │◀───────┘           │
│                        │  Evolution     │                    │
│                        │  (Learning)    │                    │
│                        └────────────────┘                    │
└─────────────────────────────────────────────────────────────┘
```

### Core Components:

1. **Enhanced Decision Logging** - Capture rich context for each AI decision
2. **Performance Analysis Engine** - Analyze outcomes and identify patterns
3. **Prompt Evolution Mechanism** - Generate improved prompts based on learnings
4. **Metrics Tracking & Alerts** - Monitor performance degradation in real-time

---

## 2. Enhanced Database Schema

### 2.1 New Table: `prompt_versions`

Tracks different AI prompt versions and their performance.

```sql
CREATE TABLE prompt_versions (
    id INTEGER PRIMARY KEY,
    version_tag VARCHAR(50) UNIQUE NOT NULL,     -- e.g., "v1.0", "v1.1-profit-opt"
    prompt_text TEXT NOT NULL,                    -- Full prompt content
    prompt_hash VARCHAR(64) NOT NULL,             -- SHA256 for deduplication
    is_active BOOLEAN DEFAULT FALSE,              -- Currently in use
    created_at TIMESTAMP DEFAULT NOW(),
    created_by VARCHAR(50) DEFAULT 'system',      -- 'system', 'manual', 'evolved'
    parent_version_id INTEGER,                    -- Previous version (for evolution chain)
    description TEXT,                             -- What changed vs parent

    -- Performance metrics (updated periodically)
    total_decisions INTEGER DEFAULT 0,
    total_trades INTEGER DEFAULT 0,
    win_rate DECIMAL(5,4),                        -- % of profitable trades
    avg_pnl_pct DECIMAL(10,4),                    -- Average P&L %
    profit_factor DECIMAL(10,4),                  -- Gross profit / gross loss
    max_drawdown_pct DECIMAL(10,4),
    sharpe_ratio DECIMAL(10,4),
    last_performance_update TIMESTAMP,

    FOREIGN KEY (parent_version_id) REFERENCES prompt_versions(id)
);

CREATE INDEX idx_prompt_versions_active ON prompt_versions(is_active);
CREATE INDEX idx_prompt_versions_created ON prompt_versions(created_at);
```

### 2.2 Enhanced Table: `ai_decision_logs`

Add fields to existing table to track context and outcomes.

```sql
ALTER TABLE ai_decision_logs ADD COLUMN:

    -- Prompt tracking
    prompt_version_id INTEGER,                    -- Which prompt generated this

    -- Market context at decision time
    decision_context JSONB,                       -- Market conditions snapshot
    /*
    {
        "prices": {"BTC": 45000, "ETH": 3000},
        "technical_scores": {"BTC": 0.85},
        "news_sentiment": {"BTC": "positive"},
        "volatility": {"BTC": 0.025},
        "portfolio_diversity": 2,                 -- number of positions
        "cash_ratio": 0.40                        -- cash / total assets
    }
    */

    -- Outcome tracking (populated after trade closes)
    outcome_tracked BOOLEAN DEFAULT FALSE,
    outcome_pnl DECIMAL(20,8),                    -- Realized P&L ($)
    outcome_pnl_pct DECIMAL(10,4),                -- P&L %
    outcome_hold_duration_minutes INTEGER,
    outcome_exit_reason VARCHAR(50),              -- 'take_profit', 'stop_loss', 'rebalance', 'ai_decision'
    outcome_exit_time TIMESTAMP,
    outcome_max_profit_pct DECIMAL(10,4),         -- Peak profit % during hold
    outcome_max_drawdown_pct DECIMAL(10,4),       -- Peak drawdown % during hold
    outcome_evaluation TEXT,                      -- Post-trade analysis

    FOREIGN KEY (prompt_version_id) REFERENCES prompt_versions(id)
);

CREATE INDEX idx_ai_logs_prompt_version ON ai_decision_logs(prompt_version_id);
CREATE INDEX idx_ai_logs_outcome_tracked ON ai_decision_logs(outcome_tracked);
CREATE INDEX idx_ai_logs_symbol ON ai_decision_logs(symbol);
```

### 2.3 New Table: `performance_alerts`

Track when performance metrics trigger alerts for prompt evolution.

```sql
CREATE TABLE performance_alerts (
    id INTEGER PRIMARY KEY,
    alert_time TIMESTAMP DEFAULT NOW(),
    prompt_version_id INTEGER NOT NULL,
    alert_type VARCHAR(50) NOT NULL,              -- 'degradation', 'consecutive_losses', 'drawdown'
    metric_name VARCHAR(50) NOT NULL,             -- 'win_rate', 'profit_factor', etc.
    current_value DECIMAL(20,8),
    threshold_value DECIMAL(20,8),
    severity VARCHAR(20) NOT NULL,                -- 'warning', 'critical'
    actions_taken TEXT,                           -- What the system did

    FOREIGN KEY (prompt_version_id) REFERENCES prompt_versions(id)
);

CREATE INDEX idx_alerts_time ON performance_alerts(alert_time);
CREATE INDEX idx_alerts_prompt_version ON performance_alerts(prompt_version_id);
```

### 2.4 New Table: `learning_insights`

Store patterns and insights learned from trade analysis.

```sql
CREATE TABLE learning_insights (
    id INTEGER PRIMARY KEY,
    discovered_at TIMESTAMP DEFAULT NOW(),
    insight_type VARCHAR(50) NOT NULL,            -- 'pattern', 'correlation', 'failure_mode'
    title VARCHAR(200) NOT NULL,
    description TEXT NOT NULL,
    supporting_data JSONB,                        -- Evidence/examples
    /*
    {
        "sample_decisions": [123, 456, 789],
        "correlation": 0.85,
        "sample_size": 50,
        "confidence": "high"
    }
    */
    confidence_score DECIMAL(3,2),                -- 0-1 confidence
    actionable BOOLEAN DEFAULT TRUE,
    applied_in_version VARCHAR(50),               -- Which prompt version applied this
    impact_measured BOOLEAN DEFAULT FALSE,

    INDEX idx_insights_type (insight_type),
    INDEX idx_insights_actionable (actionable)
);
```

---

## 3. Performance Analysis Engine

### 3.1 Key Metrics Tracked

#### Decision-Level Metrics:
- **Win Rate**: % of trades that were profitable
- **Average P&L %**: Mean profit/loss percentage per trade
- **Profit Factor**: Sum(wins) / Sum(losses)
- **Hold Duration**: How long positions were held
- **Exit Reasons Distribution**: Why positions were closed

#### Portfolio-Level Metrics:
- **Sharpe Ratio**: Risk-adjusted returns
- **Max Drawdown**: Largest peak-to-trough decline
- **Consecutive Losses**: Longest losing streak
- **Diversification Score**: Average number of positions held

#### Context-Specific Metrics:
- **Performance by Symbol**: Which coins trade better
- **Performance by Market Regime**: Trending vs ranging markets
- **Performance by Technical Score**: Correlation with entry scores
- **Performance by Position Size**: Optimal sizing analysis

### 3.2 Pattern Recognition

The system analyzes historical decisions to identify:

1. **Winning Patterns**:
   - Conditions present in most profitable trades
   - Technical score thresholds for best entries
   - Optimal hold duration ranges
   - Market conditions favoring specific strategies

2. **Losing Patterns**:
   - Common characteristics of unprofitable trades
   - False signals (high score but loss)
   - Premature exits (sold winners too early)
   - Late exits (held losers too long)

3. **Correlation Analysis**:
   - Technical score vs actual outcomes
   - News sentiment vs profitability
   - Position sizing vs returns
   - Diversification level vs risk-adjusted returns

### 3.3 Degradation Detection

Automatic alerts triggered when:

```python
DEGRADATION_THRESHOLDS = {
    'win_rate': 0.55,                # Alert if < 55%
    'profit_factor': 1.5,            # Alert if < 1.5
    'consecutive_losses': 3,         # Alert after 3 losses
    'max_drawdown_pct': 10.0,        # Alert if > 10%
    'rolling_30d_roi': -5.0,         # Alert if 30-day ROI < -5%
}
```

---

## 4. Prompt Evolution Mechanism

### 4.1 Evolution Triggers

Prompt evolution occurs when:

1. **Performance Degradation**: Metrics fall below thresholds
2. **Scheduled Review**: Weekly analysis of last 50 trades
3. **Pattern Discovery**: High-confidence insights identified
4. **Manual Trigger**: User requests prompt optimization

### 4.2 Evolution Process

```python
class PromptEvolutionEngine:

    def evolve_prompt(self, current_prompt_version) -> str:
        """Generate improved prompt based on performance analysis"""

        # 1. Analyze recent performance
        performance = self.analyze_performance(
            version_id=current_prompt_version.id,
            lookback_trades=50
        )

        # 2. Identify problems
        problems = self.identify_problems(performance)
        # Examples:
        #   - "Holding losers too long (avg -8% before exit)"
        #   - "Selling winners prematurely (missed +15% gains)"
        #   - "Over-diversifying (5+ positions with $50 balance)"

        # 3. Extract successful patterns
        patterns = self.extract_winning_patterns(
            version_id=current_prompt_version.id
        )
        # Examples:
        #   - "Technical score > 0.9 + positive news → 85% win rate"
        #   - "Exit at +7% profit → better than holding to +15%"

        # 4. Generate prompt improvements using AI
        new_prompt = self.generate_improved_prompt(
            current_prompt=current_prompt_version.prompt_text,
            problems=problems,
            successful_patterns=patterns,
            insights=self.get_learning_insights()
        )

        # 5. Validate and test new prompt
        if self.validate_prompt(new_prompt):
            return new_prompt

        return None
```

### 4.3 Prompt Generation with Meta-AI

Use DeepSeek itself to improve its own prompt:

```python
META_PROMPT = """
You are analyzing the performance of an AI trading system to improve its decision-making prompt.

CURRENT PROMPT:
{current_prompt}

PERFORMANCE ANALYSIS (Last 50 trades):
- Win Rate: {win_rate}%
- Average P&L: {avg_pnl}%
- Profit Factor: {profit_factor}
- Max Drawdown: {max_drawdown}%

IDENTIFIED PROBLEMS:
{problems}

SUCCESSFUL PATTERNS:
{patterns}

LEARNING INSIGHTS:
{insights}

Based on this analysis, generate an IMPROVED version of the trading prompt that:
1. Addresses the identified problems
2. Reinforces the successful patterns
3. Applies the learning insights
4. Maintains the same structure and format
5. Is more specific and actionable

Return ONLY the improved prompt text.
"""
```

### 4.4 A/B Testing & Gradual Rollout

To safely test new prompts:

1. **Shadow Mode** (first 24 hours):
   - New prompt generates decisions but doesn't execute trades
   - Compare recommendations vs active prompt
   - Validate no obvious issues

2. **Split Testing** (next 3 days):
   - 20% of decisions use new prompt
   - 80% use current prompt
   - Track comparative performance

3. **Full Rollout** (if successful):
   - If new prompt shows >10% improvement → make active
   - If neutral → continue testing
   - If worse → archive and analyze why

---

## 5. Learning Insights Discovery

### 5.1 Automated Analysis Queries

Run periodic analysis to discover insights:

```sql
-- Find best-performing technical score ranges
SELECT
    CASE
        WHEN decision_context->>'technical_score' < '0.6' THEN 'Low'
        WHEN decision_context->>'technical_score' < '0.8' THEN 'Medium'
        ELSE 'High'
    END as score_range,
    COUNT(*) as trades,
    AVG(outcome_pnl_pct) as avg_return,
    SUM(CASE WHEN outcome_pnl_pct > 0 THEN 1 ELSE 0 END)::FLOAT / COUNT(*) as win_rate
FROM ai_decision_logs
WHERE outcome_tracked = TRUE
  AND operation IN ('buy', 'sell')
  AND decision_time > NOW() - INTERVAL '30 days'
GROUP BY score_range;

-- Find symbols with best performance
SELECT
    symbol,
    COUNT(*) as trades,
    AVG(outcome_pnl_pct) as avg_return,
    STDDEV(outcome_pnl_pct) as volatility,
    SUM(outcome_pnl) as total_pnl
FROM ai_decision_logs
WHERE outcome_tracked = TRUE
  AND symbol IS NOT NULL
GROUP BY symbol
ORDER BY avg_return DESC
LIMIT 10;

-- Analyze exit timing
SELECT
    outcome_exit_reason,
    COUNT(*) as count,
    AVG(outcome_pnl_pct) as avg_return,
    AVG(outcome_hold_duration_minutes) as avg_hold_minutes,
    AVG(outcome_max_profit_pct - outcome_pnl_pct) as missed_profit_pct
FROM ai_decision_logs
WHERE outcome_tracked = TRUE
  AND operation = 'sell'
GROUP BY outcome_exit_reason;
```

### 5.2 Insight Examples

Examples of actionable insights:

1. **"High technical scores (>0.9) don't guarantee profit"**
   - Evidence: 15 trades with score >0.9 had 45% win rate
   - Action: Reduce weight of technical score, increase news sentiment weight

2. **"Selling at +5% profit locks in wins effectively"**
   - Evidence: +5% exits averaged +5.2% return vs +7% exits averaged +4.8%
   - Action: Recommend prompt to favor +5% take profit

3. **"Diversification below $60 balance reduces returns"**
   - Evidence: 4+ positions with <$60 balance = 48% win rate vs 3 positions = 62%
   - Action: Adjust diversification strategy for account size

4. **"BTC outperforms altcoins in current market**"
   - Evidence: BTC trades: +8.5% avg, altcoins: +2.1% avg (last 30 days)
   - Action: Increase BTC allocation recommendations

---

## 6. Implementation Phases

### Phase 1: Enhanced Logging (Week 1)
- [x] Add new database tables (prompt_versions, enhanced ai_decision_logs)
- [ ] Implement context capture at decision time
- [ ] Create initial prompt version record
- [ ] Start logging with enhanced context

### Phase 2: Outcome Tracking (Week 1-2)
- [ ] Build trade outcome calculator
- [ ] Backfill outcomes for historical decisions
- [ ] Create scheduled job to update outcomes for closed positions
- [ ] Implement P&L tracking per decision

### Phase 3: Performance Analysis (Week 2-3)
- [ ] Build metrics calculation engine
- [ ] Create performance dashboard
- [ ] Implement pattern recognition queries
- [ ] Add degradation detection & alerts

### Phase 4: Prompt Evolution (Week 3-4)
- [ ] Build prompt evolution engine
- [ ] Implement meta-AI prompt generator
- [ ] Create A/B testing framework
- [ ] Test first evolved prompt

### Phase 5: Full Loop & Optimization (Week 4+)
- [ ] Enable automatic prompt evolution
- [ ] Optimize evolution frequency
- [ ] Fine-tune thresholds
- [ ] Monitor long-term improvement

---

## 7. Success Metrics

The self-learning system will be considered successful when:

1. **Profitability Improvement**: 30-day ROI increases by >20% vs baseline
2. **Win Rate Improvement**: Win rate improves from current ~50% to >60%
3. **Drawdown Reduction**: Max drawdown decreases by >30%
4. **Adaptive Behavior**: System automatically adjusts to market regime changes
5. **Prompt Evolution**: At least 2-3 successful prompt evolutions per month

---

## 8. Risk Mitigation

### 8.1 Safety Mechanisms

1. **Human Oversight**: All prompt changes logged and reviewable
2. **Rollback Capability**: Can instantly revert to previous prompt version
3. **Kill Switch**: Manual override to disable auto-evolution
4. **Conservative Thresholds**: Only promote prompts with clear improvement
5. **Maximum Evolution Rate**: Limit prompt changes to 1 per week

### 8.2 Testing Protocol

Before any new prompt goes live:
1. Validate JSON structure and all required fields present
2. Test on historical data (backtest last 100 trades)
3. Run in shadow mode for 24 hours
4. Check for logical consistency (no contradictions)
5. Require >10% improvement to promote

---

## 9. API Endpoints

New endpoints to support the self-learning system:

```python
# Performance Analysis
GET  /api/ai/performance                # Current performance metrics
GET  /api/ai/performance/history        # Historical performance over time
GET  /api/ai/insights                   # Discovered learning insights

# Prompt Management
GET  /api/ai/prompts                    # List all prompt versions
GET  /api/ai/prompts/{version_id}       # Get specific prompt version
POST /api/ai/prompts/evolve             # Trigger prompt evolution
POST /api/ai/prompts/{version_id}/activate  # Switch to different prompt
POST /api/ai/prompts/{version_id}/rollback  # Revert to previous prompt

# Analysis & Debugging
GET  /api/ai/decisions/analysis         # Detailed decision analysis
GET  /api/ai/patterns                   # Winning/losing patterns
GET  /api/ai/alerts                     # Performance alerts
```

---

## 10. Monitoring & Observability

### 10.1 Key Dashboards

1. **Performance Dashboard**:
   - Win rate trend (daily/weekly)
   - Cumulative P&L chart
   - Profit factor over time
   - Sharpe ratio evolution

2. **Decision Analysis Dashboard**:
   - Decision breakdown (buy/sell/hold %)
   - Outcome distribution (win/loss)
   - Hold duration histogram
   - Exit reason breakdown

3. **Prompt Evolution Dashboard**:
   - Prompt version timeline
   - Performance comparison across versions
   - Evolution trigger history
   - Active experiments (A/B tests)

### 10.2 Logging

Enhanced logging for debugging:

```python
logger.info("AI Decision", extra={
    "prompt_version": "v1.2",
    "symbol": "BTC",
    "operation": "buy",
    "technical_score": 0.92,
    "execution_time_ms": 1250,
    "context_snapshot": {...}
})

logger.info("Prompt Evolution", extra={
    "from_version": "v1.2",
    "to_version": "v1.3",
    "trigger": "degradation_alert",
    "changes_summary": "Tightened stop loss from -5% to -3%",
    "expected_improvement": "Reduce max drawdown"
})
```

---

## 11. Future Enhancements

Potential improvements for v2:

1. **Multi-Account Learning**: Learn from multiple accounts simultaneously
2. **Market Regime Detection**: Separate prompts for trending vs ranging markets
3. **Symbol-Specific Prompts**: Optimized prompts per asset class
4. **Ensemble Prompts**: Multiple prompts vote on decisions
5. **Reinforcement Learning**: Deep RL for continuous optimization
6. **Social Learning**: Learn from other traders (if multi-user)

---

## 12. References

Key research sources:
- Perplexity research on AI trading systems (see research notes)
- Best practices for prompt optimization
- Performance metrics for algorithmic trading
- Reinforcement learning in trading contexts

---

**Document Version**: 1.0
**Last Updated**: 2025-11-04
**Author**: Claude + User Requirements
**Status**: Design Complete, Ready for Implementation
