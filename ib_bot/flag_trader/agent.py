"""
IB FLAG-Trader Agent — Equity LLM Trading Agent
=================================================

Adapts FlagTraderAgent from crypto_bot for IB equity trading.
Key differences:
- Evaluates scan candidates (batch, not streaming)
- Evaluates existing positions for exit
- No candle_fetcher dependency (candles passed directly from cache)
- Returns TradeDecision / ExitDecision dataclasses
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .equity_model import EquityFlagTraderModel
from .equity_prompt import EquityPromptBuilder

logger = logging.getLogger(__name__)

# Action mapping: model output -> human-readable
ACTION_NAMES = {0: "SELL", 1: "HOLD", 2: "BUY"}


@dataclass
class TradeDecision:
    """A single trade decision from the equity FLAG-Trader model."""

    symbol: str
    action: int  # 0=Sell, 1=Hold, 2=Buy
    action_name: str
    confidence: float  # state value from value head
    log_prob: float = 0.0
    tp_pct: float = 3.0  # model-predicted TP% (equity range 1-8%)
    sl_pct: float = 2.0  # model-predicted SL% (equity range 0.5-4%)


@dataclass
class ExitDecision:
    """Decision on whether to close an existing position."""

    symbol: str
    should_close: bool
    action_name: str  # what the model said (BUY/SELL/HOLD)
    confidence: float
    reason: str  # "model_reversal", "hold", "insufficient_data"


@dataclass
class IBFlagTraderConfig:
    """Configuration for the IB FLAG-Trader agent."""

    model_name: str = "Qwen/Qwen2.5-0.5B-Instruct"
    checkpoint_path: str = "models/equity_flag_trader/final_model.pt"
    device: str = "cpu"
    confidence_threshold: float = 0.6
    candle_window: int = 20

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IBFlagTraderConfig:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class IBFlagTraderAgent:
    """IB equity trading agent using FLAG-Trader model.

    Evaluates scan candidates and existing positions using the LLM model.
    Designed for batch evaluation (EOD scan -> morning decisions).
    """

    def __init__(
        self,
        config: IBFlagTraderConfig,
        model: EquityFlagTraderModel,
        prompt_builder: EquityPromptBuilder,
    ) -> None:
        self.config = config
        self.model = model
        self.prompt_builder = prompt_builder
        self._trade_history: list[str] = []

    async def evaluate_candidates(
        self,
        candidates: list[dict[str, Any]],
        candle_cache: dict[str, list[dict[str, float]]],
        portfolio: dict[str, float] | None = None,
    ) -> list[TradeDecision]:
        """Evaluate scan candidates and return trade decisions.

        Args:
            candidates: List of dicts with at least 'symbol' key.
                        May include 'sector' for GICS sector context.
            candle_cache: Dict mapping symbol -> list of candle dicts
                         (keys: open, high, low, close, volume).
            portfolio: Current portfolio state for prompt context.

        Returns:
            List of actionable TradeDecision (Buy/Sell, above confidence threshold),
            sorted by absolute confidence descending.
        """
        if portfolio is None:
            portfolio = {
                "cash_balance": 0.0,
                "asset_position": 0.0,
                "total_account_value": 0.0,
            }

        decisions: list[TradeDecision] = []

        for candidate in candidates:
            symbol = candidate["symbol"]
            sector = candidate.get("sector")
            candles = candle_cache.get(symbol)

            if not candles or len(candles) < self.config.candle_window:
                logger.debug(
                    "Insufficient candle data for %s: %d candles",
                    symbol,
                    len(candles) if candles else 0,
                )
                continue

            try:
                decision = self._evaluate_single(
                    symbol=symbol,
                    candles=candles,
                    portfolio=portfolio,
                    sector=sector,
                )
                if decision is not None:
                    decisions.append(decision)
            except Exception as e:
                logger.warning("Error evaluating candidate %s: %s", symbol, e)

        # Sort by absolute confidence descending
        decisions.sort(key=lambda d: abs(d.confidence), reverse=True)

        logger.info(
            "IB FLAG-Trader: evaluated %d candidates, %d actionable decisions",
            len(candidates),
            len(decisions),
        )
        return decisions

    async def evaluate_position(
        self,
        symbol: str,
        direction: str,  # "long" or "short"
        entry_price: float,
        pnl_pct: float,
        candles: list[dict[str, float]],
        portfolio: dict[str, float] | None = None,
    ) -> ExitDecision:
        """Evaluate whether an open position should be closed.

        Uses the LLM to decide: if model says opposite direction
        (LONG+SELL or SHORT+BUY), the position should be closed.

        Args:
            symbol: Ticker symbol.
            direction: "long" or "short".
            entry_price: Position entry price.
            pnl_pct: Current unrealized PnL percentage.
            candles: Recent candle data for the symbol.
            portfolio: Current portfolio state.

        Returns:
            ExitDecision with should_close flag and reason.
        """
        if portfolio is None:
            portfolio = {
                "cash_balance": 0.0,
                "asset_position": 0.0,
                "total_account_value": 0.0,
            }

        if not candles or len(candles) < self.config.candle_window:
            logger.debug("Insufficient candle data for position eval %s", symbol)
            return ExitDecision(
                symbol=symbol,
                should_close=False,
                action_name="HOLD",
                confidence=0.0,
                reason="insufficient_data",
            )

        history = {
            "recent_rewards": [],
            "net_values": [],
            "actions": self._trade_history[-10:],
        }

        position_info = {
            "direction": direction,
            "symbol": symbol,
            "entry_price": entry_price,
            "pnl_pct": pnl_pct,
        }

        prompt = self.prompt_builder.build_prompt(
            candles=candles,
            portfolio=portfolio,
            history=history,
            position_info=position_info,
        )

        logger.info(
            "IB FLAG-Trader POSITION | %s | direction=%s | entry=%.2f | pnl=%.1f%%",
            symbol,
            direction,
            entry_price,
            pnl_pct,
        )

        start = time.monotonic()
        action_id, state_value, log_prob, _tp, _sl = self.model.get_action(prompt)
        elapsed = time.monotonic() - start
        action_name = ACTION_NAMES.get(action_id, "HOLD")

        # Determine if model wants to reverse position
        should_close = False
        reason = "hold"
        if direction == "long" and action_id == 0:  # LONG + SELL
            should_close = True
            reason = "model_reversal"
        elif direction == "short" and action_id == 2:  # SHORT + BUY
            should_close = True
            reason = "model_reversal"

        logger.info(
            "IB FLAG-Trader EXIT | %s %s | model=%s | value=%.4f | close=%s | time=%.1fs",
            direction.upper(),
            symbol,
            action_name,
            state_value,
            should_close,
            elapsed,
        )

        self._trade_history.append(f"EXIT_EVAL_{action_name}")
        if len(self._trade_history) > 50:
            self._trade_history = self._trade_history[-50:]

        return ExitDecision(
            symbol=symbol,
            should_close=should_close,
            action_name=action_name,
            confidence=state_value,
            reason=reason,
        )

    def _evaluate_single(
        self,
        symbol: str,
        candles: list[dict[str, float]],
        portfolio: dict[str, float],
        sector: str | None = None,
    ) -> TradeDecision | None:
        """Evaluate a single candidate synchronously.

        Returns TradeDecision if actionable (Buy/Sell above threshold), else None.
        """
        history = {
            "recent_rewards": [],
            "net_values": [],
            "actions": self._trade_history[-10:],
        }

        prompt = self.prompt_builder.build_prompt(
            candles=candles,
            portfolio=portfolio,
            history=history,
            sector=sector,
        )

        logger.debug("IB FLAG-Trader PROMPT | %s | %s", symbol, prompt[:300])

        start = time.monotonic()
        action_id, state_value, log_prob, tp_pct, sl_pct = self.model.get_action(prompt)
        elapsed = time.monotonic() - start
        action_name = ACTION_NAMES.get(action_id, "HOLD")

        logger.info(
            "IB FLAG-Trader | %s | action=%s | value=%.4f | tp=%.1f%% | sl=%.1f%% | time=%.1fs",
            symbol,
            action_name,
            state_value,
            tp_pct,
            sl_pct,
            elapsed,
        )

        # Only return actionable decisions (Buy/Sell)
        if action_id == 1:  # Hold
            return None

        if abs(state_value) < self.config.confidence_threshold:
            logger.debug(
                "SKIP %s: confidence %.4f < threshold %.4f",
                symbol,
                abs(state_value),
                self.config.confidence_threshold,
            )
            return None

        # Record action
        self._trade_history.append(action_name)
        if len(self._trade_history) > 50:
            self._trade_history = self._trade_history[-50:]

        return TradeDecision(
            symbol=symbol,
            action=action_id,
            action_name=action_name,
            confidence=state_value,
            log_prob=float(log_prob),
            tp_pct=tp_pct,
            sl_pct=sl_pct,
        )

    def record_action(self, action_name: str) -> None:
        """Record a trade action for history context in future prompts."""
        self._trade_history.append(action_name)
        if len(self._trade_history) > 50:
            self._trade_history = self._trade_history[-50:]
