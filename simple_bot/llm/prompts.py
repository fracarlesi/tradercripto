"""
LLM Prompt Templates
====================

Prompt templates for DeepSeek strategy selection and market analysis.

Templates use Python format strings with named placeholders:
- {symbol} - Trading symbol
- {market_regime} - Current market regime
- {adx} - ADX indicator value
- etc.

All templates are designed to produce JSON output for structured parsing.
"""

# =============================================================================
# Strategy Selection Prompt
# =============================================================================

STRATEGY_SELECTION_PROMPT = """
You are selecting the optimal trading strategy for {symbol} based on current market conditions.

## Available Strategies

1. **momentum** - Trend-following strategy
   - Works best: Strong trends (ADX > 25), clear direction
   - Avoids: Ranging/sideways markets, choppy conditions
   - Key signals: EMA crossovers, RSI confirmation, volume expansion

2. **mean_reversion** - Counter-trend strategy
   - Works best: Range-bound markets (ADX < 20), overextended moves
   - Avoids: Strong trends, breakout conditions
   - Key signals: RSI extremes (<30 or >70), Bollinger Band touches

3. **breakout** - Volatility expansion strategy
   - Works best: Low volatility compression, clear support/resistance
   - Avoids: Already extended moves, low volume environments
   - Key signals: Price breaking key levels, volume surge, ATR expansion

4. **funding_arb** - Funding rate arbitrage
   - Works best: Extreme funding rates (>0.05% or <-0.05%)
   - Avoids: Low funding rates, volatile conditions
   - Key signals: High absolute funding rate, stable prices

## Current Market Context

- **Symbol**: {symbol}
- **Market Regime**: {market_regime}
- **Trend Strength (ADX)**: {adx}
- **RSI**: {rsi}
- **Volatility Score**: {volatility_score}
- **Volume Score**: {volume_score}
- **Funding Rate**: {funding_rate}%
- **Opportunity Score**: {opportunity_score}

## Recent Strategy Performance (last 24h)

{recent_performance}

## Your Task

Analyze the context and select the most appropriate strategy. Consider:
1. Which strategy aligns with current market conditions?
2. Recent performance - avoid strategies that have been losing
3. Risk factors that could make a strategy fail

Respond with a JSON object:
```json
{{
    "strategy": "momentum|mean_reversion|breakout|funding_arb",
    "confidence": 0.0-1.0,
    "direction": "long|short|neutral",
    "reasoning": "Brief explanation (1-2 sentences)",
    "entry_conditions": ["condition1", "condition2"],
    "risk_factors": ["risk1", "risk2"]
}}
```
"""

# =============================================================================
# Market Analysis Prompt
# =============================================================================

MARKET_ANALYSIS_PROMPT = """
Analyze the current crypto market conditions based on the following data.

## Market Snapshot

- **BTC Price**: ${btc_price:,.2f}
- **BTC 24h Change**: {btc_change_24h:+.2f}%
- **Total Market Volume (24h)**: ${total_volume_24h:,.0f}
- **Average Funding Rate**: {avg_funding_rate:.4f}%

## Top Performers (24h)
{top_gainers}

## Worst Performers (24h)
{top_losers}

## Analysis Required

Provide a structured market analysis covering:

1. **Regime**: What is the overall market state?
   - bullish: Broad uptrend, positive momentum
   - bearish: Broad downtrend, negative momentum
   - neutral: No clear direction, mixed signals
   - volatile: High uncertainty, wide price swings

2. **Trend Strength**: How strong is the current direction? (0.0-1.0)

3. **Risk Level**: Current market risk assessment
   - low: Stable conditions, clear trends
   - medium: Normal volatility, some uncertainty
   - high: Elevated volatility, mixed signals
   - extreme: Very high volatility, potential for large moves

4. **Strategy Recommendations**: Which strategies work best/worst now?

Respond with a JSON object:
```json
{{
    "regime": "bullish|bearish|neutral|volatile",
    "trend_strength": 0.0-1.0,
    "risk_level": "low|medium|high|extreme",
    "summary": "1-2 sentence market summary",
    "recommended_strategies": ["strategy1", "strategy2"],
    "avoid_strategies": ["strategy3"]
}}
```
"""

# =============================================================================
# Optimization Prompt
# =============================================================================

OPTIMIZATION_PROMPT = """
Based on recent trading performance, suggest parameter adjustments for the {strategy} strategy.

## Current Parameters

{current_params}

## Recent Performance (last {hours} hours)

- Total Trades: {total_trades}
- Win Rate: {win_rate:.1f}%
- Net PnL: ${net_pnl:+.2f}
- Average Trade Duration: {avg_duration} minutes
- Max Drawdown: {max_drawdown:.2f}%

## Trade Breakdown

{trade_details}

## Market Conditions During Period

- Average ADX: {avg_adx:.1f}
- Average Volatility: {avg_volatility:.1f}%
- Market Regime: {market_regime}

## Optimization Task

Analyze the performance data and suggest parameter adjustments to improve results.
Consider:
1. Are stop losses too tight or too loose?
2. Are entry thresholds optimal for current market conditions?
3. Is position sizing appropriate given recent volatility?

Respond with a JSON object:
```json
{{
    "adjustments": {{
        "param_name": {{
            "current": current_value,
            "suggested": new_value,
            "reason": "Brief explanation"
        }}
    }},
    "confidence": 0.0-1.0,
    "expected_improvement": "Brief description of expected outcome",
    "risks": ["potential_risk1", "potential_risk2"]
}}
```

Only suggest changes if you're confident they will improve performance.
If current parameters seem optimal, return an empty adjustments object.
"""

# =============================================================================
# Fallback / Quick Decision Prompts
# =============================================================================

QUICK_STRATEGY_PROMPT = """
Quick strategy selection for {symbol}:
- ADX: {adx} (>25 = trending)
- RSI: {rsi} (30-70 = neutral, <30 oversold, >70 overbought)
- Regime: {market_regime}

Select: momentum, mean_reversion, breakout, or funding_arb
Respond with ONLY a JSON: {{"strategy": "...", "confidence": 0.X, "direction": "long|short|neutral"}}
"""

# =============================================================================
# System Messages
# =============================================================================

TRADING_SYSTEM_MESSAGE = """
You are an expert quantitative trading assistant for a crypto trading bot.

Your role:
1. Analyze market data and technical indicators
2. Select optimal trading strategies based on conditions
3. Provide clear, actionable recommendations
4. Identify risks and potential issues

Guidelines:
- Be concise and specific
- Base decisions on data, not speculation
- Acknowledge uncertainty when present
- Always respond in valid JSON when requested
- Consider recent performance data when available

Available strategies: momentum, mean_reversion, breakout, funding_arb
"""

MARKET_ANALYST_SYSTEM_MESSAGE = """
You are an expert crypto market analyst providing high-level market assessments.

Your role:
1. Interpret overall market conditions
2. Identify prevailing trends and regimes
3. Assess risk levels
4. Recommend appropriate trading approaches

Guidelines:
- Focus on the big picture, not individual trades
- Consider multiple factors: price, volume, sentiment
- Be objective about market conditions
- Always respond in valid JSON when requested
"""
