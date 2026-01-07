"""
HLQuantBot LLM Veto Service
============================

LLM as trade filter, NOT decision maker.

Role:
- Receive Setups from strategy
- Ask LLM to ALLOW or DENY
- Pass approved setups to RiskManager
- Log decisions for accuracy tracking

Important:
- LLM does NOT choose strategies
- LLM does NOT set parameters
- Fallback behavior if LLM unavailable: ALLOW (rules already filtered)

Author: Francesco Carlesi
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, date, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .base import BaseService
from .message_bus import MessageBus, Message
from ..core.enums import Topic
from ..core.models import Setup, MarketState, LLMDecision, Regime


logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class LLMVetoConfig:
    """LLM veto configuration."""

    enabled: bool = True
    provider: str = "deepseek"
    model: str = "deepseek-chat"
    api_key_env: str = "DEEPSEEK_API_KEY"

    # Rate limiting
    max_calls_per_day: int = 6  # 1 per 4h window
    timeout_seconds: int = 30

    # Fallback behavior
    fallback_on_error: str = "allow"  # "allow" or "deny"
    fallback_on_chaos: str = "deny"   # Always deny in CHAOS

    # Decision thresholds
    min_confidence: float = 0.6


# =============================================================================
# LLM Veto Service
# =============================================================================

class LLMVetoService(BaseService):
    """
    LLM veto service for trade filtering.

    Subscribes to: Strategy signals (internal)
    Publishes to: Topic.SETUPS (approved setups)

    The LLM receives:
    - MarketState with indicators
    - Proposed Setup
    - Asks: ALLOW or DENY with confidence and reason
    """

    def __init__(
        self,
        name: str = "llm_veto",
        bus: Optional[MessageBus] = None,
        db: Optional[Any] = None,
        config: Optional[LLMVetoConfig] = None,
    ) -> None:
        """Initialize LLMVetoService."""
        super().__init__(
            name=name,
            bus=bus,
            db=db,
            loop_interval_seconds=60,
        )

        self._config = config or LLMVetoConfig()

        # API client
        self._api_key: Optional[str] = None
        self._client: Optional[Any] = None

        # Rate limiting
        self._calls_today: int = 0
        self._last_reset_date: Optional[date] = None

        # Decision history
        self._decisions: List[LLMDecision] = []

        # Market state cache
        self._market_states: Dict[str, MarketState] = {}

        self._logger.info(
            "LLMVetoService initialized: enabled=%s, provider=%s, max_calls=%d/day",
            self._config.enabled,
            self._config.provider,
            self._config.max_calls_per_day,
        )

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def _on_start(self) -> None:
        """Initialize LLM client."""
        self._logger.info("Starting LLMVetoService...")

        if not self._config.enabled:
            self._logger.info("LLM veto disabled")
            return

        # Get API key
        self._api_key = os.getenv(self._config.api_key_env)
        if not self._api_key:
            self._logger.warning(
                "API key not found (%s), LLM veto will use fallback",
                self._config.api_key_env,
            )
            return

        # Initialize client based on provider
        await self._init_client()

        # Subscribe to market state for context
        if self.bus:
            await self.subscribe(Topic.MARKET_STATE, self._handle_market_state)

        self._last_reset_date = date.today()
        self._logger.info("LLM client initialized")

    async def _init_client(self) -> None:
        """Initialize the LLM client."""
        try:
            if self._config.provider == "deepseek":
                from openai import AsyncOpenAI
                self._client = AsyncOpenAI(
                    api_key=self._api_key,
                    base_url="https://api.deepseek.com",
                )
            else:
                self._logger.warning("Unknown provider: %s", self._config.provider)
        except ImportError:
            self._logger.warning("openai package not installed, LLM disabled")
        except Exception as e:
            self._logger.error("Failed to init LLM client: %s", e)

    async def _on_stop(self) -> None:
        """Cleanup."""
        self._logger.info("Stopping LLMVetoService...")
        self._client = None

    async def _run_iteration(self) -> None:
        """Check for rate limit reset."""
        today = date.today()
        if self._last_reset_date != today:
            self._calls_today = 0
            self._last_reset_date = today
            self._logger.info("Daily rate limit reset")

    async def _health_check_impl(self) -> bool:
        """Check service health."""
        if not self._config.enabled:
            return True
        return self._client is not None or self._api_key is None

    # =========================================================================
    # Market State Handling
    # =========================================================================

    async def _handle_market_state(self, message: Message) -> None:
        """Cache market state for context."""
        try:
            payload = message.payload
            if not isinstance(payload, dict):
                self._logger.warning("Invalid market state payload type: %s", type(payload))
                return
            state = MarketState(**payload)
            self._market_states[state.symbol] = state
        except Exception as e:
            self._logger.error("Error parsing market state: %s", e)

    # =========================================================================
    # Veto Logic
    # =========================================================================

    async def evaluate_setup(self, setup: Setup) -> tuple[bool, LLMDecision]:
        """
        Evaluate a setup using LLM.

        Args:
            setup: Trade setup to evaluate

        Returns:
            Tuple of (approved: bool, decision: LLMDecision)
        """
        # Check if LLM enabled
        if not self._config.enabled:
            decision = self._create_fallback_decision(setup, "LLM disabled")
            return True, decision

        # Check CHAOS regime
        if setup.regime == Regime.CHAOS:
            decision = self._create_fallback_decision(
                setup,
                "CHAOS regime - automatic deny",
                allow=False,
            )
            return False, decision

        # Check rate limit
        if self._calls_today >= self._config.max_calls_per_day:
            decision = self._create_fallback_decision(setup, "Rate limit exceeded")
            return self._config.fallback_on_error == "allow", decision

        # Check client
        if not self._client:
            decision = self._create_fallback_decision(setup, "LLM client unavailable")
            return self._config.fallback_on_error == "allow", decision

        # Call LLM
        try:
            decision = await self._call_llm(setup)
            self._calls_today += 1
            self._decisions.append(decision)

            # Check decision
            approved = (
                decision.decision == "ALLOW" and
                float(decision.confidence) >= self._config.min_confidence
            )

            self._logger.info(
                "LLM decision: %s %s (confidence: %.2f) - %s",
                decision.decision,
                setup.symbol,
                float(decision.confidence),
                decision.reason[:50],
            )

            return approved, decision

        except asyncio.TimeoutError:
            self._logger.warning("LLM call timeout")
            decision = self._create_fallback_decision(setup, "LLM timeout")
            return self._config.fallback_on_error == "allow", decision

        except Exception as e:
            self._logger.error("LLM call error: %s", e)
            decision = self._create_fallback_decision(setup, f"LLM error: {e}")
            return self._config.fallback_on_error == "allow", decision

    async def _call_llm(self, setup: Setup) -> LLMDecision:
        """Call LLM API for veto decision."""
        # Get market state for context
        market_state = self._market_states.get(setup.symbol)

        # Build prompt
        prompt = self._build_prompt(setup, market_state)

        # Call API
        response = await asyncio.wait_for(
            self._client.chat.completions.create(
                model=self._config.model,
                messages=[
                    {"role": "system", "content": self._get_system_prompt()},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,  # Low temperature for consistent decisions
                max_tokens=200,
            ),
            timeout=self._config.timeout_seconds,
        )

        # Parse response
        return self._parse_response(setup, response)

    def _get_system_prompt(self) -> str:
        """Get system prompt for LLM."""
        return """You are a conservative trading risk filter. Your job is to ALLOW or DENY trade setups.

RULES:
1. Only respond with valid JSON in the exact format specified
2. Be conservative - when in doubt, DENY
3. Consider regime, indicators, and risk
4. Do NOT suggest alternatives or modifications
5. Do NOT explain trading strategies

Response format (EXACT):
{"decision": "ALLOW", "confidence": 0.75, "reason": "Clear trend with strong ADX confirmation"}
or
{"decision": "DENY", "confidence": 0.85, "reason": "RSI overbought in weakening trend"}"""

    def _build_prompt(self, setup: Setup, state: Optional[MarketState]) -> str:
        """Build prompt for LLM."""
        prompt_parts = [
            f"Evaluate this trade setup:",
            f"",
            f"Asset: {setup.symbol}",
            f"Setup Type: {setup.setup_type.value}",
            f"Direction: {setup.direction.value}",
            f"Regime: {setup.regime.value}",
            f"Entry Price: {setup.entry_price}",
            f"Stop Price: {setup.stop_price} ({setup.stop_distance_pct:.2f}%)",
            f"",
            f"Indicators:",
            f"- ADX: {setup.adx}",
            f"- RSI: {setup.rsi}",
            f"- ATR: {setup.atr}",
        ]

        if state:
            prompt_parts.extend([
                f"- EMA50: {state.ema50}",
                f"- EMA200: {state.ema200}",
                f"- EMA200 Slope: {state.ema200_slope}",
            ])
            if state.choppiness:
                prompt_parts.append(f"- Choppiness: {state.choppiness}")

        prompt_parts.extend([
            f"",
            f"Strategy Confidence: {setup.confidence}",
            f"Setup Quality: {setup.setup_quality}",
            f"",
            f"Should this trade be ALLOWED or DENIED?",
        ])

        return "\n".join(prompt_parts)

    def _parse_response(self, setup: Setup, response: Any) -> LLMDecision:
        """Parse LLM response into decision."""
        try:
            content = response.choices[0].message.content.strip()

            # Try to extract JSON
            # Handle cases where LLM adds extra text
            json_start = content.find("{")
            json_end = content.rfind("}") + 1

            if json_start >= 0 and json_end > json_start:
                json_str = content[json_start:json_end]
                data = json.loads(json_str)

                decision = data.get("decision", "DENY").upper()
                if decision not in ("ALLOW", "DENY"):
                    decision = "DENY"

                confidence = float(data.get("confidence", 0.5))
                confidence = max(0.0, min(1.0, confidence))

                reason = data.get("reason", "No reason provided")

                return LLMDecision(
                    setup_id=setup.id,
                    timestamp=datetime.now(timezone.utc),
                    decision=decision,
                    confidence=Decimal(str(confidence)),
                    reason=reason,
                    symbol=setup.symbol,
                    regime=setup.regime,
                    setup_type=setup.setup_type,
                )

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            self._logger.warning("Failed to parse LLM response: %s", e)

        # Fallback on parse error
        return self._create_fallback_decision(setup, "Failed to parse LLM response")

    def _create_fallback_decision(
        self,
        setup: Setup,
        reason: str,
        allow: bool = None,
    ) -> LLMDecision:
        """Create fallback decision when LLM unavailable."""
        if allow is None:
            allow = self._config.fallback_on_error == "allow"

        return LLMDecision(
            setup_id=setup.id,
            timestamp=datetime.now(timezone.utc),
            decision="ALLOW" if allow else "DENY",
            confidence=Decimal("0.5"),
            reason=f"Fallback: {reason}",
            symbol=setup.symbol,
            regime=setup.regime,
            setup_type=setup.setup_type,
        )

    # =========================================================================
    # Public API
    # =========================================================================

    def get_calls_remaining(self) -> int:
        """Get remaining LLM calls for today."""
        return max(0, self._config.max_calls_per_day - self._calls_today)

    def get_decision_history(self) -> List[LLMDecision]:
        """Get recent decision history."""
        return self._decisions[-50:]  # Last 50 decisions

    def get_accuracy_stats(self) -> Dict[str, Any]:
        """Get accuracy statistics for LLM decisions."""
        if not self._decisions:
            return {"total": 0}

        allow_count = sum(1 for d in self._decisions if d.decision == "ALLOW")
        deny_count = sum(1 for d in self._decisions if d.decision == "DENY")

        # Calculate accuracy if outcomes tracked
        outcomes_tracked = [d for d in self._decisions if d.was_correct is not None]
        accuracy = None
        if outcomes_tracked:
            correct = sum(1 for d in outcomes_tracked if d.was_correct)
            accuracy = correct / len(outcomes_tracked)

        return {
            "total": len(self._decisions),
            "allow_count": allow_count,
            "deny_count": deny_count,
            "allow_rate": allow_count / len(self._decisions) if self._decisions else 0,
            "accuracy": accuracy,
            "outcomes_tracked": len(outcomes_tracked),
        }

    @property
    def metrics(self) -> Dict[str, Any]:
        """Get service metrics."""
        return {
            "enabled": self._config.enabled,
            "provider": self._config.provider,
            "calls_today": self._calls_today,
            "calls_remaining": self.get_calls_remaining(),
            "decisions_count": len(self._decisions),
            "client_ready": self._client is not None,
            **self.get_accuracy_stats(),
        }


# =============================================================================
# Factory
# =============================================================================

def create_llm_veto(
    bus: Optional[MessageBus] = None,
    db: Optional[Any] = None,
    config: Optional[LLMVetoConfig] = None,
) -> LLMVetoService:
    """Factory function to create LLMVetoService."""
    return LLMVetoService(
        name="llm_veto",
        bus=bus,
        db=db,
        config=config,
    )
