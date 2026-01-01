"""
HLQuantBot LLM Module
=====================

DeepSeek API integration for intelligent strategy selection.

Exports:
    - DeepSeekClient: Async client for DeepSeek chat API
    - StrategyDecision: Pydantic model for LLM strategy decisions
    - MarketAnalysis: Pydantic model for market analysis results
    - STRATEGY_SELECTION_PROMPT: Prompt template for strategy selection
    - MARKET_ANALYSIS_PROMPT: Prompt template for market analysis
    - OPTIMIZATION_PROMPT: Prompt template for parameter optimization

Example:
    from simple_bot.llm import DeepSeekClient, StrategyDecision
    
    client = DeepSeekClient()
    decision = await client.select_strategy({
        "symbol": "ETH",
        "market_regime": "bullish",
        "adx": 35.5,
        "rsi": 62.3,
        ...
    })
    print(f"Strategy: {decision.strategy}, Confidence: {decision.confidence}")
"""

from .client import (
    DeepSeekClient,
    StrategyDecision,
    MarketAnalysis,
)

from .prompts import (
    STRATEGY_SELECTION_PROMPT,
    MARKET_ANALYSIS_PROMPT,
    OPTIMIZATION_PROMPT,
)

__all__ = [
    # Client
    "DeepSeekClient",
    "StrategyDecision",
    "MarketAnalysis",
    # Prompts
    "STRATEGY_SELECTION_PROMPT",
    "MARKET_ANALYSIS_PROMPT",
    "OPTIMIZATION_PROMPT",
]

__version__ = "2.0.0"
