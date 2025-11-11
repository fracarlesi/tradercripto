"""
AI Services Module - DeepSeek trading decision engine.

This module provides the NEW JSON-based architecture for AI trading decisions.

Components:
- deepseek_client: Main client for trading decisions using structured JSON

Usage:
    from services.ai import get_trading_decision_from_snapshot
    from services.orchestrator import build_market_data_snapshot

    # Get complete market data
    snapshot = await build_market_data_snapshot(account_id=1)

    # Get AI decision
    decision = await get_trading_decision_from_snapshot(account, snapshot)
"""

from .deepseek_client import (
    DeepSeekClient,
    get_trading_decision_from_snapshot,
)

__all__ = [
    "DeepSeekClient",
    "get_trading_decision_from_snapshot",
]
