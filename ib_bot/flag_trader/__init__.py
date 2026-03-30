"""
FLAG-Trader for IB Bot — Equity LLM Trading Agent
===================================================

Adapts FLAG-Trader (LLM + PPO heads) from crypto_bot for US equity trading.
Components: equity model wrapper, equity prompt builder, IB trading agent.
"""

from .agent import IBFlagTraderAgent, IBFlagTraderConfig, TradeDecision, ExitDecision
from .equity_model import EquityFlagTraderModel
from .equity_prompt import EquityPromptBuilder

__all__ = [
    "IBFlagTraderAgent",
    "IBFlagTraderConfig",
    "TradeDecision",
    "ExitDecision",
    "EquityFlagTraderModel",
    "EquityPromptBuilder",
]
