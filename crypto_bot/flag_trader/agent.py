"""
FLAG-Trader Live Trading Agent
===============================

Integrates the trained FLAG-Trader model into the live trading pipeline.
Scans assets, builds prompts from candle data, and returns trade decisions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from .model import FlagTraderModel
from .prompt import PromptBuilder

logger = logging.getLogger(__name__)

# Action mapping: model output -> human-readable
ACTION_NAMES = {0: "SELL", 1: "HOLD", 2: "BUY"}


@dataclass
class TradeDecision:
    """A single trade decision from the FLAG-Trader model."""

    symbol: str
    action: int  # 0=Sell, 1=Hold, 2=Buy
    action_name: str
    confidence: float  # state value from value head
    log_prob: float


@dataclass
class FlagTraderConfig:
    """Configuration for the FLAG-Trader agent."""

    model_name: str = "deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B"
    checkpoint_path: str = "models/flag_trader_deepseek/final_model.pt"
    device: str = "cpu"
    scan_interval_seconds: int = 300
    max_assets_to_scan: int = 10
    candle_window: int = 20
    candle_interval: str = "15m"
    confidence_threshold: float = 0.6

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FlagTraderConfig:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class FlagTraderAgent:
    """Live trading agent using FLAG-Trader model.

    Scans assets, fetches candles, builds prompts, and gets model decisions.
    """

    def __init__(
        self,
        config: FlagTraderConfig,
        model: FlagTraderModel,
        prompt_builder: PromptBuilder,
    ) -> None:
        self.config = config
        self.model = model
        self.prompt_builder = prompt_builder
        self._trade_history: list[str] = []  # recent action names for prompt context

    async def scan_and_decide(
        self,
        assets: list[str],
        candle_fetcher: Any,
        portfolio: Optional[dict[str, float]] = None,
    ) -> list[TradeDecision]:
        """Scan assets and return trade decisions.

        Args:
            assets: List of asset symbols to scan.
            candle_fetcher: Object with async get_candles(symbol, interval, limit) method.
            portfolio: Current portfolio state for prompt context.

        Returns:
            List of TradeDecision for actionable signals (not Hold, above threshold).
        """
        if portfolio is None:
            portfolio = {"cash_balance": 0.0, "asset_position": 0.0, "total_account_value": 0.0}

        # Limit scan to top N assets
        scan_assets = assets[: self.config.max_assets_to_scan]
        decisions: list[TradeDecision] = []

        for symbol in scan_assets:
            try:
                decision = await self._evaluate_asset(symbol, candle_fetcher, portfolio)
                if decision is not None:
                    decisions.append(decision)
            except Exception as e:
                logger.warning("Error evaluating %s: %s", symbol, e)

        # Sort by absolute confidence descending
        decisions.sort(key=lambda d: abs(d.confidence), reverse=True)

        logger.info(
            "FLAG-Trader scan: %d assets, %d actionable decisions",
            len(scan_assets),
            len(decisions),
        )
        return decisions

    async def _evaluate_asset(
        self,
        symbol: str,
        candle_fetcher: Any,
        portfolio: dict[str, float],
    ) -> Optional[TradeDecision]:
        """Evaluate a single asset with the model.

        Returns TradeDecision if action is Buy/Sell and above threshold, else None.
        """
        candles_raw = await candle_fetcher.get_candles(
            symbol,
            interval=self.config.candle_interval,
            limit=self.config.candle_window + 5,  # extra buffer
        )

        if not candles_raw or len(candles_raw) < self.config.candle_window:
            logger.debug("Insufficient candle data for %s: %d candles", symbol, len(candles_raw) if candles_raw else 0)
            return None

        candles = self._candles_to_prompt_format(candles_raw)
        history = {
            "recent_rewards": [],
            "net_values": [],
            "actions": self._trade_history[-10:],
        }

        prompt = self.prompt_builder.build_prompt(candles, portfolio, history)
        action_id, state_value, log_prob = self.model.get_action(prompt)
        action_name = ACTION_NAMES.get(action_id, "HOLD")

        logger.info(
            "FLAG-Trader | %s | action=%s | value=%.4f | log_prob=%.4f",
            symbol, action_name, state_value, float(log_prob),
        )

        # Only return actionable decisions (Buy/Sell) above confidence threshold
        if action_id == 1:  # Hold
            return None

        if abs(state_value) < self.config.confidence_threshold:
            logger.debug(
                "SKIP %s: confidence %.4f < threshold %.4f",
                symbol, abs(state_value), self.config.confidence_threshold,
            )
            return None

        return TradeDecision(
            symbol=symbol,
            action=action_id,
            action_name=action_name,
            confidence=state_value,
            log_prob=float(log_prob),
        )

    @staticmethod
    def _candles_to_prompt_format(candles_raw: list[dict]) -> list[dict[str, float]]:
        """Convert raw candle dicts from HyperliquidClient to PromptBuilder format.

        Input format (from API): {t, o, h, l, c, v}
        Output format (for PromptBuilder): {open, high, low, close, volume}
        """
        result: list[dict[str, float]] = []
        for c in candles_raw:
            result.append({
                "open": float(c.get("o", c.get("open", 0))),
                "high": float(c.get("h", c.get("high", 0))),
                "low": float(c.get("l", c.get("low", 0))),
                "close": float(c.get("c", c.get("close", 0))),
                "volume": float(c.get("v", c.get("volume", 0))),
            })
        return result

    def record_action(self, action_name: str) -> None:
        """Record a trade action for history context in future prompts."""
        self._trade_history.append(action_name)
        if len(self._trade_history) > 50:
            self._trade_history = self._trade_history[-50:]
