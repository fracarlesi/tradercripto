"""
FLAG-Trader Package
===================

LLM-based trading agent using PPO/RL, inspired by FLAG-Trader paper.
Components: Gymnasium environment, prompt builder, data collector, reward function.
"""

from .data_collector import HyperliquidDataCollector
from .environment import HyperliquidTradingEnv
from .model import FlagTraderModel
from .prompt import PromptBuilder
from .reward import compute_sharpe_delta
from .trainer import PPOTrainer, RolloutBuffer

__all__ = [
    "HyperliquidTradingEnv",
    "FlagTraderModel",
    "PPOTrainer",
    "PromptBuilder",
    "HyperliquidDataCollector",
    "RolloutBuffer",
    "compute_sharpe_delta",
]
