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
    """Trading aggression levels.

    SPECIFICA REQUISITI 5.1:
    - PAUSED: 0.0x risk/leverage (no trading)
    - CONSERVATIVE: 0.5x risk/leverage
    - NORMAL: 1.0x risk/leverage (default)
    - AGGRESSIVE: 1.5x risk/leverage
    - VERY_AGGRESSIVE: 2.0x risk/leverage
    """
    PAUSED = "paused"              # No trading (0.0x)
    CONSERVATIVE = "conservative"  # 0.5x normal parameters
    NORMAL = "normal"              # 1.0x (default config)
    AGGRESSIVE = "aggressive"      # 1.5x parameters
    VERY_AGGRESSIVE = "very_aggressive"  # 2.0x parameters


@dataclass
class AggressionState:
    """Current aggression state for the bot.

    Tracks both the global aggression level and per-strategy overrides.
    Includes metrics that influenced the decision for logging/debugging.
    """
    level: AggressionLevel = AggressionLevel.NORMAL
    risk_multiplier: Decimal = Decimal("1.0")     # Moltiplicatore rischio per trade
    leverage_multiplier: Decimal = Decimal("1.0")  # Moltiplicatore leva
    reason: str = ""
    updated_at: Optional[datetime] = None
    valid_until: Optional[datetime] = None

    # Per-strategy overrides
    strategy_overrides: Dict[str, AggressionLevel] = field(default_factory=dict)

    # Metrics that influenced the decision
    metrics: Dict[str, Any] = field(default_factory=dict)

    # Track last 100 trades for win rate calculation
    recent_trades: List[Dict] = field(default_factory=list)


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


# Aggression level multipliers per SPECIFICA REQUISITI 5.1
# Ogni livello ha moltiplicatori separati per risk e leverage
AGGRESSION_RISK_MULTIPLIERS = {
    AggressionLevel.PAUSED: Decimal("0"),
    AggressionLevel.CONSERVATIVE: Decimal("0.5"),
    AggressionLevel.NORMAL: Decimal("1.0"),
    AggressionLevel.AGGRESSIVE: Decimal("1.5"),
    AggressionLevel.VERY_AGGRESSIVE: Decimal("2.0"),
}

AGGRESSION_LEVERAGE_MULTIPLIERS = {
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
            api_key = getattr(self.settings, 'deepseek_api_key', None)
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
    def current_risk_multiplier(self) -> Decimal:
        """Get current risk multiplier."""
        return self._state.risk_multiplier

    @property
    def current_leverage_multiplier(self) -> Decimal:
        """Get current leverage multiplier."""
        return self._state.leverage_multiplier

    @property
    def is_paused(self) -> bool:
        """Check if trading is paused by aggression controller."""
        return self._state.level == AggressionLevel.PAUSED

    def get_strategy_risk_multiplier(self, strategy_id: StrategyId) -> Decimal:
        """Get risk multiplier for a specific strategy (with override support)."""
        override = self._state.strategy_overrides.get(strategy_id.value)
        if override:
            return AGGRESSION_RISK_MULTIPLIERS.get(override, Decimal("1.0"))
        return self._state.risk_multiplier

    def get_strategy_leverage_multiplier(self, strategy_id: StrategyId) -> Decimal:
        """Get leverage multiplier for a specific strategy (with override support)."""
        override = self._state.strategy_overrides.get(strategy_id.value)
        if override:
            return AGGRESSION_LEVERAGE_MULTIPLIERS.get(override, Decimal("1.0"))
        return self._state.leverage_multiplier

    async def update(
        self,
        regime: MarketRegime,
        recent_pnl_pct: float,
        win_rate: float,
        volatility_percentile: float = 50.0,
        spread_percentile: float = 50.0,
        volume_ratio: float = 1.0,
        circuit_breaker_triggered: bool = False,
        circuit_breaker_level: Optional[int] = None,
    ):
        """
        Update aggression state based on current conditions.

        Called periodically (e.g., every 5 minutes) or on specific events.

        REQUISITI 5.2 - Trigger cambio livello:
        - win rate ultimi 100 trade > 58% → +1 livello
        - P&L daily < -2% → -1 livello
        - attivazione CB livello 2 o 3 → scende a CONSERVATIVE
        - regime = trend_up/down → +1 livello (fino a AGGRESSIVE)

        Args:
            regime: Current market regime
            recent_pnl_pct: Recent P&L percentage (daily)
            win_rate: Win rate over last 100 trades
            volatility_percentile: Volatility percentile (0-100)
            spread_percentile: Spread percentile (0-100)
            volume_ratio: Volume ratio vs average
            circuit_breaker_triggered: Whether circuit breaker was triggered
            circuit_breaker_level: Which circuit breaker level (2 or 3)
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
            new_level = self._determine_level_rules(
                circuit_breaker_triggered=circuit_breaker_triggered,
                circuit_breaker_level=circuit_breaker_level,
            )

        # Update state with separate risk and leverage multipliers
        old_level = self._state.level
        self._state.level = new_level
        self._state.risk_multiplier = AGGRESSION_RISK_MULTIPLIERS.get(new_level, Decimal("1.0"))
        self._state.leverage_multiplier = AGGRESSION_LEVERAGE_MULTIPLIERS.get(new_level, Decimal("1.0"))
        self._state.updated_at = now
        self._state.valid_until = now + timedelta(minutes=self._update_interval_minutes * 2)
        self._state.metrics = {
            "regime": regime.value,
            "volatility_percentile": volatility_percentile,
            "spread_percentile": spread_percentile,
            "volume_ratio": volume_ratio,
            "recent_pnl_pct": recent_pnl_pct,
            "win_rate": win_rate,
            "circuit_breaker_triggered": circuit_breaker_triggered,
            "circuit_breaker_level": circuit_breaker_level,
        }

        self._last_update = now

        if old_level != new_level:
            logger.info(
                f"Aggression level changed: {old_level.value} -> {new_level.value} "
                f"(risk_mult: {self._state.risk_multiplier}, leverage_mult: {self._state.leverage_multiplier}) "
                f"Reason: {self._state.reason}"
            )

    def _determine_level_rules(
        self,
        circuit_breaker_triggered: bool = False,
        circuit_breaker_level: Optional[int] = None,
    ) -> AggressionLevel:
        """
        Determine aggression level using rule-based logic per REQUISITI 5.2.

        TRIGGER CAMBIO LIVELLO:
        1. Win rate ultimi 100 trade > 58% → +1 livello
        2. P&L daily < -2% → -1 livello
        3. Attivazione CB livello 2 o 3 → scende a CONSERVATIVE
        4. Regime = trend_up/down → +1 livello (fino a AGGRESSIVE)

        Args:
            circuit_breaker_triggered: Se il circuit breaker è stato attivato
            circuit_breaker_level: Livello del circuit breaker (2 o 3)

        Returns:
            AggressionLevel calcolato
        """
        conditions = self._conditions
        current_level = self._state.level

        # TRIGGER 3: Circuit Breaker livello 2 o 3 → CONSERVATIVE
        # Questo ha priorità assoluta perché indica condizioni di rischio grave
        if circuit_breaker_triggered and circuit_breaker_level in [2, 3]:
            self._state.reason = f"Circuit breaker livello {circuit_breaker_level} attivato → CONSERVATIVE"
            logger.warning(f"Aggression Controller: {self._state.reason}")
            return AggressionLevel.CONSERVATIVE

        # TRIGGER 2: P&L daily < -2% → -1 livello
        # Se stiamo perdendo troppo, riduciamo l'aggressività
        if conditions.recent_pnl_pct < -0.02:
            new_level = self._decrease_level(current_level)
            self._state.reason = f"P&L daily {conditions.recent_pnl_pct:.2%} < -2% → ridotto a {new_level.value}"
            logger.warning(f"Aggression Controller: {self._state.reason}")
            return new_level

        # Calcola livello base partendo da NORMAL
        base_level = AggressionLevel.NORMAL
        adjustments = []

        # TRIGGER 1: Win rate > 58% → +1 livello
        if conditions.win_rate > 0.58:
            base_level = self._increase_level(base_level)
            adjustments.append(f"win_rate {conditions.win_rate:.1%} > 58%")

        # TRIGGER 4: Regime trend_up/down → +1 livello (fino a AGGRESSIVE)
        if conditions.regime in (MarketRegime.TREND_UP, MarketRegime.TREND_DOWN):
            # Aumenta di 1 livello ma non oltre AGGRESSIVE per regime alone
            if base_level == AggressionLevel.NORMAL:
                base_level = AggressionLevel.AGGRESSIVE
                adjustments.append(f"regime {conditions.regime.value}")
            elif base_level == AggressionLevel.CONSERVATIVE:
                base_level = AggressionLevel.NORMAL
                adjustments.append(f"regime {conditions.regime.value}")

        # Additional safety checks (manteniamo alcune regole di sicurezza)

        # Se win rate è troppo basso, limitiamo comunque a CONSERVATIVE
        if conditions.win_rate < 0.45:
            if base_level in (AggressionLevel.AGGRESSIVE, AggressionLevel.VERY_AGGRESSIVE):
                base_level = AggressionLevel.NORMAL
                adjustments.append(f"win_rate {conditions.win_rate:.1%} < 45% → capped at NORMAL")
            elif base_level == AggressionLevel.NORMAL:
                base_level = AggressionLevel.CONSERVATIVE
                adjustments.append(f"win_rate {conditions.win_rate:.1%} < 45% → reduced to CONSERVATIVE")

        # Se P&L giornaliero è positivo e win rate eccellente (>60%), possiamo essere VERY_AGGRESSIVE
        if conditions.win_rate > 0.60 and conditions.recent_pnl_pct > 0.01:
            base_level = AggressionLevel.VERY_AGGRESSIVE
            adjustments.append(f"exceptional: win_rate {conditions.win_rate:.1%} & pnl {conditions.recent_pnl_pct:.2%}")

        # Build reason string
        if adjustments:
            self._state.reason = f"{base_level.value}: " + ", ".join(adjustments)
        else:
            self._state.reason = f"{base_level.value}: no special triggers"

        # Apply regime-specific strategy overrides
        self._apply_regime_overrides(conditions)

        return base_level

    def _increase_level(self, level: AggressionLevel) -> AggressionLevel:
        """Aumenta il livello di aggressività di 1 step."""
        order = [
            AggressionLevel.PAUSED,
            AggressionLevel.CONSERVATIVE,
            AggressionLevel.NORMAL,
            AggressionLevel.AGGRESSIVE,
            AggressionLevel.VERY_AGGRESSIVE,
        ]
        try:
            idx = order.index(level)
            if idx < len(order) - 1:
                return order[idx + 1]
        except ValueError:
            pass
        return level

    def _decrease_level(self, level: AggressionLevel) -> AggressionLevel:
        """Diminuisce il livello di aggressività di 1 step."""
        order = [
            AggressionLevel.PAUSED,
            AggressionLevel.CONSERVATIVE,
            AggressionLevel.NORMAL,
            AggressionLevel.AGGRESSIVE,
            AggressionLevel.VERY_AGGRESSIVE,
        ]
        try:
            idx = order.index(level)
            if idx > 0:
                return order[idx - 1]
        except ValueError:
            pass
        return level

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

        # Safe values with defaults to avoid None format errors
        vol_pct = c.volatility_percentile if c.volatility_percentile is not None else 50.0
        spread_pct = c.spread_percentile if c.spread_percentile is not None else 50.0
        vol_ratio = c.volume_ratio if c.volume_ratio is not None else 1.0
        pnl_pct = c.recent_pnl_pct if c.recent_pnl_pct is not None else 0.0
        win_rt = c.win_rate if c.win_rate is not None else 0.5

        prompt = f"""Current market conditions:

- Regime: {c.regime.value}
- Regime history (last hour): {json.dumps(regime_counts)}
- Volatility percentile: {vol_pct:.1f}%
- Spread percentile: {spread_pct:.1f}%
- Volume ratio vs average: {vol_ratio:.2f}x
- Recent P&L: {pnl_pct:.2%}
- Win rate: {win_rt:.1%}

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

    def apply_risk_multiplier(self, value: Decimal, strategy_id: Optional[StrategyId] = None) -> Decimal:
        """Apply risk multiplier to a value (per-trade risk sizing)."""
        if strategy_id:
            multiplier = self.get_strategy_risk_multiplier(strategy_id)
        else:
            multiplier = self._state.risk_multiplier

        return value * multiplier

    def apply_leverage_multiplier(self, value: Decimal, strategy_id: Optional[StrategyId] = None) -> Decimal:
        """Apply leverage multiplier to a value (leverage limits)."""
        if strategy_id:
            multiplier = self.get_strategy_leverage_multiplier(strategy_id)
        else:
            multiplier = self._state.leverage_multiplier

        return value * multiplier

    def get_adjusted_parameters(self, strategy_id: StrategyId) -> Dict[str, Decimal]:
        """
        Get adjusted parameters for a strategy based on aggression.

        Ritorna moltiplicatori separati per risk e leverage per permettere
        al risk_engine di applicarli in modo indipendente.
        """
        risk_mult = self.get_strategy_risk_multiplier(strategy_id)
        leverage_mult = self.get_strategy_leverage_multiplier(strategy_id)

        # Base adjustments
        adjustments = {
            "risk_multiplier": risk_mult,            # Applica a max_risk_per_trade_pct
            "leverage_multiplier": leverage_mult,    # Applica a max_portfolio_leverage e position leverage
            "signal_threshold_multiplier": Decimal("2") - risk_mult,  # Inverse for thresholds
        }

        return adjustments

    def to_dict(self) -> Dict:
        """Convert state to dictionary."""
        return {
            "level": self._state.level.value,
            "risk_multiplier": float(self._state.risk_multiplier),
            "leverage_multiplier": float(self._state.leverage_multiplier),
            "reason": self._state.reason,
            "updated_at": self._state.updated_at.isoformat() if self._state.updated_at else None,
            "valid_until": self._state.valid_until.isoformat() if self._state.valid_until else None,
            "strategy_overrides": {k: v.value for k, v in self._state.strategy_overrides.items()},
            "metrics": self._state.metrics,
        }

    def record_trade(self, trade_result: Dict):
        """
        Record trade result for win rate calculation.

        Mantiene gli ultimi 100 trade per calcolare il win rate
        richiesto dal REQUISITO 5.2 (win rate > 58% → +1 livello).

        Args:
            trade_result: Dict with keys 'pnl', 'is_win', 'timestamp', etc.
        """
        self._state.recent_trades.append(trade_result)

        # Keep only last 100 trades
        if len(self._state.recent_trades) > 100:
            self._state.recent_trades = self._state.recent_trades[-100:]

    def get_win_rate(self) -> float:
        """
        Calculate win rate from last N trades (up to 100).

        Returns:
            Win rate as float (0.0 to 1.0)
        """
        if not self._state.recent_trades:
            return 0.5  # Default 50% if no trades

        wins = sum(1 for t in self._state.recent_trades if t.get('is_win', False))
        return wins / len(self._state.recent_trades)


# =============================================================================
# INTEGRAZIONE CON RISK ENGINE
# =============================================================================
"""
ESEMPIO DI INTEGRAZIONE NEL RISK_ENGINE:

1. Inizializzazione nel risk_engine.__init__():

   from ..ai.aggression_controller import AggressionController

   self.aggression_controller = AggressionController(settings)

2. Applicare i moltiplicatori nel calcolo del position sizing:

   # In risk_engine.py, metodo _process_single_proposal():

   # Get aggression multipliers for this strategy
   risk_mult = self.aggression_controller.get_strategy_risk_multiplier(proposal.strategy_id)
   leverage_mult = self.aggression_controller.get_strategy_leverage_multiplier(proposal.strategy_id)

   # Apply risk multiplier to max_risk_per_trade_pct
   adjusted_risk_pct = self.risk_config.max_risk_per_trade_pct * risk_mult

   # Apply leverage multiplier to leverage limits
   adjusted_max_leverage = self.risk_config.max_portfolio_leverage * leverage_mult

   # Calcola size con i parametri aggiustati
   size = self.position_sizer.calculate_size(
       proposal,
       account,
       current_price,
       max_risk_pct=adjusted_risk_pct,  # Risk aggiustato
       atr=atr,
   )

   # Calcola leverage con limite aggiustato
   leverage = min(
       self.position_sizer.calculate_leverage(size, current_price, account, proposal.symbol),
       adjusted_max_leverage  # Leverage limit aggiustato
   )

3. Aggiornare lo stato nel main loop del bot:

   # In bot.py, nel main loop:

   # Calculate win rate from recent trades (database query)
   win_rate = await self._calculate_win_rate_last_100_trades()

   # Get circuit breaker status
   cb_triggered = self.risk_engine.circuit_breaker.is_temporal_cooldown()
   cb_level = None
   if cb_triggered:
       temporal_status = self.risk_engine.circuit_breaker.get_temporal_status()
       if temporal_status.get('active_level') in ['level_2', 'level_3']:
           cb_level = 2 if 'level_2' in temporal_status.get('active_level', '') else 3

   # Update aggression controller
   await self.aggression_controller.update(
       regime=current_regime,
       recent_pnl_pct=account.daily_pnl_pct,
       win_rate=win_rate,
       volatility_percentile=volatility_pct,
       circuit_breaker_triggered=cb_triggered,
       circuit_breaker_level=cb_level,
   )

4. Registrare i risultati dei trade:

   # Dopo che un trade è chiuso (in execution_engine o bot):

   trade_result = {
       'pnl': trade.realized_pnl,
       'is_win': trade.realized_pnl > 0,
       'timestamp': datetime.now(timezone.utc),
       'strategy_id': trade.strategy_id.value,
   }
   self.aggression_controller.record_trade(trade_result)

5. Check se trading è permesso:

   # Prima di processare proposals:

   if self.aggression_controller.is_paused:
       logger.warning("Aggression controller in PAUSED mode - skipping proposals")
       return []

QUESTO GARANTISCE CHE:
- I moltiplicatori influenzino realmente il rischio per trade (via max_risk_per_trade_pct)
- I moltiplicatori influenzino realmente la leva (via max_portfolio_leverage)
- Il sistema reagisca dinamicamente alle performance e al market regime
- I trigger specificati nei REQUISITI 5.2 vengano applicati correttamente
"""
