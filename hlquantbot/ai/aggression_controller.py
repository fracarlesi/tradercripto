"""AI Aggression Controller for dynamic HFT parameter tuning."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional, Any
import json

from ..core.enums import MarketRegime, StrategyId
from ..config.settings import Settings


logger = logging.getLogger(__name__)


class AggressionLevel(str, Enum):
    """Trading aggression levels."""
    CONSERVATIVE = "conservative"  # 0.5x normal parameters
    NORMAL = "normal"              # 1.0x (default config)
    AGGRESSIVE = "aggressive"      # 1.5x parameters
    VERY_AGGRESSIVE = "very_aggressive"  # 2.0x parameters
    PAUSED = "paused"              # No trading


@dataclass
class AggressionState:
    """Current aggression state for the bot."""
    level: AggressionLevel = AggressionLevel.NORMAL
    multiplier: Decimal = Decimal("1.0")
    reason: str = ""
    updated_at: Optional[datetime] = None
    valid_until: Optional[datetime] = None

    # Per-strategy overrides
    strategy_overrides: Dict[str, AggressionLevel] = field(default_factory=dict)

    # Metrics that influenced the decision
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MarketConditions:
    """Aggregated market conditions for aggression decision."""
    regime: MarketRegime = MarketRegime.UNCERTAIN
    volatility_percentile: float = 50.0  # 0-100
    trend_strength: float = 0.0  # -1 to 1
    volume_ratio: float = 1.0  # vs average
    spread_percentile: float = 50.0  # 0-100 (tighter = lower)
    funding_rate: float = 0.0
    recent_pnl_pct: float = 0.0  # Recent P&L
    win_rate: float = 0.5  # Recent win rate


# Aggression level multipliers
AGGRESSION_MULTIPLIERS = {
    AggressionLevel.PAUSED: Decimal("0"),
    AggressionLevel.CONSERVATIVE: Decimal("0.5"),
    AggressionLevel.NORMAL: Decimal("1.0"),
    AggressionLevel.AGGRESSIVE: Decimal("1.5"),
    AggressionLevel.VERY_AGGRESSIVE: Decimal("2.0"),
}


class AggressionController:
    """
    AI-powered aggression controller for HFT trading.

    Dynamically adjusts trading parameters based on:
    - Market regime (trend, range, volatility)
    - Recent performance (win rate, P&L)
    - Market conditions (spread, volume, liquidity)
    - Time of day / session

    Can use DeepSeek V3.2-Speciale for regime detection and parameter recommendations,
    or operate in rule-based mode without AI.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.openai_config = getattr(settings, 'openai', None)

        # State
        self._state = AggressionState()
        self._conditions = MarketConditions()

        # History for decision making
        self._regime_history: List[tuple] = []  # (timestamp, regime)
        self._pnl_history: List[tuple] = []  # (timestamp, pnl_pct)

        # AI client (optional)
        self._openai_client = None
        if self.openai_config and getattr(self.openai_config, 'enabled', False):
            self._init_openai()

        # Update interval
        self._update_interval_minutes = 5
        if self.openai_config:
            self._update_interval_minutes = getattr(
                self.openai_config, 'regime_detection_interval_minutes', 5
            )

        self._last_update: Optional[datetime] = None

    def _init_openai(self):
        """Initialize OpenAI/DeepSeek client."""
        try:
            import openai
            api_key = getattr(self.settings, 'openai_api_key', None)
            base_url = getattr(self.openai_config, 'base_url', None) if self.openai_config else None
            if api_key:
                if base_url:
                    self._openai_client = openai.OpenAI(api_key=api_key, base_url=base_url)
                else:
                    self._openai_client = openai.OpenAI(api_key=api_key)
                logger.info("DeepSeek client initialized for aggression control")
        except ImportError:
            logger.warning("OpenAI package not installed, using rule-based mode")
        except Exception as e:
            logger.error(f"Failed to initialize DeepSeek: {e}")

    @property
    def current_state(self) -> AggressionState:
        """Get current aggression state."""
        return self._state

    @property
    def current_multiplier(self) -> Decimal:
        """Get current aggression multiplier."""
        return self._state.multiplier

    @property
    def is_paused(self) -> bool:
        """Check if trading is paused by aggression controller."""
        return self._state.level == AggressionLevel.PAUSED

    def get_strategy_multiplier(self, strategy_id: StrategyId) -> Decimal:
        """Get aggression multiplier for a specific strategy."""
        override = self._state.strategy_overrides.get(strategy_id.value)
        if override:
            return AGGRESSION_MULTIPLIERS.get(override, Decimal("1.0"))
        return self._state.multiplier

    async def update(
        self,
        regime: MarketRegime,
        recent_pnl_pct: float,
        win_rate: float,
        volatility_percentile: float = 50.0,
        spread_percentile: float = 50.0,
        volume_ratio: float = 1.0,
    ):
        """
        Update aggression state based on current conditions.

        Called periodically (e.g., every 5 minutes).
        """
        now = datetime.now(timezone.utc)

        # Check if update is needed
        if self._last_update:
            elapsed = (now - self._last_update).total_seconds() / 60
            if elapsed < self._update_interval_minutes:
                return

        # Update conditions
        self._conditions = MarketConditions(
            regime=regime,
            volatility_percentile=volatility_percentile,
            spread_percentile=spread_percentile,
            volume_ratio=volume_ratio,
            recent_pnl_pct=recent_pnl_pct,
            win_rate=win_rate,
        )

        # Update history
        self._regime_history.append((now, regime))
        self._pnl_history.append((now, recent_pnl_pct))

        # Clean old history (keep last hour)
        cutoff = now - timedelta(hours=1)
        self._regime_history = [(ts, r) for ts, r in self._regime_history if ts >= cutoff]
        self._pnl_history = [(ts, p) for ts, p in self._pnl_history if ts >= cutoff]

        # Determine new aggression level
        if self._openai_client:
            new_level = await self._determine_level_ai()
        else:
            new_level = self._determine_level_rules()

        # Update state
        old_level = self._state.level
        self._state.level = new_level
        self._state.multiplier = AGGRESSION_MULTIPLIERS.get(new_level, Decimal("1.0"))
        self._state.updated_at = now
        self._state.valid_until = now + timedelta(minutes=self._update_interval_minutes * 2)
        self._state.metrics = {
            "regime": regime.value,
            "volatility_percentile": volatility_percentile,
            "spread_percentile": spread_percentile,
            "volume_ratio": volume_ratio,
            "recent_pnl_pct": recent_pnl_pct,
            "win_rate": win_rate,
        }

        self._last_update = now

        if old_level != new_level:
            logger.info(
                f"Aggression level changed: {old_level.value} -> {new_level.value} "
                f"(multiplier: {self._state.multiplier})"
            )

    def _determine_level_rules(self) -> AggressionLevel:
        """
        Determine aggression level using enhanced rule-based logic.

        Updated for aggressive P&L targeting with performance-based scaling.
        """
        conditions = self._conditions

        # Rule 1: PAUSE if daily loss > 2% (was 5%)
        if conditions.recent_pnl_pct < -0.02:
            self._state.reason = "Daily loss > 2%, pausing trading"
            return AggressionLevel.PAUSED

        # Rule 2: CONSERVATIVE if losing or low win rate
        if conditions.recent_pnl_pct < -0.01 or conditions.win_rate < 0.45:
            self._state.reason = "Losing streak, reducing aggression"
            return AggressionLevel.CONSERVATIVE

        # Rule 3: CONSERVATIVE in extreme volatility (but higher threshold)
        if conditions.volatility_percentile > 90:
            self._state.reason = "Extreme volatility environment"
            return AggressionLevel.CONSERVATIVE

        # Rule 4: AGGRESSIVE if performing well (lower thresholds)
        if conditions.win_rate > 0.55 and conditions.recent_pnl_pct > 0.01:
            self._state.reason = "Good performance, increasing aggression"

            # Rule 5: VERY_AGGRESSIVE if exceptional performance
            if (conditions.win_rate > 0.60 and
                conditions.recent_pnl_pct > 0.02 and
                conditions.volatility_percentile < 70):
                self._state.reason = "Exceptional performance, maximum aggression"
                return AggressionLevel.VERY_AGGRESSIVE

            return AggressionLevel.AGGRESSIVE

        # Rule 6: Regime-specific strategy overrides
        self._apply_regime_overrides(conditions)

        # Default: Normal
        self._state.reason = "Normal market conditions"
        return AggressionLevel.NORMAL

    def _apply_regime_overrides(self, conditions: MarketConditions):
        """Apply regime-specific strategy overrides."""
        # Clear previous overrides
        self._state.strategy_overrides.clear()

        if conditions.regime == MarketRegime.RANGE_BOUND:
            # MMR-HFT works well in range-bound
            self._state.strategy_overrides["mmr_hft"] = AggressionLevel.AGGRESSIVE
            # Momentum scalping doesn't work well in range-bound
            self._state.strategy_overrides["momentum_scalping"] = AggressionLevel.CONSERVATIVE

        elif conditions.regime in (MarketRegime.TREND_UP, MarketRegime.TREND_DOWN):
            # Reduce mean reversion in trends
            self._state.strategy_overrides["mmr_hft"] = AggressionLevel.CONSERVATIVE
            # Momentum scalping thrives in trends
            self._state.strategy_overrides["momentum_scalping"] = AggressionLevel.AGGRESSIVE
            # Liquidation sniping can work well in strong trends
            self._state.strategy_overrides["liquidation_sniping"] = AggressionLevel.AGGRESSIVE

        elif conditions.regime == MarketRegime.HIGH_VOLATILITY:
            # Most strategies should be conservative in high vol
            self._state.strategy_overrides["mmr_hft"] = AggressionLevel.CONSERVATIVE
            self._state.strategy_overrides["micro_breakout"] = AggressionLevel.CONSERVATIVE
            # But liquidation sniping can catch bounces
            self._state.strategy_overrides["liquidation_sniping"] = AggressionLevel.NORMAL

        elif conditions.regime == MarketRegime.LOW_VOLATILITY:
            # Breakout strategies wait for expansion
            self._state.strategy_overrides["micro_breakout"] = AggressionLevel.AGGRESSIVE
            # MMR works well in low vol
            self._state.strategy_overrides["mmr_hft"] = AggressionLevel.AGGRESSIVE

    async def _determine_level_ai(self) -> AggressionLevel:
        """Determine aggression level using AI (DeepSeek)."""
        try:
            prompt = self._build_ai_prompt()

            response = self._openai_client.chat.completions.create(
                model=getattr(self.openai_config, 'model', 'deepseek-reasoner'),
                messages=[
                    {"role": "system", "content": self._get_system_prompt()},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=500,
                temperature=0.3,
            )

            result = response.choices[0].message.content
            return self._parse_ai_response(result)

        except Exception as e:
            logger.error(f"AI aggression determination failed: {e}")
            # Fallback to rules
            return self._determine_level_rules()

    def _get_system_prompt(self) -> str:
        """Get system prompt for AI."""
        return """You are an AI trading aggression controller for an HFT crypto trading bot.
Your job is to analyze market conditions and recommend an aggression level.

Aggression levels:
- PAUSED: Stop trading entirely (use for extreme conditions or major losses)
- CONSERVATIVE: Reduce position sizes and be more selective (0.5x normal)
- NORMAL: Standard parameters (1.0x)
- AGGRESSIVE: Increase position sizes, more trades (1.5x)
- VERY_AGGRESSIVE: Maximum aggression, all strategies active (2.0x)

Respond with JSON:
{
    "level": "NORMAL",
    "reason": "Brief explanation",
    "strategy_overrides": {"mmr_hft": "AGGRESSIVE"} // optional
}"""

    def _build_ai_prompt(self) -> str:
        """Build prompt with current conditions."""
        c = self._conditions

        # Build regime history summary
        regime_counts = {}
        for _, regime in self._regime_history[-12:]:  # Last 12 samples
            regime_counts[regime.value] = regime_counts.get(regime.value, 0) + 1

        prompt = f"""Current market conditions:

- Regime: {c.regime.value}
- Regime history (last hour): {json.dumps(regime_counts)}
- Volatility percentile: {c.volatility_percentile:.1f}%
- Spread percentile: {c.spread_percentile:.1f}%
- Volume ratio vs average: {c.volume_ratio:.2f}x
- Recent P&L: {c.recent_pnl_pct:.2%}
- Win rate: {c.win_rate:.1%}

Active HFT strategies:
- MMR-HFT: Micro mean reversion (works best in range-bound)
- Micro-Breakout: Breakout trading (works best after consolidation)
- Pair Trading: Long/short spread trading
- Liquidation Sniping: Counter-trend after cascades
- Momentum Scalping: Trend following (works best in TREND_UP/TREND_DOWN)

Recommend an aggression level."""

        return prompt

    def _parse_ai_response(self, response: str) -> AggressionLevel:
        """Parse AI response to extract aggression level."""
        try:
            # Try to extract JSON
            import re
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                level_str = data.get("level", "NORMAL").upper()
                self._state.reason = data.get("reason", "AI recommendation")

                # Parse strategy overrides
                overrides = data.get("strategy_overrides", {})
                for strategy, level in overrides.items():
                    try:
                        self._state.strategy_overrides[strategy] = AggressionLevel(level.lower())
                    except ValueError:
                        pass

                return AggressionLevel(level_str.lower())

        except Exception as e:
            logger.warning(f"Failed to parse AI response: {e}")

        # Fallback
        return AggressionLevel.NORMAL

    def apply_multiplier(self, value: Decimal, strategy_id: Optional[StrategyId] = None) -> Decimal:
        """Apply aggression multiplier to a value."""
        if strategy_id:
            multiplier = self.get_strategy_multiplier(strategy_id)
        else:
            multiplier = self._state.multiplier

        return value * multiplier

    def get_adjusted_parameters(self, strategy_id: StrategyId) -> Dict[str, Decimal]:
        """Get adjusted parameters for a strategy based on aggression."""
        multiplier = self.get_strategy_multiplier(strategy_id)

        # Base adjustments
        adjustments = {
            "position_size_multiplier": multiplier,
            "signal_threshold_multiplier": 2 - multiplier,  # Inverse for thresholds
            "max_positions_multiplier": multiplier,
        }

        return adjustments

    def to_dict(self) -> Dict:
        """Convert state to dictionary."""
        return {
            "level": self._state.level.value,
            "multiplier": float(self._state.multiplier),
            "reason": self._state.reason,
            "updated_at": self._state.updated_at.isoformat() if self._state.updated_at else None,
            "valid_until": self._state.valid_until.isoformat() if self._state.valid_until else None,
            "strategy_overrides": {k: v.value for k, v in self._state.strategy_overrides.items()},
            "metrics": self._state.metrics,
        }
