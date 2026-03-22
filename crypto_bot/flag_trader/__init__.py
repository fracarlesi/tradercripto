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
from .reward import (
    REWARD_FUNCTIONS,
    compute_calmar_delta,
    compute_sharpe_delta,
    compute_sortino_delta,
)
from .trainer import PPOTrainer, RolloutBuffer
from .autoresearch import AutoResearcher, ExperimentResult, ResearchState
from .walk_forward import WalkForwardResult, WalkForwardValidator, WindowResult

__all__ = [
    "HyperliquidTradingEnv",
    "FlagTraderModel",
    "PPOTrainer",
    "PromptBuilder",
    "HyperliquidDataCollector",
    "RolloutBuffer",
    "compute_sharpe_delta",
    "compute_sortino_delta",
    "compute_calmar_delta",
    "REWARD_FUNCTIONS",
    "WalkForwardValidator",
    "WalkForwardResult",
    "WindowResult",
    "AutoResearcher",
    "ExperimentResult",
    "ResearchState",
]
