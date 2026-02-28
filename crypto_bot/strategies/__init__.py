"""Trading strategies for HLQuantBot."""

from .base import BaseStrategy, StrategyResult

# Re-export indicator functions
from crypto_bot.services.market_state import (
    calculate_adx,
    calculate_atr,
    calculate_ema,
    calculate_rsi,
    calculate_sma,
    calculate_bollinger_bands,
    calculate_choppiness_index,
)

__all__ = [
    "BaseStrategy",
    "StrategyResult",
    # Indicator functions
    "calculate_adx",
    "calculate_atr",
    "calculate_ema",
    "calculate_rsi",
    "calculate_sma",
    "calculate_bollinger_bands",
    "calculate_choppiness_index",
]
