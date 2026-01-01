"""Trading strategies for HLQuantBot conservative refactor."""

from .base import BaseStrategy, StrategyResult
from .trend_follow import TrendFollowStrategy
from .mean_reversion import MeanReversionStrategy

# Re-export indicator functions for backward compatibility
# These are used by opportunity_ranker and other legacy services
from simple_bot.services.market_state import (
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
    "TrendFollowStrategy",
    "MeanReversionStrategy",
    # Indicator functions
    "calculate_adx",
    "calculate_atr",
    "calculate_ema",
    "calculate_rsi",
    "calculate_sma",
    "calculate_bollinger_bands",
    "calculate_choppiness_index",
]
