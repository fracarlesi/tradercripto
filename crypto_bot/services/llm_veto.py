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
from .trade_memory import get_trade_memory, TradeMemory
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
    max_calls_per_day: int = 50000
    timeout_seconds: int = 30

    # Fallback behavior
    fallback_on_error: str = "deny"   # Fail-safe: deny if LLM unavailable
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
        config: Optional[LLMVetoConfig] = None,
    ) -> None:
        """Initialize LLMVetoService."""
        super().__init__(
            name=name,
            bus=bus,
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

        # Trade memory for learning
        self._trade_memory: TradeMemory = get_trade_memory()

        # Alert throttling: only send one ntfy alert per failure type per hour
        self._last_alert_time: Dict[str, datetime] = {}
        self._alert_cooldown_minutes: int = 60

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
    # Alert Notifications
    # =========================================================================

    async def _send_llm_alert(self, alert_type: str, message: str) -> None:
        """Send ntfy alert when LLM is non-functional. Throttled: 1 per type per hour."""
        now = datetime.now(timezone.utc)

        # Throttle: skip if same alert type sent recently
        last_sent = self._last_alert_time.get(alert_type)
        if last_sent and (now - last_sent).total_seconds() < self._alert_cooldown_minutes * 60:
            return

        self._last_alert_time[alert_type] = now

        if self.bus:
            await self.bus.publish(Topic.RISK_ALERTS, {
                "alert_type": "llm_failure",
                "failure_reason": alert_type,
                "message": message,
                "calls_today": self._calls_today,
                "max_calls": self._config.max_calls_per_day,
            })

        self._logger.warning("LLM ALERT sent: %s - %s", alert_type, message)

    # =========================================================================
    # Veto Logic
    # =========================================================================

    async def evaluate_setup(self, setup: Setup) -> tuple[bool, LLMDecision]:
        """
        Evaluate a setup using LLM.

        When LLM veto is disabled (config.enabled=False), all setups are
        automatically approved. This allows the strategy signals to pass
        directly to the risk manager without LLM filtering.

        Args:
            setup: Trade setup to evaluate

        Returns:
            Tuple of (approved: bool, decision: LLMDecision)
            When disabled: always returns (True, fallback_decision)
        """
        # Check if LLM enabled - if disabled, approve all setups
        # This is the main bypass for simplicity when LLM is not needed
        if not self._config.enabled:
            decision = self._create_fallback_decision(setup, "LLM disabled - trade approved")
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
            await self._send_llm_alert("rate_limit", f"LLM rate limit reached ({self._config.max_calls_per_day}/day). All trades DENIED until reset.")
            return self._config.fallback_on_error == "allow", decision

        # Check client
        if not self._client:
            decision = self._create_fallback_decision(setup, "LLM client unavailable")
            await self._send_llm_alert("client_unavailable", "LLM client not initialized. Check API key. All trades DENIED.")
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
            await self._send_llm_alert("timeout", "DeepSeek API timeout. Trades DENIED until LLM responds.")
            return self._config.fallback_on_error == "allow", decision

        except Exception as e:
            self._logger.error("LLM call error: %s", e)
            decision = self._create_fallback_decision(setup, f"LLM error: {e}")
            await self._send_llm_alert("api_error", f"DeepSeek API error: {e}. Trades DENIED.")
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
        return """You are a trading filter for an EMA crossover momentum bot. Your job is to block clearly bad trades while letting valid setups through.

IMPORTANT CONTEXT: This bot uses EMA9/EMA21 crossover (a LAGGING indicator). The crossover confirms the trend AFTER price has moved. A single candle in the opposite direction is NORMAL noise — it does NOT invalidate a confirmed crossover.

DIRECTION CHECK:
- If the EMA crossover is confirmed AND ADX > 25 → trust the trend direction, even if the last 1-2 candles pulled back
- DENY only if price action shows a CLEAR REVERSAL (multiple candles, strong momentum shift), not just a single candle

DENY when you see 2+ of these red flags:
- RSI > 62 for LONG or RSI < 42 for SHORT (near extremes for this setup)
- ADX < 22 (weak trend, likely to chop)
- EMA spread is tiny (<0.05%) suggesting the crossover is marginal
- Recent losses on same symbol (3+ consecutive losses)
- Volume suspiciously low (<0.5x avg) suggesting fakeout

ALLOW when:
- EMA crossover is confirmed (this is the primary signal — respect it)
- ADX > 25 (confirmed trend strength)
- RSI not at extremes
- No obvious red flags from the list above

Volume context (IMPORTANT — check the volume_ratio value carefully):
- High volume (>1.2x avg) increases confidence
- Normal volume (0.7-1.2x avg) is fine
- Low volume (0.5-0.7x avg) is a yellow flag — count it as a red flag
- Very low volume (<0.5x avg) DENY — this is likely a fakeout on thin volume

Default stance: If the crossover is confirmed and ADX > 25, ALLOW unless there are clear reasons not to.

Response format (EXACT JSON, nothing else):
{"decision": "ALLOW", "confidence": 0.75, "reason": "Short explanation"}
{"decision": "DENY", "confidence": 0.85, "reason": "Short explanation"}"""

    def _build_prompt(self, setup: Setup, state: Optional[MarketState]) -> str:
        """Build prompt for LLM with price action and historical context."""
        # Get historical context from trade memory
        history_context = self._trade_memory.get_llm_context(
            symbol=setup.symbol,
            regime=setup.regime.value if setup.regime else "unknown",
        )

        prompt_parts = [
            history_context,
            "=== CURRENT SETUP ===",
            "",
            f"Asset: {setup.symbol}",
            f"Direction: {setup.direction.value.upper()}",
            f"Regime: {setup.regime.value}",
            f"Entry Price: {setup.entry_price}",
            f"Stop Price: {setup.stop_price} ({setup.stop_distance_pct:.2f}%)",
        ]

        # Price action context (critical for direction validation)
        if state:
            prompt_parts.append("")
            prompt_parts.append("=== PRICE ACTION (critical) ===")

            # Current vs previous candle
            if state.prev_close and state.close:
                candle_chg = (float(state.close) - float(state.prev_close)) / float(state.prev_close) * 100
                direction_word = "UP" if candle_chg > 0 else "DOWN"
                prompt_parts.append(
                    f"Last candle: {direction_word} {abs(candle_chg):.3f}% "
                    f"(prev close: {state.prev_close} → current: {state.close})"
                )

            # EMA9/EMA21 (the actual signal EMAs)
            if state.ema9 and state.ema21:
                ema_spread = (float(state.ema9) - float(state.ema21)) / float(state.ema21) * 100
                ema_signal = "BULLISH (EMA9 > EMA21)" if ema_spread > 0 else "BEARISH (EMA9 < EMA21)"
                prompt_parts.append(f"EMA Signal: {ema_signal}, spread: {ema_spread:.3f}%")

            # Price vs EMAs
            if state.ema9:
                price_vs_ema9 = (float(state.close) - float(state.ema9)) / float(state.ema9) * 100
                prompt_parts.append(f"Price vs EMA9: {price_vs_ema9:+.3f}%")

            # Volume context
            if state.volume_ratio is not None:
                vol_ratio_f = float(state.volume_ratio)
                vol_label = "above" if vol_ratio_f >= 1.0 else "below"
                prompt_parts.append("")
                prompt_parts.append("=== VOLUME ===")
                prompt_parts.append(
                    f"Volume ratio: {vol_ratio_f:.1f}x average ({vol_label} 20-period avg)"
                )
                if vol_ratio_f >= 1.5:
                    prompt_parts.append("Volume: ELEVATED - strong participation")
                elif vol_ratio_f <= 0.5:
                    prompt_parts.append("Volume: LOW - weak participation, possible fakeout")

        # Indicators
        prompt_parts.extend([
            "",
            "=== INDICATORS ===",
            f"ADX: {setup.adx}",
            f"RSI: {setup.rsi}",
            f"ATR: {setup.atr}",
        ])

        if state:
            if state.choppiness:
                prompt_parts.append(f"Choppiness: {state.choppiness}")
            prompt_parts.append(f"EMA200 Slope: {state.ema200_slope}")

        # Recent losses on same symbol (critical context)
        recent_symbol_trades = [
            t for t in self._trade_memory._closed_trades[-20:]
            if t.symbol == setup.symbol and t.is_winner is not None
        ]
        if recent_symbol_trades:
            recent_results = [
                f"{'WIN' if t.is_winner else 'LOSS'} (${t.pnl:+.2f})"
                for t in recent_symbol_trades[-5:]
            ]
            losses = sum(1 for t in recent_symbol_trades[-5:] if not t.is_winner)
            prompt_parts.extend([
                "",
                f"=== RECENT {setup.symbol} TRADES (last {len(recent_results)}) ===",
                f"Results: {', '.join(recent_results)}",
                f"Recent losses: {losses}/{len(recent_results)}",
            ])

        prompt_parts.extend([
            "",
            f"Should this {setup.direction.value.upper()} trade be ALLOWED or DENIED?",
            "The EMA crossover is the primary signal. Evaluate the overall setup quality.",
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
            "trade_memory_size": self._trade_memory.total_trades,
            **self.get_accuracy_stats(),
        }

    # =========================================================================
    # Trade Memory Integration
    # =========================================================================

    def record_trade_entry(
        self,
        trade_id: str,
        setup: Setup,
        decision: LLMDecision,
        position_size: float,
    ) -> None:
        """
        Record a trade entry in memory for learning.

        Call this when a trade is actually opened after LLM approval.
        """
        state = self._market_states.get(setup.symbol)

        self._trade_memory.record_entry(
            trade_id=trade_id,
            symbol=setup.symbol,
            direction=setup.direction.value,
            regime=setup.regime.value,
            entry_price=float(setup.entry_price),
            position_size=position_size,
            adx=float(setup.adx) if setup.adx else 0.0,
            rsi=float(setup.rsi) if setup.rsi else 50.0,
            atr=float(setup.atr) if setup.atr else 0.0,
            atr_pct=float(setup.stop_distance_pct) if setup.stop_distance_pct else 0.0,
            llm_decision=decision.decision,
            llm_confidence=float(decision.confidence),
            llm_reason=decision.reason,
            strategy=setup.setup_type.value if setup.setup_type else "unknown",
            setup_type=setup.setup_type.value if setup.setup_type else "breakout",
            ema50=float(state.ema50) if state and state.ema50 else None,
            ema200=float(state.ema200) if state and state.ema200 else None,
        )

        self._logger.info(
            "Recorded trade entry in memory: %s %s %s",
            trade_id, setup.symbol, setup.direction.value
        )

    def record_trade_outcome(
        self,
        trade_id: str,
        exit_price: float,
        pnl: float,
        pnl_pct: float,
        duration_minutes: float,
        exit_reason: str = "unknown",
    ) -> None:
        """
        Record trade outcome in memory for learning.

        Call this when a trade is closed.
        """
        self._trade_memory.record_outcome(
            trade_id=trade_id,
            exit_price=exit_price,
            pnl=pnl,
            pnl_pct=pnl_pct,
            duration_minutes=duration_minutes,
            exit_reason=exit_reason,
        )

        self._logger.info(
            "Recorded trade outcome in memory: %s P&L=$%.2f",
            trade_id, pnl
        )

    async def save_memory(self) -> None:
        """Persist trade memory to disk."""
        await self._trade_memory.save()

    def get_memory_summary(self) -> Dict[str, Any]:
        """Get trade memory summary for monitoring."""
        return self._trade_memory.get_summary()


# =============================================================================
# Factory
# =============================================================================

def create_llm_veto(
    bus: Optional[MessageBus] = None,
    config: Optional[LLMVetoConfig] = None,
) -> LLMVetoService:
    """Factory function to create LLMVetoService."""
    return LLMVetoService(
        name="llm_veto",
        bus=bus,
        config=config,
    )
