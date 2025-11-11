"""
DeepSeek AI Client - JSON-based trading decisions (REFACTORED ARCHITECTURE)

This module replaces narrative prompts with structured JSON input from the orchestrator.

Key Changes:
- INPUT: Complete MarketDataSnapshot JSON with ALL 142 symbols
- OUTPUT: Trading decision with detailed reasoning
- BENEFITS:
  - DeepSeek sees complete market data (not just top 5)
  - Can learn optimal indicator weights through feedback loop
  - Structured format enables better analysis
  - Reduces prompt engineering complexity

Architecture:
1. Orchestrator builds complete JSON (all symbols, all indicators)
2. DeepSeek client formats JSON for API prompt
3. DeepSeek analyzes ALL data and returns decision
4. Decision includes reasoning showing which indicators influenced choice

Example:
    >>> from services.orchestrator.market_data_orchestrator import build_market_data_snapshot
    >>> snapshot = await build_market_data_snapshot(account_id=1, enable_prophet=True)
    >>> client = DeepSeekClient(account)
    >>> decision = await client.get_trading_decision(snapshot)
    >>> print(f"Operation: {decision['operation']}, Symbol: {decision['symbol']}")
"""

import json
import logging
import random
import time
from typing import Any, Dict, Optional

import requests

from database.models import Account
from services.orchestrator.schemas import MarketDataSnapshot

logger = logging.getLogger(__name__)


class DeepSeekClient:
    """
    Client for DeepSeek AI trading decisions using structured JSON input.

    This client replaces the old narrative prompt system with structured JSON
    from the orchestrator, enabling DeepSeek to analyze ALL 142 symbols with
    complete indicator data.
    """

    def __init__(self, account: Account):
        """
        Initialize DeepSeek client for an account.

        Args:
            account: Account model with API configuration
        """
        self.account = account
        self.api_key = account.api_key
        self.base_url = account.base_url.rstrip("/")
        self.model = account.model

        # API endpoint (OpenAI-compatible)
        self.api_endpoint = f"{self.base_url}/chat/completions"

        # Retry configuration
        self.max_retries = 3
        self.timeout = 300  # 5 minutes for long reasoning chains

        logger.info(f"DeepSeekClient initialized for account {account.name} (model: {self.model})")

    async def get_trading_decision(
        self,
        market_snapshot: MarketDataSnapshot,
    ) -> Optional[Dict[str, Any]]:
        """
        Get trading decision from DeepSeek using complete market data JSON.

        Args:
            market_snapshot: Complete market data snapshot from orchestrator

        Returns:
            Trading decision dict or None if error:
            {
                "operation": str,  # "buy", "sell", "short", "hold"
                "symbol": str,
                "target_portion_of_balance": float,
                "leverage": int,
                "reason": str,
                "analysis": {
                    "indicators_used": List[str],
                    "confidence": float,
                    "alternatives_considered": List[Dict],
                }
            }

        Example:
            >>> decision = await client.get_trading_decision(snapshot)
            >>> if decision and decision["operation"] != "hold":
            ...     execute_trade(decision)
        """
        try:
            # Build prompt with complete JSON
            prompt = self._build_json_prompt(market_snapshot)

            # Count tokens for monitoring
            input_tokens = self._estimate_tokens(prompt)
            logger.info(
                f"DeepSeek prompt: ~{input_tokens} tokens "
                f"({len(market_snapshot['symbols'])} symbols, "
                f"{len(market_snapshot['global_indicators']['whale_alerts'])} whales, "
                f"{len(market_snapshot['global_indicators']['news'])} news)"
            )

            # Prepare API request
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }

            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
                "response_format": {"type": "json_object"},  # Request JSON response
            }

            # Call API with retry logic
            response_data = await self._call_api_with_retry(headers, payload)

            if not response_data:
                return None

            # Extract decision from response
            decision = self._parse_decision(response_data)

            if decision:
                logger.info(
                    f"DeepSeek decision: {decision['operation']} {decision.get('symbol', 'N/A')} "
                    f"(portion: {decision.get('target_portion_of_balance', 0):.2f})"
                )

            return decision

        except Exception as e:
            logger.error(f"Failed to get DeepSeek decision: {e}", exc_info=True)
            return None

    def _build_json_prompt(self, snapshot: MarketDataSnapshot) -> str:
        """
        Build structured prompt with complete JSON market data.

        This is the CORE of the new architecture - DeepSeek receives ALL data
        in structured format, not narrative summaries.

        Args:
            snapshot: Complete market data snapshot

        Returns:
            Formatted prompt string with JSON data and instructions
        """
        # Extract key data for compact summary
        metadata = snapshot["metadata"]
        symbols_count = metadata["symbols_analyzed"]
        portfolio = snapshot["portfolio"]
        sentiment = snapshot["global_indicators"]["sentiment"]
        strategy_weights = portfolio["strategy_weights"]

        # Compact JSON representation (only essential fields for display)
        # Full JSON is included below for analysis
        compact_portfolio = {
            "total_assets": portfolio["total_assets"],
            "available_cash": portfolio["available_cash"],
            "positions_count": len(portfolio["positions"]),
            "positions": [
                {
                    "symbol": pos["symbol"],
                    "side": pos["side"],
                    "quantity": pos["quantity"],
                    "entry_price": pos["entry_price"],
                    "current_price": pos["current_price"],
                    "unrealized_pnl": pos["unrealized_pnl"],
                    "unrealized_pnl_pct": pos["unrealized_pnl_pct"],
                }
                for pos in portfolio["positions"]
            ],
        }

        # Build prompt
        prompt = f"""You are a cryptocurrency trading AI using STRUCTURED JSON data for ALL {symbols_count} symbols.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎯 NEW ARCHITECTURE - COMPLETE MARKET DATA JSON
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**CRITICAL CHANGE**: You now receive COMPLETE data for ALL {symbols_count} symbols, not just top 5.

**Your Task**:
1. Analyze ALL {symbols_count} symbols using the structured JSON below
2. Consider ALL 6 indicators for EACH symbol:
   - Technical Analysis (momentum + support)
   - Pivot Points (support/resistance levels)
   - Prophet Forecast (24h price prediction)
   - Sentiment Index (market fear/greed)
   - Whale Alerts (large transactions)
   - News (crypto headlines)

3. Weight indicators according to strategy weights:
{json.dumps(strategy_weights, indent=3)}

4. Identify the BEST trading opportunity (buy/short) OR manage existing positions (sell/hold)
5. Provide detailed reasoning showing which indicators influenced your decision

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 CURRENT PORTFOLIO
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{json.dumps(compact_portfolio, indent=2)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📈 COMPLETE MARKET DATA JSON ({symbols_count} symbols)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Below is the COMPLETE market data snapshot. Each symbol has:
- price: Current market price
- technical_analysis: Momentum + support scores (0-1), signal (STRONG_BUY/BUY/HOLD/SELL/STRONG_SELL)
- pivot_points: PP, R1-R3, S1-S3, current_zone, signal
- prophet_forecast: 24h price forecast, trend, confidence (if available)
- market_data: Volume, price_change_24h, etc.

**IMPORTANT**: Some symbols may have null prophet_forecast (Prophet only enabled for select symbols).
This is NORMAL - analyze those symbols using the other 5 indicators.

MARKET DATA JSON:
```json
{json.dumps(snapshot, indent=2)}
```

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🧠 TRADING DECISION RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**STEP 1: CHECK EXISTING POSITIONS FIRST (PROFIT/LOSS MANAGEMENT)**

For each position in portfolio:
- If unrealized_pnl_pct > +5% → EVALUATE TAKE PROFIT:
  • Check if technical_analysis.score STILL > 0.7 (momentum continuing?)
  • Check if news sentiment is POSITIVE (catalysts?)
  • If YES → HOLD (let winners run)
  • If NO → SELL (lock in profits)

- If unrealized_pnl_pct < -5% → EVALUATE STOP LOSS:
  • Check if technical_analysis.score < 0.3 (downward momentum?)
  • Check if news sentiment is NEGATIVE (bad news?)
  • If YES → SELL IMMEDIATELY (cut losses)
  • If NO and score > 0.6 → MAY HOLD (recovery possible)

**STEP 2: ANALYZE ALL {symbols_count} SYMBOLS FOR NEW OPPORTUNITIES**

For each symbol, calculate weighted score:
```
weighted_score = (
    technical_analysis.score * {strategy_weights['technical_analysis']:.2f} +
    pivot_points.signal_score * {strategy_weights['pivot_points']:.2f} +
    prophet_forecast.trend_score * {strategy_weights['prophet']:.2f} +
    sentiment.signal_score * {strategy_weights['sentiment']:.2f} +
    whale_alerts.signal_score * {strategy_weights['whale_alerts']:.2f} +
    news.sentiment_score * {strategy_weights['news']:.2f}
)
```

Convert indicator signals to scores:
- Technical: Use score directly (0-1)
- Pivot: bullish_zone=1.0, long_opportunity=0.9, neutral=0.5, bearish_zone=0.0, short_opportunity=0.1
- Prophet: (forecast_24h - current_price) / current_price normalized to 0-1
- Sentiment: (value / 100) normalized with contrarian logic
- Whale: 1.0 if buy signal, 0.0 if sell signal, 0.5 if neutral
- News: 1.0 if positive, 0.5 if neutral, 0.0 if negative

**STEP 3: POSITION SIZING (CRITICAL)**

- If weighted_score >= 0.85 AND technical.momentum >= 0.90 AND news positive:
  → target_portion_of_balance = 1.0 (100% conviction)

- If weighted_score >= 0.60 and < 0.85:
  → target_portion_of_balance = 0.25 (diversify across 4 positions)

- If weighted_score < 0.60:
  → operation = "hold" (signal too weak)

**STEP 4: LEVERAGE (USE INTELLIGENTLY)**

- leverage = 1: Weak signals (score 0.60-0.70)
- leverage = 2-3: Moderate signals (score 0.70-0.80)
- leverage = 4-6: Strong signals (score 0.80-0.90)
- leverage = 7-10: VERY strong signals (score > 0.90) - USE WITH CAUTION

**STEP 5: OUTPUT DECISION**

Return JSON with:
- operation: "buy", "sell", "short", or "hold"
- symbol: Symbol to trade (if not "hold")
- target_portion_of_balance: 0.0-1.0
- leverage: 1-10
- reason: Brief explanation (2-3 sentences)
- analysis: Detailed breakdown showing:
  - indicators_used: List of indicators that influenced decision
  - confidence: 0.0-1.0 confidence score
  - alternatives_considered: Top 3 alternative symbols with their scores

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📋 REQUIRED OUTPUT FORMAT (JSON ONLY)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Respond with ONLY a JSON object (no markdown, no explanations):

{{
  "operation": "buy|sell|short|hold",
  "symbol": "BTC",
  "target_portion_of_balance": 1.0,
  "leverage": 1,
  "reason": "BTC shows strong technical score (0.87) + bullish Prophet forecast (+2.3%) + positive whale activity. Pivot points confirm bullish zone. High conviction trade.",
  "analysis": {{
    "indicators_used": [
      "technical_analysis (score: 0.87, weight: {strategy_weights['technical_analysis']:.2f})",
      "prophet_forecast (trend: up, +2.3%, weight: {strategy_weights['prophet']:.2f})",
      "pivot_points (zone: bullish, weight: {strategy_weights['pivot_points']:.2f})",
      "whale_alerts (signal: buy, weight: {strategy_weights['whale_alerts']:.2f})"
    ],
    "confidence": 0.92,
    "alternatives_considered": [
      {{"symbol": "ETH", "weighted_score": 0.78, "reason": "Good but lower momentum than BTC"}},
      {{"symbol": "SOL", "weighted_score": 0.72, "reason": "Decent but bearish Prophet forecast"}},
      {{"symbol": "AVAX", "weighted_score": 0.68, "reason": "Weak technical score"}}
    ]
  }}
}}

**CRITICAL CONSTRAINTS**:
- Available cash: ${portfolio["available_cash"]:.2f}
- Minimum order size: $10 (system auto-adjusts if allocation < $10)
- Can only sell positions listed in portfolio.positions
- Must choose symbol from the {symbols_count} symbols in market data JSON"""

        return prompt

    async def _call_api_with_retry(
        self, headers: Dict[str, str], payload: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Call DeepSeek API with exponential backoff retry logic.

        Args:
            headers: HTTP headers
            payload: Request payload

        Returns:
            API response JSON or None if failed
        """
        for attempt in range(self.max_retries):
            try:
                response = requests.post(
                    self.api_endpoint,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                    verify=False,  # Disable SSL verification for custom endpoints
                )

                # Success
                if response.status_code == 200:
                    result = response.json()

                    # Log token usage
                    usage = result.get("usage", {})
                    input_tokens = usage.get("prompt_tokens", 0)
                    output_tokens = usage.get("completion_tokens", 0)
                    cost = self._calculate_cost(input_tokens, output_tokens)

                    logger.info(
                        f"DeepSeek API response: "
                        f"input={input_tokens} tokens, "
                        f"output={output_tokens} tokens, "
                        f"cost=${cost:.6f}"
                    )

                    return result

                # Rate limited - retry with backoff
                elif response.status_code == 429:
                    wait_time = (2**attempt) + random.uniform(0, 1)
                    logger.warning(
                        f"DeepSeek API rate limited (attempt {attempt + 1}/{self.max_retries}), "
                        f"waiting {wait_time:.1f}s..."
                    )
                    if attempt < self.max_retries - 1:
                        time.sleep(wait_time)
                        continue
                    else:
                        logger.error(
                            f"DeepSeek API rate limited after {self.max_retries} attempts",
                            exc_info=True,
                        )
                        return None

                # Other errors
                else:
                    logger.error(
                        f"DeepSeek API error (status {response.status_code}): {response.text}",
                        exc_info=True,
                    )
                    return None

            except requests.RequestException as e:
                if attempt < self.max_retries - 1:
                    wait_time = (2**attempt) + random.uniform(0, 1)
                    logger.warning(
                        f"DeepSeek API request failed (attempt {attempt + 1}/{self.max_retries}), "
                        f"retrying in {wait_time:.1f}s: {e}"
                    )
                    time.sleep(wait_time)
                    continue
                else:
                    logger.error(
                        f"DeepSeek API request failed after {self.max_retries} attempts: {e}",
                        exc_info=True,
                    )
                    return None

        return None

    def _parse_decision(self, response_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Parse trading decision from API response.

        Handles:
        - OpenAI-compatible response format
        - JSON extraction from markdown code blocks
        - Response truncation
        - Malformed JSON cleanup

        Args:
            response_data: API response JSON

        Returns:
            Parsed decision dict or None if failed
        """
        try:
            # Extract content from OpenAI-compatible response
            if "choices" not in response_data or len(response_data["choices"]) == 0:
                logger.error(f"Invalid response format: {response_data}")
                return None

            choice = response_data["choices"][0]
            message = choice.get("message", {})
            finish_reason = choice.get("finish_reason", "")

            # Check for truncation
            if finish_reason == "length":
                logger.warning(
                    "DeepSeek response truncated due to token limit. "
                    "Decision may be incomplete."
                )

            # Get content
            text_content = message.get("content", "")
            if not text_content:
                logger.error(f"Empty content in response: {response_data}")
                return None

            # Clean up content (remove markdown code blocks)
            text_content = text_content.strip()
            if "```json" in text_content:
                text_content = text_content.split("```json")[1].split("```")[0].strip()
            elif "```" in text_content:
                text_content = text_content.split("```")[1].split("```")[0].strip()

            # Parse JSON
            try:
                decision = json.loads(text_content)
            except json.JSONDecodeError as e:
                logger.warning(f"Initial JSON parse failed: {e}")
                # Try cleanup
                decision = self._clean_and_parse_json(text_content)
                if not decision:
                    return None

            # Validate structure
            if not isinstance(decision, dict):
                logger.error(f"Decision is not a dict: {type(decision)}")
                return None

            required_fields = ["operation", "symbol", "target_portion_of_balance", "leverage", "reason"]
            missing_fields = [field for field in required_fields if field not in decision]

            if missing_fields:
                logger.error(f"Missing required fields in decision: {missing_fields}")
                return None

            # Validate values
            operation = decision["operation"].lower()
            if operation not in ["buy", "sell", "short", "hold"]:
                logger.error(f"Invalid operation: {operation}")
                return None

            portion = float(decision["target_portion_of_balance"])
            if not (0.0 <= portion <= 1.0):
                logger.error(f"Invalid target_portion_of_balance: {portion}")
                return None

            leverage = int(decision["leverage"])
            if not (1 <= leverage <= 10):
                logger.error(f"Invalid leverage: {leverage}")
                return None

            return decision

        except Exception as e:
            logger.error(f"Failed to parse decision: {e}", exc_info=True)
            return None

    def _clean_and_parse_json(self, text: str) -> Optional[Dict[str, Any]]:
        """
        Attempt to clean malformed JSON and parse.

        Args:
            text: Raw JSON text

        Returns:
            Parsed dict or None
        """
        try:
            # Replace problematic characters
            cleaned = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
            cleaned = cleaned.replace('"', '"').replace('"', '"')
            cleaned = cleaned.replace(""", "'").replace(""", "'")
            cleaned = cleaned.replace("–", "-").replace("—", "-").replace("‑", "-")

            # Try parsing cleaned version
            decision = json.loads(cleaned)
            logger.info("Successfully parsed JSON after cleanup")
            return decision

        except json.JSONDecodeError:
            logger.error("JSON cleanup failed - unable to parse decision", exc_info=True)
            return None

    def _estimate_tokens(self, text: str) -> int:
        """
        Estimate token count (1 token ≈ 4 characters).

        Args:
            text: Text to estimate

        Returns:
            Estimated token count
        """
        return len(text) // 4

    def _calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """
        Calculate API call cost in USD.

        DeepSeek pricing (as of 2025):
        - Input: $0.14 per 1M tokens
        - Output: $0.28 per 1M tokens

        Args:
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens

        Returns:
            Cost in USD
        """
        input_cost_per_million = 0.14
        output_cost_per_million = 0.28

        input_cost = (input_tokens / 1_000_000) * input_cost_per_million
        output_cost = (output_tokens / 1_000_000) * output_cost_per_million

        return input_cost + output_cost


async def get_trading_decision_from_snapshot(
    account: Account,
    market_snapshot: MarketDataSnapshot,
) -> Optional[Dict[str, Any]]:
    """
    Convenience function to get trading decision from market snapshot.

    This is the NEW API that replaces call_ai_for_decision() in the old system.

    Args:
        account: Account with AI configuration
        market_snapshot: Complete market data from orchestrator

    Returns:
        Trading decision dict or None

    Example:
        >>> from services.orchestrator.market_data_orchestrator import build_market_data_snapshot
        >>> snapshot = await build_market_data_snapshot(account_id=1)
        >>> decision = await get_trading_decision_from_snapshot(account, snapshot)
        >>> if decision:
        ...     execute_trade(decision)
    """
    client = DeepSeekClient(account)
    return await client.get_trading_decision(market_snapshot)
