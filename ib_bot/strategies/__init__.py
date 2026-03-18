"""IB Bot Strategies."""

from .base import BaseStrategy, StrategyResult
from .registry import create_strategy, create_rsi_mean_reversion, create_rsi2_connors
from .rsi2_connors import RSI2ConnorsStrategy
from .options_spreads import CreditSpreadStrategy
from .etf_rotation import ETFRotationStrategy

__all__ = [
    "BaseStrategy",
    "StrategyResult",
    "create_strategy",
    "create_rsi_mean_reversion",
    "create_rsi2_connors",
    "RSI2ConnorsStrategy",
    "CreditSpreadStrategy",
    "ETFRotationStrategy",
]
