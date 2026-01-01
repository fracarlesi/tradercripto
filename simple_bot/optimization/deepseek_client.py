"""
DeepSeek Reasoner API Client
Handles communication with DeepSeek for parameter optimization.
"""

import os
import json
import httpx
from typing import Dict, Optional, Tuple
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class OptimizationResult:
    """Result from DeepSeek optimization call."""
    success: bool
    new_params: Optional[Dict]
    reasoning: str
    confidence: float
    raw_response: str
    prompt_tokens: int
    completion_tokens: int
    action: str  # "modify", "no_change", or "error"


class DeepSeekOptimizer:
    """
    Interfaces with DeepSeek Reasoner API to get parameter suggestions.
    Uses structured output for reliable parsing.
    """

    API_URL = "https://api.deepseek.com/v1/chat/completions"
    MODEL = "deepseek-chat"  # deepseek-reasoner has JSON truncation issues

    def __init__(self, api_key: str = None):
        """
        Initialize DeepSeek client.

        Args:
            api_key: DeepSeek API key (or uses DEEPSEEK_API_KEY env var)
        """
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        if not self.api_key:
            raise ValueError("DEEPSEEK_API_KEY not set")

        self.client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            },
            timeout=120.0  # LLM can take time
        )

    async def optimize_parameters(
        self,
        context: str,
        current_params: Dict
    ) -> OptimizationResult:
        """
        Send context to DeepSeek and get parameter recommendations.

        Args:
            context: Formatted context string from TieredSummarizer
            current_params: Current parameter configuration

        Returns:
            OptimizationResult with suggested changes
        """
        system_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(context, current_params)

        try:
            logger.info("Calling DeepSeek API...")

            response = await self.client.post(
                self.API_URL,
                json={
                    "model": self.MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    "temperature": 0.3,  # Lower for more consistent output
                    "max_tokens": 2000
                }
            )
            response.raise_for_status()
            data = response.json()

            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})

            logger.debug(f"DeepSeek response: {content[:500]}...")

            # Parse the structured response
            new_params, reasoning, confidence, action = self._parse_response(content)

            return OptimizationResult(
                success=new_params is not None,
                new_params=new_params,
                reasoning=reasoning,
                confidence=confidence,
                raw_response=content,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                action=action
            )

        except httpx.HTTPStatusError as e:
            logger.error(f"DeepSeek API error: {e.response.status_code} - {e.response.text}")
            return OptimizationResult(
                success=False,
                new_params=None,
                reasoning=f"API Error: {e.response.status_code}",
                confidence=0,
                raw_response="",
                prompt_tokens=0,
                completion_tokens=0,
                action="error"
            )

        except Exception as e:
            logger.error(f"DeepSeek error: {e}", exc_info=True)
            return OptimizationResult(
                success=False,
                new_params=None,
                reasoning=f"Error: {str(e)}",
                confidence=0,
                raw_response="",
                prompt_tokens=0,
                completion_tokens=0,
                action="error"
            )

    def _build_system_prompt(self) -> str:
        """Build system prompt for DeepSeek."""
        return """You are an expert trading system optimizer for a cryptocurrency perpetual futures trading bot on Hyperliquid DEX.

Your task is to analyze performance data and suggest parameter adjustments to OPTIMIZE FOR RISK-ADJUSTED RETURNS.

## PRIMARY OPTIMIZATION TARGETS (in order of priority):
1. **Sharpe Ratio >= 1.5** - Risk-adjusted returns are the primary goal
2. **Profit Factor >= 1.5** - Gross profits should be at least 1.5x gross losses
3. **Max Drawdown <= 10%** - Protect capital, avoid large drawdowns
4. **Positive Net P&L** - Must be profitable overall

## STRATEGY OVERVIEW
The bot runs 3 strategies concurrently:
1. **MOMENTUM**: Follows trends using EMA crossover + RSI confirmation
   - Goes LONG when fast EMA > slow EMA AND RSI > long_threshold
   - Goes SHORT when fast EMA < slow EMA AND RSI < short_threshold

2. **MEAN_REVERSION**: Fades market extremes using RSI + Bollinger Bands
   - Goes LONG when RSI < oversold OR price < lower BB
   - Goes SHORT when RSI > overbought OR price > upper BB

3. **BREAKOUT**: Trades range breaks using N-bar high/low
   - Goes LONG when price breaks above N-bar high by min_pct
   - Goes SHORT when price breaks below N-bar low by min_pct

All positions use TP (take profit) and SL (stop loss) to manage risk.

## OPTIMIZATION GUIDELINES:
1. **SHARPE RATIO FOCUS**: Prioritize consistency over large gains. Reduce volatility of returns.
2. **PROFIT FACTOR FOCUS**: Cut losers faster (tighter SL) or let winners run longer (wider TP).
3. **DRAWDOWN CONTROL**: If drawdown is high, consider reducing position size or tightening stops.
4. Analyze the data carefully - look at per-strategy performance metrics.
5. Consider market regime when optimizing (trending vs ranging, volatile vs calm).
6. If a strategy has Profit Factor < 1.0 consistently, consider disabling it (enabled: false).
7. **IMPORTANT**: Your changes will be constrained to +/-10% per parameter per cycle. 
   Suggest your ideal values - the system will apply gradual changes automatically.
8. If current metrics meet targets (Sharpe >= 1.5, PF >= 1.5, DD <= 10%), suggest NO CHANGES.
9. TP should generally be larger than SL for positive expectancy (risk/reward > 1).
10. In high volatility, consider wider TP/SL; in low volatility, consider tighter levels.

## WALK-FORWARD VALIDATION
You will receive out-of-sample performance metrics for the last 7 days.
Use these to validate that your suggested changes improve RISK-ADJUSTED returns, not just raw P&L.
A strategy with Sharpe 2.0 and $50 P&L is BETTER than one with Sharpe 0.5 and $100 P&L.

OUTPUT FORMAT (strict JSON only, no other text):
```json
{
  "action": "modify",
  "confidence": 0.75,
  "reasoning": "Brief explanation focusing on Sharpe/PF/Drawdown improvements",
  "target_improvements": {
    "sharpe_ratio": "current -> expected",
    "profit_factor": "current -> expected",
    "max_drawdown": "current -> expected"
  },
  "parameters": {
    "global": {
      "tp_pct": 0.01,
      "sl_pct": 0.005,
      "position_size_usd": 100,
      "leverage": 5
    },
    "momentum": {
      "enabled": true,
      "ema_fast": 20,
      "ema_slow": 50,
      "rsi_period": 14,
      "rsi_long_threshold": 55,
      "rsi_short_threshold": 45
    },
    "mean_reversion": {
      "enabled": true,
      "rsi_oversold": 30,
      "rsi_overbought": 70,
      "bb_period": 20,
      "bb_std": 2.0
    },
    "breakout": {
      "enabled": true,
      "lookback_bars": 20,
      "min_breakout_pct": 0.002
    }
  }
}
```

If targets are already met (Sharpe >= 1.5, PF >= 1.5, DD <= 10%):
```json
{
  "action": "no_change",
  "confidence": 0.8,
  "reasoning": "All targets met - Sharpe: X.XX, PF: X.XX, MaxDD: X.X%",
  "parameters": null
}
```

ONLY output the JSON block. No markdown, no explanation outside JSON."""

    def _build_user_prompt(self, context: str, current_params: Dict) -> str:
        """Build user prompt with context."""
        return f"""Analyze the following trading performance data and suggest parameter optimizations.

{context}

Based on this data, provide your optimization recommendation in the specified JSON format.

## OPTIMIZATION PRIORITY (focus on these in order):
1. **Sharpe Ratio >= 1.5**: Is the risk-adjusted return acceptable? If not, how to improve consistency?
2. **Profit Factor >= 1.5**: Are gross profits at least 1.5x gross losses? If not, adjust TP/SL ratio.
3. **Max Drawdown <= 10%**: Is capital protected? If drawdown is high, reduce risk exposure.
4. **Net P&L**: After above targets, maximize absolute returns.

## KEY QUESTIONS:
- What are the current Sharpe Ratio, Profit Factor, and Max Drawdown? Are targets met?
- Which strategies contribute positively vs negatively to risk-adjusted returns?
- What market regime are we in? (trending/ranging, volatile/calm)
- Should any parameters be adjusted to REDUCE VOLATILITY of returns?
- Should TP be widened or SL tightened to improve Profit Factor?
- Should any strategy be disabled due to poor risk-adjusted performance?

## IMPORTANT CONSTRAINTS:
- All parameter changes will be limited to +/-10% per cycle
- Suggest your ideal target values - the system applies gradual changes
- If all targets are already met, recommend NO CHANGE

Provide your recommendation:"""

    def _parse_response(self, content: str) -> Tuple[Optional[Dict], str, float, str]:
        """
        Parse LLM response to extract parameters.

        Returns:
            (params, reasoning, confidence, action)
        """
        try:
            # Clean up response - remove markdown code blocks if present
            content = content.strip()
            if content.startswith("```"):
                # Remove markdown code block
                lines = content.split("\n")
                # Find JSON start and end
                json_lines = []
                in_json = False
                for line in lines:
                    if line.startswith("```") and not in_json:
                        in_json = True
                        continue
                    elif line.startswith("```") and in_json:
                        break
                    elif in_json:
                        json_lines.append(line)
                content = "\n".join(json_lines)

            # Extract JSON from response
            json_start = content.find("{")
            json_end = content.rfind("}") + 1

            if json_start == -1 or json_end == 0:
                return None, "Failed to find JSON in response", 0, "error"

            json_str = content[json_start:json_end]

            # Clean common JSON issues from LLMs
            import re
            # Remove trailing commas before } or ]
            json_str = re.sub(r',(\s*[}\]])', r'\1', json_str)
            # Remove // comments
            json_str = re.sub(r'//.*$', '', json_str, flags=re.MULTILINE)
            # Remove /* */ comments
            json_str = re.sub(r'/\*.*?\*/', '', json_str, flags=re.DOTALL)

            data = json.loads(json_str)

            reasoning = data.get("reasoning", "No reasoning provided")
            confidence = float(data.get("confidence", 0.5))
            action = data.get("action", "no_change")

            if action == "no_change":
                return None, reasoning, confidence, action

            params = data.get("parameters")
            if not params:
                return None, "No parameters in response", 0, "error"

            # Validate and normalize parameter structure
            normalized = self._normalize_params(params)
            if not normalized:
                return None, "Invalid parameter structure", 0, "error"

            return normalized, reasoning, confidence, action

        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error: {e}")
            return None, f"JSON parse error: {e}", 0, "error"

        except Exception as e:
            logger.error(f"Parse error: {e}")
            return None, f"Parse error: {e}", 0, "error"

    def _normalize_params(self, params: Dict) -> Optional[Dict]:
        """
        Validate and normalize parameter structure.

        Returns:
            Normalized params dict or None if invalid
        """
        try:
            # Extract global params (might be nested or flat)
            global_p = params.get("global", params)

            normalized = {
                "tp_pct": float(global_p.get("tp_pct", 0.01)),
                "sl_pct": float(global_p.get("sl_pct", 0.005)),
                "position_size_usd": float(global_p.get("position_size_usd", 100)),
                "leverage": int(global_p.get("leverage", 5)),
            }

            # Momentum
            m = params.get("momentum", {})
            normalized["momentum"] = {
                "enabled": bool(m.get("enabled", True)),
                "ema_fast": int(m.get("ema_fast", 20)),
                "ema_slow": int(m.get("ema_slow", 50)),
                "rsi_period": int(m.get("rsi_period", 14)),
                "rsi_long_threshold": int(m.get("rsi_long_threshold", 55)),
                "rsi_short_threshold": int(m.get("rsi_short_threshold", 45)),
            }

            # Mean Reversion
            mr = params.get("mean_reversion", {})
            normalized["mean_reversion"] = {
                "enabled": bool(mr.get("enabled", True)),
                "rsi_oversold": int(mr.get("rsi_oversold", 30)),
                "rsi_overbought": int(mr.get("rsi_overbought", 70)),
                "bb_period": int(mr.get("bb_period", 20)),
                "bb_std": float(mr.get("bb_std", 2.0)),
            }

            # Breakout
            b = params.get("breakout", {})
            normalized["breakout"] = {
                "enabled": bool(b.get("enabled", True)),
                "lookback_bars": int(b.get("lookback_bars", 20)),
                "min_breakout_pct": float(b.get("min_breakout_pct", 0.002)),
            }

            # Validation
            if normalized["tp_pct"] <= 0 or normalized["sl_pct"] <= 0:
                logger.error("TP/SL must be positive")
                return None

            if normalized["momentum"]["ema_fast"] >= normalized["momentum"]["ema_slow"]:
                logger.error("EMA fast must be < EMA slow")
                return None

            return normalized

        except (ValueError, KeyError, TypeError) as e:
            logger.error(f"Parameter normalization error: {e}")
            return None

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()
