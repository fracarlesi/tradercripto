# Daily Learning System - Complete Documentation

**Status**: ✅ Production Ready
**Last Updated**: 2025-11-20
**Version**: 1.0

---

## 📋 Overview

The Daily Learning System analyzes trading performance every evening at **21:00** and generates actionable suggestions to improve future trading decisions.

### Key Principles

1. **Skill-Based Metrics** - Measures trading skill, NOT market direction
2. **Manual Review** - User approves all changes (no auto-apply)
3. **Daily Feedback** - Fast iteration cycle (not 24h delay per trade)
4. **Actionable Suggestions** - Specific weight changes and prompt rules

---

## 🏗️ Architecture

### System Components

```
┌─────────────────────────────────────────────────────┐
│ 21:00 - Daily Analysis Cron Job                     │
│ (main.py:194-200)                                   │
└──────────────────┬──────────────────────────────────┘
                   ↓
┌─────────────────────────────────────────────────────┐
│ Daily Analysis Service                              │
│ (daily_analysis_service.py)                         │
│                                                     │
│ 1. Fetch today's decision snapshots (every 3 min)  │
│ 2. Fetch today's completed trades                  │
│ 3. Calculate skill-based metrics                   │
│ 4. Call DeepSeek for pattern analysis              │
│ 5. Save report to database                         │
└──────────────────┬──────────────────────────────────┘
                   ↓
┌─────────────────────────────────────────────────────┐
│ DailyLearningReport (database)                      │
│ - skill_metrics (JSON)                              │
│ - deepseek_analysis (JSON)                          │
│ - suggested_weights (JSON)                          │
│ - suggested_prompt_changes (JSON)                   │
│ - status: pending                                   │
└──────────────────┬──────────────────────────────────┘
                   ↓
┌─────────────────────────────────────────────────────┐
│ User Reviews via Dashboard                          │
│ - Views metrics and suggestions                     │
│ - Approves/Dismisses changes                        │
│ - Applies weights (automatic)                       │
│ - Applies prompts (manual code update)              │
└─────────────────────────────────────────────────────┘
```

---

## 📊 Skill-Based Metrics

### What Makes a Metric "Skill-Based"?

A skill-based metric measures the trader's (AI's) ability to make good decisions, **independent of market direction**.

**Example**:
- ❌ **Total P&L in USD** - Market-dependent (bull market → everyone profits)
- ✅ **Win Rate %** - Skill-based (measures decision accuracy regardless of market)

### Calculated Metrics

| Metric | Formula | Interpretation | Target |
|--------|---------|----------------|--------|
| **Win Rate (%)** | (Winning trades / Total trades) × 100 | % of trades that were profitable | > 50% |
| **Profit Factor** | Gross profit / Gross loss | Quality of risk management | > 1.5 |
| **Risk/Reward Ratio** | Avg win / Avg loss | Ability to cut losses, let winners run | > 1.5 |
| **Max Drawdown (%)** | (Peak - Trough) / Peak × 100 | Worst equity decline from peak | < 5% |
| **Sharpe Ratio** | (Avg return - Risk-free rate) / Std dev | Risk-adjusted return | > 1.0 |
| **Sortino Ratio** | (Avg return - Risk-free rate) / Downside std dev | Downside risk-adjusted return | > 1.0 |
| **Entry Timing Quality (%)** | 1 - (Entry price - Candle low) / (High - Low) | How close to optimal entry (candle low) | > 60% |
| **Exit Timing Quality (%)** | (Exit price - Candle low) / (High - Low) | How close to optimal exit (candle high) | > 60% |
| **False Signal Rate (%)** | (Trades < 1h with loss / Total trades) × 100 | % of weak signals followed | < 15% |
| **Avg Hold Time (hours)** | Avg duration in hours | Alignment with strategy (target: 1-6h) | 1-6h |

### Implementation

**File**: `backend/services/learning/skill_metrics_calculator.py`

```python
async def calculate_daily_skill_metrics(
    account_id: int,
    target_date: date
) -> Dict[str, Any]:
    """
    Calculate skill-based metrics for a specific trading day.

    Returns:
        {
            "win_rate_pct": 65.0,
            "profit_factor": 2.3,
            "sharpe_ratio": 1.2,
            "max_drawdown_pct": 3.5,
            "entry_timing_quality_pct": 72.0,
            "exit_timing_quality_pct": 68.0,
            "false_signal_rate_pct": 12.0,
            "avg_hold_time_hours": 2.4,
            ...
        }
    """
```

---

## 🤖 DeepSeek Analysis

### Prompt Structure

**File**: `backend/services/learning/daily_deepseek_prompts.py`

The prompt is designed to extract actionable patterns from today's trading:

1. **Context**: Single day analysis (not weeks/months)
2. **Metrics**: Skill-based performance indicators
3. **Decisions**: Sample of today's AI decisions (up to 20)
4. **Trades**: Sample of completed trades (up to 15)
5. **Analysis Tasks**:
   - Indicator performance (accuracy, win rate when followed)
   - Worst mistakes (symbol, cost, lesson learned)
   - Systematic errors (patterns in bad decisions)
   - Suggested weights (based on performance)
   - Suggested prompt changes (add/remove rules)

### Output Format

```json
{
    "summary": "Brief 2-3 sentence summary of today's performance",

    "indicator_performance": {
        "prophet": {
            "accuracy_pct": 75.0,
            "times_used": 4,
            "win_rate": 80.0,
            "notes": "Bullish signals were accurate"
        },
        "rsi_macd": {
            "accuracy_pct": 40.0,
            "times_used": 6,
            "win_rate": 33.0,
            "notes": "Gave false overbought signals"
        },
        ...
    },

    "worst_mistakes": [
        {
            "trade_symbol": "BTC",
            "mistake": "Ignored Prophet BULLISH (+2.5%) because RSI >70",
            "cost_usd": 15.50,
            "lesson": "Follow Prophet when confidence >0.9"
        }
    ],

    "suggested_weights": {
        "prophet": 0.65,  // Increased from 0.50
        "rsi_macd": 0.35,  // Decreased from 0.50
        ...
    },

    "suggested_prompt_changes": {
        "add_rules": [
            "When Prophet confidence >0.9, ignore RSI overbought"
        ],
        "remove_rules": [
            "Contrarian sentiment trading"
        ]
    }
}
```

---

## 🗄️ Database Schema

### DailyLearningReport

```sql
CREATE TABLE daily_learning_reports (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,

    -- Date analyzed
    report_date DATE NOT NULL,
    analyzed_at TIMESTAMP NOT NULL DEFAULT NOW(),

    -- Analysis data (JSON)
    skill_metrics JSONB NOT NULL,
    deepseek_analysis JSONB NOT NULL,
    suggested_weights JSONB,
    suggested_prompt_changes JSONB,

    -- Status tracking
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    reviewed_at TIMESTAMP,
    review_notes TEXT,

    -- Unique constraint: one report per account per day
    UNIQUE(account_id, report_date)
);

CREATE INDEX idx_daily_reports_date ON daily_learning_reports(report_date);
CREATE INDEX idx_daily_reports_status ON daily_learning_reports(status);
```

### DecisionSnapshot (Updated)

Added link to daily report:

```sql
ALTER TABLE decision_snapshots
ADD COLUMN analyzed_in_daily_report_id INTEGER
REFERENCES daily_learning_reports(id) ON DELETE SET NULL;
```

---

## 🔌 API Endpoints

### Base URL: `/api/daily-learning`

#### 1. List Reports

```http
GET /reports/{account_id}?limit=30&status=pending
```

**Response**:
```json
[
    {
        "id": 123,
        "report_date": "2025-11-20",
        "analyzed_at": "2025-11-20T21:05:00Z",
        "status": "pending",
        "win_rate_pct": 65.0,
        "profit_factor": 2.3,
        "total_trades": 8,
        "suggested_weights_count": 6,
        "suggested_rules_count": 3
    }
]
```

#### 2. Get Specific Report

```http
GET /reports/{account_id}/2025-11-20
```

**Response**:
```json
{
    "id": 123,
    "account_id": 1,
    "report_date": "2025-11-20",
    "status": "pending",
    "skill_metrics": {...},
    "deepseek_analysis": {...},
    "suggested_weights": {...},
    "suggested_prompt_changes": {...}
}
```

#### 3. Apply Suggested Weights

```http
POST /reports/{report_id}/apply-weights
Content-Type: application/json

{
    "notes": "Applying weights after review"
}
```

**Response**:
```json
{
    "status": "success",
    "message": "Weights applied successfully",
    "old_weights": {"prophet": 0.50, ...},
    "new_weights": {"prophet": 0.65, ...},
    "diff": {
        "prophet": {
            "old": 0.50,
            "new": 0.65,
            "change": +0.15
        }
    }
}
```

#### 4. Get Prompt Change Instructions

```http
GET /reports/{report_id}/prompt-instructions
```

**Response**:
```json
{
    "status": "instructions_ready",
    "file_path": "backend/services/ai/deepseek_client.py",
    "add_rules": [
        "When Prophet confidence >0.9, ignore RSI overbought"
    ],
    "remove_rules": [
        "Contrarian sentiment trading"
    ],
    "instructions_markdown": "# Manual Prompt Update Instructions\n\n..."
}
```

#### 5. Mark Prompts as Applied

```http
POST /reports/{report_id}/mark-prompts-applied
Content-Type: application/json

{
    "notes": "Updated deepseek_client.py with new rules"
}
```

#### 6. Dismiss Report

```http
POST /reports/{report_id}/dismiss
Content-Type: application/json

{
    "reason": "Suggestions not applicable for current market conditions"
}
```

#### 7. Trigger Manual Analysis (Testing/Backfill)

```http
POST /trigger-analysis/1?target_date=2025-11-19
```

---

## ⏰ Scheduler Configuration

### Cron Job

**File**: `backend/main.py:194-200`

```python
from services.learning.daily_analysis_service import run_daily_analysis_sync

scheduler_service.add_cron_job(
    job_func=run_daily_analysis_sync,
    hour=21,
    minute=0,
    job_id="daily_evening_analysis"
)
logger.info("✅ daily_evening_analysis enabled (21:00 every day)")
```

### Manual Trigger

For testing or backfilling missing days:

```python
from services.learning.daily_analysis_service import run_daily_analysis
from datetime import date

# Analyze specific date
result = await run_daily_analysis(
    account_id=1,
    target_date=date(2025, 11, 19)
)
```

---

## 🔍 Troubleshooting

### No Trades Found

**Symptom**: Daily analysis returns `"status": "no_trades"`

**Cause**: No completed trades on that day (entry + exit both on same day)

**Solution**: Normal for low-activity days. Report will not be generated.

### DeepSeek Analysis Failed

**Symptom**: Daily analysis returns `"status": "error", "message": "DeepSeek analysis failed"`

**Causes**:
1. API timeout (>90s)
2. Invalid API key
3. JSON parsing error

**Debug**:
```bash
# Check logs
grep "DeepSeek API call failed" logs.json | jq '.exception'

# Test API manually
curl -X POST "https://api.deepseek.com/chat/completions" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "deepseek-chat", "messages": [{"role": "user", "content": "test"}]}'
```

### Metrics Calculation Error

**Symptom**: `calculate_daily_skill_metrics` throws exception

**Causes**:
1. Missing trade data (entry_price, exit_price, pnl)
2. Division by zero (no trades or all zeros)

**Debug**:
```python
# Check trade data integrity
from services.learning.skill_metrics_calculator import _fetch_daily_trades
from datetime import date

trades = await _fetch_daily_trades(session, account_id=1, target_date=date.today())
print(f"Trades found: {len(trades)}")
for t in trades:
    print(f"{t['symbol']}: PNL={t['pnl']}, Duration={t['duration_minutes']}min")
```

---

## 📈 Best Practices

### Reviewing Reports

1. **Daily Habit**: Review reports every morning (after 21:00 generation)
2. **Focus on Patterns**: Look for recurring mistakes (3+ occurrences)
3. **Gradual Changes**: Don't apply large weight changes (max ±0.15)
4. **Test First**: Apply weights on Friday, monitor weekend performance

### Applying Weight Changes

**Do**:
- ✅ Apply when pattern is clear (3+ examples)
- ✅ Apply gradually (70% old + 30% new blend)
- ✅ Monitor performance for 3-5 days after change
- ✅ Revert if performance degrades

**Don't**:
- ❌ Apply based on single day's performance
- ❌ Make drastic changes (>0.20 weight shift)
- ❌ Apply without understanding the reasoning
- ❌ Auto-apply without review

### Prompt Modifications

**Guidelines**:
1. **Specific Rules Only**: "When X and Y, do Z" (not vague advice)
2. **Test Locally First**: Modify prompt, test with historical data
3. **One Rule at a Time**: Add/remove one rule, monitor impact
4. **Document Changes**: Add comment in code explaining why rule added

**Example - Good Rule**:
```
When Prophet confidence >0.9 and trend BULLISH, ignore RSI overbought (>70)
```

**Example - Bad Rule**:
```
Be more confident in trending markets
```

---

## 🚀 Deployment Checklist

### Pre-Production

- [x] Database migration created (`DailyLearningReport` table)
- [x] All services implemented and tested
- [x] API endpoints tested with Postman
- [x] Scheduler configured (21:00 cron)
- [x] Documentation complete

### Production Deployment

```bash
# 1. SSH to VPS
ssh root@46.224.45.196

# 2. Stop production
cd /opt/trader_bitcoin
docker compose -f docker-compose.simple.yml stop

# 3. Pull latest code
git pull origin main

# 4. Run migrations
docker compose -f docker-compose.simple.yml run --rm backend alembic upgrade head

# 5. Restart
docker compose -f docker-compose.simple.yml up -d

# 6. Verify scheduler
docker compose -f docker-compose.simple.yml logs -f | grep "daily_evening_analysis enabled"

# 7. Monitor first run (21:00)
docker compose -f docker-compose.simple.yml logs -f | grep "DAILY EVENING ANALYSIS"
```

### Post-Deployment Verification

```bash
# Check report generated
curl http://46.224.45.196:8000/api/daily-learning/reports/1 | jq '.[0]'

# Trigger manual test
curl -X POST http://46.224.45.196:8000/api/daily-learning/trigger-analysis/1

# Monitor logs
docker compose logs -f | grep "daily_evening_analysis"
```

---

## 📚 Related Documentation

- **CLAUDE.md** - Project overview and development rules
- **backend/docs/WEBSOCKET_ARCHITECTURE.md** - Real-time data streaming
- **backend/docs/DEPLOYMENT_ROADMAP.md** - Infrastructure and deployment

---

## 🔧 Maintenance

### Weekly Tasks

- Review pending reports (ensure none stuck in "pending")
- Check `indicator_weights_history` for weight drift over time
- Verify scheduler is running (check logs for 21:00 execution)

### Monthly Tasks

- Analyze weight history trends (are weights converging?)
- Review prompt modifications applied (are they still relevant?)
- Check database size (`daily_learning_reports` table growth)
- Cleanup old reports (>90 days, status="dismissed")

---

**End of Documentation**
