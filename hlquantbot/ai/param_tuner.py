"""Strategy parameter tuning using GPT."""

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

from openai import AsyncOpenAI

from ..core.models import StrategyMetrics, ClosedTrade, MarketContext
from ..core.enums import StrategyId
from ..config.settings import Settings
from .prompts import PARAM_TUNING_SYSTEM_PROMPT, PARAM_TUNING_USER_TEMPLATE


logger = logging.getLogger(__name__)


@dataclass
class ParameterSuggestion:
    """Suggested parameter change."""
    parameter: str
    current_value: Any
    suggested_value: Any
    change_pct: float
    reasoning: str


@dataclass
class TuningResult:
    """Result of parameter tuning analysis."""
    strategy_id: StrategyId
    suggestions: List[ParameterSuggestion]
    confidence: Decimal
    expected_impact: str
    reasoning: str
    timestamp: datetime


class ParameterTuner:
    """
    Suggests parameter adjustments based on strategy performance.

    Runs periodically (daily/weekly) to optimize strategy parameters
    based on recent performance data.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.config = settings.openai

        self._client: Optional[AsyncOpenAI] = None
        self._last_tuning: Dict[StrategyId, TuningResult] = {}
        self._tuning_interval = timedelta(hours=self.config.param_tuning_interval_hours)

    @property
    def is_enabled(self) -> bool:
        return self.config.enabled and bool(self.config.api_key)

    async def _get_client(self) -> AsyncOpenAI:
        """Get or create OpenAI/DeepSeek client."""
        if self._client is None:
            base_url = getattr(self.config, 'base_url', None)
            if base_url:
                self._client = AsyncOpenAI(
                    api_key=self.config.api_key,
                    base_url=base_url
                )
            else:
                self._client = AsyncOpenAI(api_key=self.config.api_key)
        return self._client

    def should_tune(self, strategy_id: StrategyId) -> bool:
        """Check if strategy should be tuned."""
        if strategy_id not in self._last_tuning:
            return True

        last = self._last_tuning[strategy_id]
        return datetime.now(timezone.utc) - last.timestamp > self._tuning_interval

    async def tune_strategy(
        self,
        strategy_id: StrategyId,
        metrics: StrategyMetrics,
        recent_trades: List[ClosedTrade],
        current_params: Dict[str, Any],
        market_conditions: Optional[str] = None,
    ) -> Optional[TuningResult]:
        """
        Analyze strategy performance and suggest parameter adjustments.

        Args:
            strategy_id: Strategy to tune
            metrics: Recent performance metrics
            recent_trades: List of recent closed trades
            current_params: Current strategy parameters
            market_conditions: Optional description of market conditions

        Returns:
            TuningResult with suggestions, or None if tuning not needed
        """
        if not self.is_enabled:
            return None

        if not self.should_tune(strategy_id):
            return self._last_tuning.get(strategy_id)

        try:
            result = await self._run_tuning(
                strategy_id, metrics, recent_trades, current_params, market_conditions
            )
            self._last_tuning[strategy_id] = result
            return result

        except Exception as e:
            logger.error(f"Parameter tuning failed for {strategy_id.value}: {e}")
            return None

    async def _run_tuning(
        self,
        strategy_id: StrategyId,
        metrics: StrategyMetrics,
        recent_trades: List[ClosedTrade],
        current_params: Dict[str, Any],
        market_conditions: Optional[str],
    ) -> TuningResult:
        """Run GPT analysis for parameter tuning."""
        client = await self._get_client()

        # Format current parameters
        params_str = "\n".join(f"- {k}: {v}" for k, v in current_params.items())

        # Format recent trades
        trades_lines = []
        for trade in recent_trades[-10:]:  # Last 10 trades
            trades_lines.append(
                f"- {trade.symbol} {trade.side.value}: "
                f"P&L ${trade.pnl:.2f} ({trade.pnl_pct:.2%}), "
                f"Duration: {trade.duration_seconds // 60}min, "
                f"Exit: {trade.exit_reason.value}"
            )
        trades_str = "\n".join(trades_lines) if trades_lines else "No recent trades"

        # Build user prompt
        user_prompt = PARAM_TUNING_USER_TEMPLATE.format(
            strategy_id=strategy_id.value,
            current_params=params_str,
            total_trades=metrics.total_trades,
            win_rate=float(metrics.win_rate),
            profit_factor=float(metrics.profit_factor),
            avg_win=float(metrics.avg_win),
            avg_loss=float(metrics.avg_loss),
            max_drawdown=float(metrics.max_drawdown),
            sharpe_ratio=float(metrics.sharpe_ratio or 0),
            recent_trades=trades_str,
            market_conditions=market_conditions or "No specific conditions noted",
        )

        # Call GPT
        response = await client.chat.completions.create(
            model=self.config.model,
            messages=[
                {"role": "system", "content": PARAM_TUNING_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            response_format={"type": "json_object"},
        )

        # Parse response
        content = response.choices[0].message.content
        result = json.loads(content)

        # Parse suggestions
        suggestions = []
        for sugg in result.get("suggestions", []):
            suggestions.append(ParameterSuggestion(
                parameter=sugg.get("parameter", ""),
                current_value=sugg.get("current_value"),
                suggested_value=sugg.get("suggested_value"),
                change_pct=sugg.get("change_pct", 0),
                reasoning=sugg.get("reasoning", ""),
            ))

        tuning_result = TuningResult(
            strategy_id=strategy_id,
            suggestions=suggestions,
            confidence=Decimal(str(result.get("confidence", 0.5))),
            expected_impact=result.get("expected_impact", ""),
            reasoning=result.get("reasoning", ""),
            timestamp=datetime.now(timezone.utc),
        )

        # Log suggestions
        if suggestions:
            logger.info(
                f"Parameter tuning for {strategy_id.value}: "
                f"{len(suggestions)} suggestions (confidence: {tuning_result.confidence:.2f})"
            )
            for s in suggestions:
                logger.info(
                    f"  - {s.parameter}: {s.current_value} -> {s.suggested_value} "
                    f"({s.change_pct:+.1f}%)"
                )
        else:
            logger.info(f"Parameter tuning for {strategy_id.value}: No changes suggested")

        return tuning_result

    async def apply_suggestions(
        self,
        tuning_result: TuningResult,
        min_confidence: Decimal = Decimal("0.7"),
        max_change_pct: float = 20.0,
    ) -> Dict[str, Any]:
        """
        Apply tuning suggestions that meet criteria.

        Args:
            tuning_result: Tuning result with suggestions
            min_confidence: Minimum confidence to apply changes
            max_change_pct: Maximum percentage change to apply

        Returns:
            Dictionary of parameter changes to apply
        """
        changes = {}

        if tuning_result.confidence < min_confidence:
            logger.info(
                f"Skipping tuning for {tuning_result.strategy_id.value}: "
                f"confidence {tuning_result.confidence:.2f} < {min_confidence:.2f}"
            )
            return changes

        for suggestion in tuning_result.suggestions:
            if abs(suggestion.change_pct) > max_change_pct:
                logger.info(
                    f"Skipping {suggestion.parameter}: "
                    f"change {suggestion.change_pct:.1f}% > max {max_change_pct:.1f}%"
                )
                continue

            changes[suggestion.parameter] = suggestion.suggested_value
            logger.info(
                f"Applying change: {suggestion.parameter} = {suggestion.suggested_value}"
            )

        return changes

    def get_last_tuning(self, strategy_id: StrategyId) -> Optional[TuningResult]:
        """Get last tuning result for a strategy."""
        return self._last_tuning.get(strategy_id)
