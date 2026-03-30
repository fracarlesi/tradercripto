"""
LLM Equity Strategy — FLAG-Trader Based Stock Selection
=========================================================

Uses the equity FLAG-Trader model to evaluate scan candidates
and produce TradeSetup objects for the execution engine.

Does NOT extend BaseStrategy (which is ORB-centric). Instead,
operates on scan results with batch evaluation.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from ..core.enums import Direction, SetupType
from ..core.models import TradeSetup
from ..flag_trader.agent import IBFlagTraderAgent, TradeDecision

logger = logging.getLogger(__name__)


class LLMEquityStrategy:
    """LLM-based equity strategy using FLAG-Trader model.

    Evaluates scan candidates via the LLM agent and converts
    TradeDecision objects into TradeSetup models for execution.

    This is NOT a BaseStrategy subclass — it operates on scanner
    output (batch candidates) rather than real-time ORB ranges.
    """

    name: str = "llm_equity"

    def __init__(
        self,
        agent: IBFlagTraderAgent,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.agent = agent
        self.config = config or {}
        self._logger = logging.getLogger("ib_bot.strategy.llm_equity")

    async def evaluate_candidates(
        self,
        scan_results: list[dict[str, Any]],
        candle_cache: dict[str, list[dict[str, float]]],
        portfolio: dict[str, float] | None = None,
    ) -> list[TradeSetup]:
        """Evaluate scanner candidates and return trade setups.

        Args:
            scan_results: List of dicts from scanner with at least 'symbol'.
                         May include 'sector', 'last_price', etc.
            candle_cache: Dict mapping symbol -> list of candle dicts.
            portfolio: Current portfolio state.

        Returns:
            List of TradeSetup Pydantic models ready for risk sizing.
        """
        decisions = await self.agent.evaluate_candidates(
            candidates=scan_results,
            candle_cache=candle_cache,
            portfolio=portfolio,
        )

        setups: list[TradeSetup] = []
        for decision in decisions:
            # Get last price from candle cache
            candles = candle_cache.get(decision.symbol, [])
            if not candles:
                continue

            last_price = Decimal(str(candles[-1]["close"]))
            setup = self._decision_to_setup(decision, last_price)
            if setup is not None:
                setups.append(setup)

        self._logger.info(
            "LLM equity: %d decisions -> %d trade setups",
            len(decisions),
            len(setups),
        )
        return setups

    def _decision_to_setup(
        self,
        decision: TradeDecision,
        last_price: Decimal,
    ) -> TradeSetup | None:
        """Convert a TradeDecision to a TradeSetup Pydantic model.

        Calculates entry, stop, and target prices from model TP/SL percentages.
        """
        if decision.action == 2:  # BUY
            direction = Direction.LONG
            setup_type = SetupType.LLM_EQUITY_LONG
            entry_price = last_price
            stop_price = last_price * (1 - Decimal(str(decision.sl_pct)) / 100)
            target_price = last_price * (1 + Decimal(str(decision.tp_pct)) / 100)
        elif decision.action == 0:  # SELL
            direction = Direction.SHORT
            setup_type = SetupType.LLM_EQUITY_SHORT
            entry_price = last_price
            stop_price = last_price * (1 + Decimal(str(decision.sl_pct)) / 100)
            target_price = last_price * (1 - Decimal(str(decision.tp_pct)) / 100)
        else:
            return None

        return TradeSetup(
            symbol=decision.symbol,
            asset_class="equity",
            direction=direction,
            setup_type=setup_type,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            confidence=Decimal(str(round(abs(decision.confidence), 4))),
            source="llm_equity",
        )
