"""
Daily analysis DeepSeek prompts.

Builds prompts for daily evening analysis focused on skill-based metrics
and actionable improvements.
"""

import json
import logging
from typing import Dict, List, Any

logger = logging.getLogger(__name__)


def build_daily_analysis_prompt(
    snapshots: List[Dict[str, Any]],
    trades: List[Dict[str, Any]],
    metrics: Dict[str, float]
) -> str:
    """
    Build prompt for daily analysis.

    Args:
        snapshots: List of decision snapshots from today
        trades: List of completed trades from today
        metrics: Skill-based metrics calculated

    Returns:
        Formatted prompt for DeepSeek
    """

    # Truncate snapshots for token efficiency (keep max 20 samples)
    if len(snapshots) > 20:
        # Take first 10 and last 10
        sample_snapshots = snapshots[:10] + snapshots[-10:]
        logger.info(f"Truncated {len(snapshots)} snapshots to 20 for prompt")
    else:
        sample_snapshots = snapshots

    # Truncate trades (keep max 15)
    if len(trades) > 15:
        sample_trades = trades[:15]
        logger.info(f"Truncated {len(trades)} trades to 15 for prompt")
    else:
        sample_trades = trades

    prompt = f"""Analyze today's trading performance and suggest improvements.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📋 CONTEXT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

You are analyzing a SINGLE day of trading (not weeks/months).
Focus on identifying patterns in today's decisions that can be improved tomorrow.

**IMPORTANT**: All metrics below are SKILL-BASED (not market-dependent).
They measure YOUR ability to make good decisions, not the market's direction.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 SKILL-BASED METRICS (Today)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**Decision Quality**:
- Total Decisions: {metrics['total_decisions']} (evaluations made)
- Total Trades: {metrics['total_trades']} (executed)
- Win Rate: {metrics['win_rate_pct']:.1f}% ({metrics['winning_trades']}/{metrics['total_trades']} wins)
- Profit Factor: {metrics['profit_factor']:.2f} (gross profit / gross loss)
- Risk/Reward Ratio: {metrics['risk_reward_ratio']:.2f} (avg win / avg loss)

**Risk Management**:
- Max Drawdown: {metrics['max_drawdown_pct']:.1f}% (peak to trough)
- Sharpe Ratio: {metrics['sharpe_ratio']:.2f} (return / volatility)
- Sortino Ratio: {metrics['sortino_ratio']:.2f} (return / downside deviation)

**Execution Quality**:
- Entry Timing: {metrics['entry_timing_quality_pct']:.1f}% (how close to candle low)
- Exit Timing: {metrics['exit_timing_quality_pct']:.1f}% (how close to candle high)
- False Signal Rate: {metrics['false_signal_rate_pct']:.1f}% (trades < 1h with loss)
- Avg Hold Time: {metrics['avg_hold_time_hours']:.1f}h (target: 1-6h)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ METRICS INTERPRETATION GUIDE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**What's GOOD**:
- Win rate > 50% → More good decisions than bad ones
- Profit factor > 1.5 → Winning trades are larger than losing ones
- Risk/Reward > 1.5 → Good risk management (small losses, big wins)
- Max drawdown < 5% → Controlled losses
- Sharpe ratio > 1.0 → Good risk-adjusted returns
- Entry/Exit timing > 60% → Entering near lows, exiting near highs
- False signal rate < 15% → Not chasing weak signals

**What's BAD**:
- Win rate < 45% → Making more bad decisions than good ones
- Profit factor < 1.0 → Losing more than winning
- Risk/Reward < 1.0 → Letting losses run, cutting winners early
- Max drawdown > 10% → Not managing risk properly
- Entry/Exit timing < 40% → Bad execution (buying high, selling low)
- False signal rate > 25% → Too many weak signals followed

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🧠 TODAY'S DECISIONS (sample of {len(sample_snapshots)}/{metrics['total_decisions']})
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

```json
{json.dumps([{
    "time": s['timestamp'].strftime('%H:%M'),
    "symbol": s['symbol'],
    "decision": s['actual_decision'],
    "reasoning": s['reasoning'][:150] + "..." if len(s['reasoning']) > 150 else s['reasoning']
} for s in sample_snapshots], indent=2)}
```

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
💰 TODAY'S COMPLETED TRADES ({len(trades)} total, showing {len(sample_trades)})
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

```json
{json.dumps([{
    "symbol": t['symbol'],
    "entry": t['entry_time'].strftime('%H:%M'),
    "exit": t['exit_time'].strftime('%H:%M'),
    "pnl": round(t['pnl'], 2),
    "pnl_pct": round(t['pnl_pct'], 2),
    "duration_min": t['duration_minutes']
} for t in sample_trades], indent=2)}
```

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎯 ANALYSIS TASK
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Your job is to identify ACTIONABLE patterns that can improve tomorrow's trading.

**1. INDICATOR PERFORMANCE**
For each indicator (Prophet, Pivot Points, RSI/MACD, EMA Alignment, etc.):
- Did it work well today? (accuracy %)
- How many times was it used?
- Win rate when followed?
- Any systematic errors?

**2. WORST MISTAKES**
Identify the 2-3 BIGGEST mistakes made today:
- What decision was made? (symbol, LONG/SHORT/HOLD)
- Why was it wrong? (which indicators were ignored?)
- How much did it cost? (missed profit or actual loss)
- What should have been done instead?

**3. SYSTEMATIC ERRORS**
Look for patterns in mistakes:
- Do you ignore strong signals? (high confidence but made HOLD)
- Do you over-trade weak signals? (low confidence but made LONG/SHORT)
- Entry/Exit timing issues? (buying at candle highs, selling at lows)
- Do you exit winners too early? (< 1h profitable trades)
- Do you let losers run? (long losing trades)

**4. SUGGESTED WEIGHTS**
Based on today's performance, suggest new indicator weights (0.1-1.0).
- If an indicator had 70%+ accuracy → increase weight
- If an indicator gave false signals → decrease weight
- Suggest gradual changes (max ±0.15 per indicator)

**5. SUGGESTED PROMPT MODIFICATIONS**
Propose SPECIFIC rules to add/remove from the trading decision prompt:
- Add rules: New patterns discovered that work
- Remove rules: Existing rules that don't work

Examples:
- "When Prophet confidence >0.9 and trend BULLISH, ignore RSI overbought (>70)"
- "Avoid trading symbols with volume < $50k/hour"
- "Remove contrarian sentiment trading - not working in current market"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📋 OUTPUT FORMAT (JSON)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Respond ONLY with a JSON object in this exact format:

```json
{{
    "summary": "Brief 2-3 sentence summary of today's performance and key insights",

    "indicator_performance": {{
        "prophet": {{
            "accuracy_pct": 75.0,
            "times_used": 4,
            "win_rate": 80.0,
            "notes": "Bullish signals were accurate, predicted BTC rally correctly"
        }},
        "pivot_points": {{
            "accuracy_pct": 60.0,
            "times_used": 5,
            "win_rate": 50.0,
            "notes": "Support levels held, but resistance levels were broken"
        }},
        "rsi_macd": {{
            "accuracy_pct": 40.0,
            "times_used": 6,
            "win_rate": 33.0,
            "notes": "Gave many false overbought signals in trending market"
        }},
        "ema_alignment": {{
            "accuracy_pct": 70.0,
            "times_used": 8,
            "win_rate": 75.0,
            "notes": "Trend following worked well today"
        }}
    }},

    "worst_mistakes": [
        {{
            "trade_symbol": "BTC",
            "mistake": "Ignored Prophet BULLISH (+2.5%) because RSI >70",
            "cost_usd": 15.50,
            "lesson": "RSI overbought is OK in strong trends - follow Prophet when confidence >0.9"
        }},
        {{
            "trade_symbol": "ETH",
            "mistake": "Entered at candle high (poor entry timing)",
            "cost_usd": 8.20,
            "lesson": "Wait for pullback before entry, don't chase momentum"
        }}
    ],

    "systematic_errors": [
        "Over-trading on weak signals (confidence <0.6) - 40% of trades had confidence <0.6 and lost money",
        "Exiting winners too early - Average winning trade held only 1.2h (target: 3-6h)",
        "Bad entry timing - Buying near candle highs (entry quality 42%)"
    ],

    "suggested_weights": {{
        "prophet": 0.65,
        "pivot_points": 0.75,
        "rsi_macd": 0.35,
        "whale_alerts": 0.45,
        "sentiment": 0.25,
        "news": 0.20
    }},

    "suggested_prompt_changes": {{
        "add_rules": [
            "When Prophet confidence >0.9 and trend BULLISH, ignore RSI overbought (>70)",
            "Require minimum confidence 0.65 for all trades (was 0.60)",
            "For LONG positions, wait for entry_timing_quality >50% (avoid buying candle highs)"
        ],
        "remove_rules": [
            "Contrarian sentiment trading (extreme fear = buy) - not working in trending markets"
        ]
    }}
}}
```

**CRITICAL RULES**:
1. Be BRUTALLY HONEST about mistakes - don't sugarcoat
2. Focus on ACTIONABLE improvements (not vague observations)
3. Base suggestions on TODAY's data (not general theory)
4. Suggest gradual weight changes (max ±0.15 per indicator)
5. Prompt modifications must be SPECIFIC and implementable

Your goal is to help the system make BETTER decisions tomorrow.
"""

    return prompt
