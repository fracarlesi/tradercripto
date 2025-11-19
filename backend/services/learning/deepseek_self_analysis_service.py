"""
DeepSeek Self-Analysis Service

Enables DeepSeek to analyze its own trading decisions and suggest improvements.
Uses counterfactual analysis to identify patterns of success and failure.

Key features:
- Analyzes last N decisions with counterfactual P&L
- Identifies systematic errors (e.g., "ignoring Prophet when RSI >70 costs $X")
- Suggests new indicator weights based on historical performance
- Proposes new trading rules based on discovered patterns
"""

import json
import logging
import requests
from typing import Any, Dict, List, Optional

from services.learning.decision_snapshot_service import get_snapshots_for_analysis

logger = logging.getLogger(__name__)


async def run_self_analysis(
    account_id: int, limit: int = 100, min_regret: Optional[float] = None
) -> Dict[str, Any]:
    """
    Run DeepSeek self-analysis on past decisions.

    Analyzes both executed trades AND missed opportunities to identify:
    - Patterns that lead to profit
    - Patterns that lead to losses
    - Systematic biases (e.g., ignoring certain indicators)
    - Optimal indicator weights based on actual performance

    Args:
        account_id: Account to analyze
        limit: Number of recent decisions to analyze (default: 100)
        min_regret: Only analyze decisions with regret >= this value

    Returns:
        Analysis results with suggested improvements

    Example:
        >>> analysis = await run_self_analysis(account_id=1, limit=50)
        >>> print(f"Total regret: ${analysis['total_regret_usd']:.2f}")
        >>> print(f"Suggested weights: {analysis['suggested_weights']}")
    """
    logger.info(
        f"Starting DeepSeek self-analysis for account_id={account_id} "
        f"(limit={limit}, min_regret={min_regret})"
    )

    try:
        # Fetch decision snapshots with counterfactuals
        snapshots = await get_snapshots_for_analysis(
            account_id=account_id, limit=limit, min_regret=min_regret
        )

        if not snapshots:
            logger.warning(f"No snapshots available for analysis (account_id={account_id})")
            return {
                "error": "No decision snapshots with counterfactuals available yet. "
                "Wait 24h after first decision for counterfactuals to be calculated."
            }

        logger.info(f"Analyzing {len(snapshots)} decision snapshots")

        # Build analysis prompt for DeepSeek
        analysis_prompt = _build_self_analysis_prompt(snapshots)

        # Call DeepSeek with self-analysis prompt
        from database.models import Account
        from database.connection import get_async_session_factory

        async with get_async_session_factory()() as db:
            from sqlalchemy import select

            stmt = select(Account).where(Account.id == account_id)
            result = await db.execute(stmt)
            account = result.scalar_one_or_none()

            if not account:
                raise ValueError(f"Account {account_id} not found")

            # Call DeepSeek API directly using requests
            logger.info("Calling DeepSeek for self-analysis...")

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {account.api_key}"
            }

            payload = {
                "model": account.model or "deepseek-chat",
                "messages": [
                    {
                        "role": "system",
                        "content": "You are an AI trading system analyzing your own past decisions.",
                    },
                    {"role": "user", "content": analysis_prompt},
                ],
                "temperature": 0.3,  # Lower temperature for analytical tasks
                "response_format": {"type": "json_object"},  # Force JSON response
            }

            # Construct API endpoint URL
            base_url = account.base_url.rstrip("/")
            api_endpoint = f"{base_url}/chat/completions"

            response = requests.post(
                api_endpoint,
                headers=headers,
                json=payload,
                timeout=60,  # Longer timeout for analysis
                verify=False,  # Disable SSL verification for custom endpoints
            )

            response.raise_for_status()

            result_data = response.json()
            analysis_text = result_data["choices"][0]["message"]["content"]
            analysis = json.loads(analysis_text)

            logger.info(
                f"Self-analysis complete: "
                f"Total regret=${analysis.get('total_regret_usd', 0):.2f}, "
                f"Accuracy={analysis.get('accuracy_rate', 0):.1%}"
            )

            # AUTO-APPLICATION: Apply suggested weights once per day
            if analysis.get("suggested_weights"):
                from services.learning.indicator_weights_service import (
                    should_auto_apply_today,
                    apply_indicator_weights,
                )

                # Check if we should auto-apply today
                should_apply = await should_auto_apply_today(account_id)

                if should_apply:
                    # Validate weights before applying
                    if _validate_weights(analysis["suggested_weights"]):
                        try:
                            # Blend weights: 70% old + 30% new (gradual adjustment)
                            blended_weights = _blend_weights(
                                current_weights=account.indicator_weights,
                                suggested_weights=analysis["suggested_weights"],
                            )

                            # Apply using the service (tracks history)
                            await apply_indicator_weights(
                                account_id=account_id,
                                suggested_weights=blended_weights,
                                source="auto_daily",
                                session=db,
                            )

                            logger.info(
                                f"✅ Auto-applied blended weights to account {account_id}",
                                extra={
                                    "context": {
                                        "account_id": account_id,
                                        "old_weights": account.indicator_weights,
                                        "suggested_weights": analysis["suggested_weights"],
                                        "blended_weights": blended_weights,
                                    }
                                },
                            )
                        except Exception as e:
                            logger.error(
                                f"Failed to auto-apply weights to account {account_id}",
                                extra={"context": {"error": str(e)}},
                                exc_info=True,
                            )
                    else:
                        logger.warning(
                            f"Skipping auto-apply - suggested weights failed validation"
                        )
                else:
                    logger.debug(
                        f"Skipping auto-apply for account {account_id} (already applied today or disabled)"
                    )

            return analysis

    except Exception as e:
        logger.error(
            f"Self-analysis failed for account_id={account_id}: {e}",
            extra={"context": {"account_id": account_id, "error": str(e)}},
            exc_info=True,
        )
        raise


def _build_self_analysis_prompt(snapshots: List[Dict[str, Any]]) -> str:
    """
    Build prompt for DeepSeek self-analysis.

    Args:
        snapshots: List of decision snapshots with counterfactuals

    Returns:
        Formatted prompt for DeepSeek
    """
    # Calculate summary statistics
    total_snapshots = len(snapshots)
    total_regret = sum(s.get("regret", 0) or 0 for s in snapshots)
    total_actual_pnl = sum(s.get("actual_pnl", 0) or 0 for s in snapshots)

    # Count optimal vs actual decisions
    correct_decisions = sum(
        1 for s in snapshots if s.get("actual_decision") == s.get("optimal_decision")
    )
    accuracy_rate = correct_decisions / total_snapshots if total_snapshots > 0 else 0

    # Group by decision type
    decision_counts = {"LONG": 0, "SHORT": 0, "HOLD": 0}
    for s in snapshots:
        decision = s.get("actual_decision")
        if decision in decision_counts:
            decision_counts[decision] += 1

    # Sample snapshots (include highest regret + random selection)
    sorted_by_regret = sorted(snapshots, key=lambda x: x.get("regret", 0) or 0, reverse=True)
    top_mistakes = sorted_by_regret[:10]  # Top 10 mistakes
    random_sample = snapshots[10:30]  # 20 random decisions
    sample_snapshots_full = top_mistakes + random_sample

    # Create SLIM version of snapshots (only essential data to reduce token usage)
    # Full snapshots can be 50k+ tokens → slim version ~5k tokens
    sample_snapshots = []
    for s in sample_snapshots_full:
        # Extract only key indicator values from indicators_snapshot
        indicators = s.get("indicators_snapshot", {})
        tech_factors = indicators.get("technical_factors", {})

        # Get symbol data if available
        symbol = s.get("symbol", "UNKNOWN")
        symbol_data = None
        if tech_factors.get("recommendations"):
            symbol_data = next(
                (r for r in tech_factors["recommendations"] if r["symbol"] == symbol),
                None
            )

        slim_snapshot = {
            "symbol": symbol,
            "timestamp": s.get("timestamp"),
            "actual_decision": s.get("actual_decision"),
            "optimal_decision": s.get("optimal_decision"),
            "regret_usd": s.get("regret", 0),
            "entry_price": s.get("entry_price"),
            "exit_price_24h": s.get("exit_price_24h"),
            "actual_pnl_usd": s.get("actual_pnl", 0),
            # Include ONLY key indicator values (not full data)
            "indicators": {
                "score": symbol_data.get("score") if symbol_data else None,
                "momentum": symbol_data.get("momentum") if symbol_data else None,
                "support": symbol_data.get("support") if symbol_data else None,
                "prophet_trend": symbol_data.get("prophet_forecast", {}).get("trend") if symbol_data else None,
                "prophet_change_24h": symbol_data.get("prophet_forecast", {}).get("change_pct_24h") if symbol_data else None,
                "pivot_zone": symbol_data.get("pivot_points", {}).get("current_zone") if symbol_data else None,
                "rsi": symbol_data.get("rsi") if symbol_data else None,
            },
            # Short reasoning summary (first 200 chars to save tokens)
            "reasoning_summary": (s.get("deepseek_reasoning", "")[:200] + "..."
                                if len(s.get("deepseek_reasoning", "")) > 200
                                else s.get("deepseek_reasoning", "")),
        }
        sample_snapshots.append(slim_snapshot)

    prompt = f"""Analyze your past {total_snapshots} trading decisions and identify patterns to improve performance.

**Summary Statistics**:
- Total Regret: ${total_regret:.2f} (money left on table by not choosing optimal decision)
- Actual P&L: ${total_actual_pnl:.2f}
- Potential P&L (if perfect): ${total_actual_pnl + total_regret:.2f}
- Accuracy Rate: {accuracy_rate:.1%} (% times you chose the optimal decision)
- Decision Breakdown: {decision_counts['LONG']} LONG, {decision_counts['SHORT']} SHORT, {decision_counts['HOLD']} HOLD

**Your Decision Snapshots** (sample of {len(sample_snapshots)}, condensed for analysis):

{json.dumps(sample_snapshots, indent=2)}

**Analysis Task**:

1. **MISSED OPPORTUNITIES**: Identify patterns where you made HOLD but should have made LONG/SHORT
   - Example: "10 times Prophet said BULLISH >+2% but RSI >70 scared me → missed $150"
   - Look for indicator combinations that YOU ignored but would have been profitable

2. **GOOD HOLDS**: Identify times you correctly avoided losses by making HOLD
   - Example: "8 times Sentiment >80 + Whale sell → correctly avoided -$230 in losses"

3. **SYSTEMATIC ERRORS**: Find biases in your decision-making
   - Do you ignore certain indicators too often?
   - Do you have a long/short bias?
   - Do you over-react to certain signals?

4. **INDICATOR PERFORMANCE**: For each indicator, calculate win rate when you followed it
   - Prophet: When Prophet said BULLISH and you went LONG, what was the win rate?
   - RSI: When RSI >70 and you went SHORT, was it correct?

5. **OPTIMAL WEIGHTS**: Based on actual performance, suggest new indicator weights
   - If Prophet has 80% accuracy → increase weight
   - If Sentiment has 40% accuracy → decrease weight

**Output Format** (JSON):
```json
{{
    "total_regret_usd": float,
    "total_actual_pnl": float,
    "potential_pnl_if_perfect": float,
    "accuracy_rate": float,

    "worst_patterns": [
        {{
            "pattern": "Ignored Prophet BULLISH when RSI >70",
            "occurrences": 12,
            "total_regret": 145.50,
            "explanation": "Prophet was correct 10/12 times, RSI overbought is often ignored in strong trends"
        }}
    ],

    "best_patterns": [
        {{
            "pattern": "HOLD when Sentiment >80 + Whale sell",
            "occurrences": 8,
            "avoided_losses": 230.00,
            "explanation": "This combination correctly predicted reversals"
        }}
    ],

    "indicator_performance": {{
        "prophet": {{"win_rate": 0.75, "times_followed": 30, "avg_pnl_when_followed": 15.20}},
        "pivot_points": {{"win_rate": 0.68, "times_followed": 25, "avg_pnl_when_followed": 12.50}},
        "rsi_macd": {{"win_rate": 0.52, "times_followed": 40, "avg_pnl_when_followed": 3.10}},
        "whale_alerts": {{"win_rate": 0.60, "times_followed": 15, "avg_pnl_when_followed": 8.00}},
        "sentiment": {{"win_rate": 0.45, "times_followed": 20, "avg_pnl_when_followed": -2.50}},
        "news": {{"win_rate": 0.50, "times_followed": 10, "avg_pnl_when_followed": 1.00}}
    }},

    "suggested_weights": {{
        "prophet": 0.65,
        "pivot_points": 0.75,
        "rsi_macd": 0.40,
        "whale_alerts": 0.50,
        "sentiment": 0.20,
        "news": 0.15
    }},

    "new_rules": [
        "When Prophet >+2% confidence >0.95 → LONG even if RSI >70",
        "When Sentiment >85 AND Whale sell → HOLD (override other signals)",
        "When Pivot breakout + Prophet confirm → increase size to 30%"
    ],

    "summary": "Your biggest mistake is ignoring Prophet when RSI is overbought. Prophet has 75% win rate but you only followed it 30/100 times. Sentiment indicator is noisy (45% accuracy) - reduce weight from 0.3 to 0.2. Overall, trust technical indicators (Prophet, Pivot) more than sentiment."
}}
```

Be brutally honest about mistakes. Your goal is to maximize profit, not validate past decisions.
"""

    return prompt


def _validate_weights(weights: Dict[str, float]) -> bool:
    """
    Validate strategy weights before applying.

    Rules:
    - All weights must be between 0.1 and 1.0
    - Must contain expected indicators
    - No negative values

    Args:
        weights: Dictionary of indicator weights

    Returns:
        True if weights are valid, False otherwise
    """
    expected_indicators = ["prophet", "pivot_points", "rsi_macd", "whale_alerts", "sentiment", "news"]

    # Check all expected indicators are present
    for indicator in expected_indicators:
        if indicator not in weights:
            logger.warning(f"Missing expected indicator in suggested_weights: {indicator}")
            return False

        value = weights[indicator]

        # Check value is numeric
        if not isinstance(value, (int, float)):
            logger.warning(f"Invalid weight type for {indicator}: {type(value)}")
            return False

        # Check value is in valid range [0.1, 1.0]
        if value < 0.1 or value > 1.0:
            logger.warning(f"Weight out of range for {indicator}: {value} (must be 0.1-1.0)")
            return False

    logger.info("✅ Suggested weights validation passed")
    return True


def _blend_weights(
    current_weights: Optional[Dict[str, float]],
    suggested_weights: Dict[str, float],
    blend_old: float = 0.7,
    blend_new: float = 0.3,
) -> Dict[str, float]:
    """
    Blend current weights with suggested weights for gradual adjustment.

    This prevents sudden strategy shifts while allowing AI to improve over time.

    Args:
        current_weights: Current indicator weights (None = use defaults)
        suggested_weights: New weights from DeepSeek analysis
        blend_old: Weight for current values (default: 0.7)
        blend_new: Weight for suggested values (default: 0.3)

    Returns:
        Blended weights dictionary

    Example:
        >>> current = {"prophet": 0.5, "pivot_points": 0.8}
        >>> suggested = {"prophet": 0.9, "pivot_points": 0.6}
        >>> _blend_weights(current, suggested)
        {"prophet": 0.62, "pivot_points": 0.74}  # 70% old + 30% new
    """
    # Default weights if current_weights is None
    default_weights = {
        "pivot_points": 0.8,
        "prophet": 0.5,
        "rsi_macd": 0.5,
        "whale_alerts": 0.4,
        "sentiment": 0.3,
        "news": 0.2,
    }

    current_weights = current_weights or default_weights

    blended_weights = {}
    for indicator, suggested_value in suggested_weights.items():
        current_value = current_weights.get(indicator, 0.5)  # Fallback to 0.5 if missing

        # Gradual blend: blend_old% old + blend_new% new
        blended_value = blend_old * current_value + blend_new * suggested_value

        # Round to 2 decimal places
        blended_weights[indicator] = round(blended_value, 2)

    logger.info(
        f"Blended weights ({blend_old*100:.0f}% old + {blend_new*100:.0f}% new): {blended_weights}"
    )

    return blended_weights


# Expose for API endpoint
__all__ = ["run_self_analysis"]
