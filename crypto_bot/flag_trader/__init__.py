"""
FLAG-Trader Package
===================

LLM-based trading agent using PPO/RL, inspired by FLAG-Trader paper.
Components: Gymnasium environment, prompt builder, data collector, reward function.
"""

from .agent import FlagTraderAgent, FlagTraderConfig, TradeDecision
from .data_collector import HyperliquidDataCollector
from .trade_logger import FlagTradeLogger, TradeRecord
from .trade_memory_rag import TradeMemoryRAG
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
    "FlagTraderAgent",
    "FlagTraderConfig",
    "TradeDecision",
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
    "FlagTradeLogger",
    "TradeRecord",
    "TradeMemoryRAG",
]
