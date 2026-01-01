"""
Strategy Selector Service
=========================

Intelligent strategy selection using DeepSeek LLM with rule-based fallback.

This service:
1. Subscribes to Topic.OPPORTUNITIES for ranked trading opportunities
2. For each top opportunity, gathers context and selects optimal strategy
3. Uses DeepSeek LLM for intelligent selection when available
4. Falls back to rule-based selection when LLM unavailable
5. Publishes signals to Topic.SIGNALS with strategy decisions
6. Persists decisions to database for analysis

Strategy Selection Logic:
- LLM Mode: Sends market context to DeepSeek for intelligent selection
- Rule-Based Mode: Uses ADX/RSI/Funding thresholds for selection

Available Strategies:
- momentum: Trend-following with EMA crossovers
- mean_reversion: Counter-trend at RSI extremes
- breakout: Volatility expansion plays
- funding_arb: Funding rate arbitrage
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from simple_bot.config.loader import (
    StrategySelectorConfig,
    get_config,
)
from simple_bot.llm.client import (
    DeepSeekClient,
    StrategyDecision,
    StrategyType,
    DirectionType,
    LLMError,
    create_deepseek_client,
)
from simple_bot.services.base import BaseService
from simple_bot.services.message_bus import Message, MessageBus, Topic


logger = logging.getLogger(__name__)


# =============================================================================
# Data Models
# =============================================================================


class Signal(BaseModel):
    """
    Trading signal output from strategy selector.
    
    Published to Topic.SIGNALS for downstream processing.
    """
    
    symbol: str = Field(description="Trading symbol (e.g., 'ETH')")
    strategy: str = Field(description="Selected strategy name")
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Strategy selection confidence"
    )
    direction: Literal["long", "short", "neutral"] = Field(
        description="Trade direction"
    )
    entry_price: Optional[float] = Field(
        default=None,
        description="Suggested entry price"
    )
    reasoning: str = Field(
        default="",
        description="Explanation for strategy selection"
    )
    opportunity_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Original opportunity score from ranker"
    )
    llm_selected: bool = Field(
        default=False,
        description="Whether LLM was used for selection"
    )
    timestamp: datetime = Field(
        default_factory=datetime.utcnow,
        description="Signal generation timestamp"
    )
    
    # Additional context
    adx: Optional[float] = None
    rsi: Optional[float] = None
    funding_rate: Optional[float] = None
    market_regime: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary for message bus."""
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "confidence": round(self.confidence, 4),
            "direction": self.direction,
            "entry_price": self.entry_price,
            "reasoning": self.reasoning,
            "opportunity_score": round(self.opportunity_score, 4),
            "llm_selected": self.llm_selected,
            "timestamp": self.timestamp.isoformat(),
            "adx": round(self.adx, 2) if self.adx else None,
            "rsi": round(self.rsi, 2) if self.rsi else None,
            "funding_rate": round(self.funding_rate, 6) if self.funding_rate else None,
            "market_regime": self.market_regime,
        }


@dataclass
class StrategyPerformance:
    """Track recent performance of each strategy."""
    
    strategy: str
    trades_24h: int = 0
    wins_24h: int = 0
    pnl_24h: Decimal = Decimal("0")
    avg_duration_minutes: float = 0.0
    last_updated: datetime = field(default_factory=datetime.utcnow)
    
    @property
    def win_rate(self) -> float:
        """Calculate win rate percentage."""
        if self.trades_24h == 0:
            return 0.0
        return (self.wins_24h / self.trades_24h) * 100
    
    def to_summary_str(self) -> str:
        """Format as summary string for LLM prompt."""
        return (
            f"- {self.strategy}: {self.trades_24h} trades, "
            f"{self.win_rate:.1f}% win rate, "
            f"${float(self.pnl_24h):+.2f} PnL"
        )


# =============================================================================
# Strategy Selector Service
# =============================================================================


class StrategySelectorService(BaseService):
    """
    Service that selects optimal trading strategies for opportunities.
    
    Uses DeepSeek LLM for intelligent selection when available,
    with rule-based fallback for reliability.
    
    Workflow:
    1. Receive opportunities from Topic.OPPORTUNITIES
    2. For each top opportunity, gather context
    3. Call LLM or use rules to select strategy
    4. Publish signal to Topic.SIGNALS
    5. Store decision in database
    """
    
    # Rule-based thresholds
    STRONG_TREND_ADX = 25
    WEAK_TREND_ADX = 20
    OVERSOLD_RSI = 30
    OVERBOUGHT_RSI = 70
    HIGH_FUNDING_RATE = 0.0005  # 0.05%
    
    def __init__(
        self,
        bus: MessageBus,
        db: Optional[Any] = None,
        config: Optional[StrategySelectorConfig] = None,
        llm_client: Optional[DeepSeekClient] = None,
    ) -> None:
        """
        Initialize StrategySelectorService.
        
        Args:
            bus: MessageBus instance for pub/sub
            db: Optional Database instance for persistence
            config: Optional config (loads from global config if None)
            llm_client: Optional pre-configured LLM client
        """
        # Load config from global config if not provided
        if config is None:
            global_config = get_config()
            config = global_config.services.strategy_selector
        
        super().__init__(
            name="strategy_selector",
            bus=bus,
            db=db,
            loop_interval_seconds=1.0,  # Event-driven, not timer-based
        )
        
        self._selector_config = config
        
        # LLM client
        self._llm_client = llm_client
        self._llm_enabled = config.use_llm
        
        # Strategy performance tracking
        self._strategy_performance: Dict[str, StrategyPerformance] = {
            "momentum": StrategyPerformance(strategy="momentum"),
            "mean_reversion": StrategyPerformance(strategy="mean_reversion"),
            "breakout": StrategyPerformance(strategy="breakout"),
            "funding_arb": StrategyPerformance(strategy="funding_arb"),
        }
        
        # Recent decisions cache (symbol -> last decision time)
        self._recent_decisions: Dict[str, datetime] = {}
        
        # Last market regime
        self._current_regime: str = "neutral"
        
        # Statistics
        self._signals_generated: int = 0
        self._llm_selections: int = 0
        self._rule_selections: int = 0
        self._opportunities_processed: int = 0
    
    # =========================================================================
    # Lifecycle Methods
    # =========================================================================
    
    async def _on_start(self) -> None:
        """Initialize on service start."""
        self._logger.info(
            "Starting StrategySelectorService (LLM: %s, fallback: %s)",
            "enabled" if self._llm_enabled else "disabled",
            self._selector_config.fallback_strategy,
        )
        
        # Initialize LLM client if enabled
        if self._llm_enabled and self._llm_client is None:
            try:
                self._llm_client = create_deepseek_client()
                if not self._llm_client.is_available:
                    self._logger.warning(
                        "LLM client not available (missing API key?). "
                        "Falling back to rule-based selection."
                    )
                    self._llm_enabled = False
            except Exception as e:
                self._logger.error("Failed to create LLM client: %s", e)
                self._llm_enabled = False
        
        # Subscribe to opportunities
        await self.subscribe(Topic.OPPORTUNITIES, self._on_opportunities)
        
        # Load recent performance from database
        if self.db:
            await self._load_strategy_performance()
    
    async def _on_stop(self) -> None:
        """Cleanup on service stop."""
        self._logger.info(
            "StrategySelectorService stopped. "
            "Signals: %d (LLM: %d, Rules: %d), Opportunities: %d",
            self._signals_generated,
            self._llm_selections,
            self._rule_selections,
            self._opportunities_processed,
        )
        
        # Close LLM client
        if self._llm_client:
            await self._llm_client.close()
    
    async def _run_iteration(self) -> None:
        """Main loop - event-driven, no periodic work needed."""
        pass
    
    async def _health_check_impl(self) -> bool:
        """Service-specific health check."""
        # Check LLM availability if enabled
        if self._llm_enabled and self._llm_client:
            if not self._llm_client.is_available:
                self._logger.warning("LLM client unavailable for health check")
                # Not unhealthy - we have fallback
        return True
    
    # =========================================================================
    # Event Handlers
    # =========================================================================
    
    async def _on_opportunities(self, message: Message) -> None:
        """
        Handle incoming opportunities from ranker.
        
        Expected payload format:
        {
            "timestamp": "2024-01-01T00:00:00",
            "market_regime": "bullish|bearish|neutral|volatile",
            "rankings": [
                {
                    "rank": 1,
                    "symbol": "ETH",
                    "score": 0.85,
                    "adx": 35.5,
                    "rsi": 62.3,
                    "funding_rate": 0.0001,
                    ...
                },
                ...
            ]
        }
        """
        try:
            payload = message.payload
            if not isinstance(payload, dict):
                return
            
            self._opportunities_processed += 1
            
            # Update market regime
            self._current_regime = payload.get("market_regime", "neutral")
            
            # Get rankings
            rankings = payload.get("rankings", [])
            if not rankings:
                return
            
            # Process top opportunities
            # Only process top 5 to avoid excessive LLM calls
            top_opportunities = rankings[:5]
            
            for opp in top_opportunities:
                await self._process_opportunity(opp)
                
        except Exception as e:
            self._logger.error(
                "Error processing opportunities: %s",
                e,
                exc_info=True,
            )
    
    async def _process_opportunity(self, opportunity: Dict[str, Any]) -> None:
        """
        Process a single opportunity and generate signal.
        
        Args:
            opportunity: Opportunity data from ranker
        """
        symbol = opportunity.get("symbol")
        if not symbol:
            return
        
        # Check if we recently processed this symbol
        if not self._should_process_symbol(symbol):
            return
        
        try:
            # Gather context for decision
            context = self._build_decision_context(opportunity)
            
            # Select strategy (LLM or rules)
            if self._llm_enabled and self._llm_client and self._llm_client.is_available:
                decision = await self._select_strategy_llm(context)
                llm_selected = True
                self._llm_selections += 1
            else:
                decision = self._select_strategy_rules(context)
                llm_selected = False
                self._rule_selections += 1
            
            # Create and publish signal
            signal = Signal(
                symbol=symbol,
                strategy=decision.strategy.value,
                confidence=decision.confidence,
                direction=decision.direction.value,
                entry_price=context.get("price"),
                reasoning=decision.reasoning,
                opportunity_score=context.get("opportunity_score", 0.0),
                llm_selected=llm_selected,
                adx=context.get("adx"),
                rsi=context.get("rsi"),
                funding_rate=context.get("funding_rate"),
                market_regime=self._current_regime,
            )
            
            # Publish to message bus
            await self._publish_signal(signal)
            
            # Store in database
            if self.db:
                await self._store_decision(signal, context)
            
            # Update tracking
            self._recent_decisions[symbol] = datetime.utcnow()
            self._signals_generated += 1
            
            self._logger.info(
                "Generated signal: %s -> %s (%s, confidence=%.2f)",
                symbol,
                signal.strategy,
                signal.direction,
                signal.confidence,
            )
            
        except LLMError as e:
            self._logger.warning(
                "LLM selection failed for %s, using rules: %s",
                symbol,
                e,
            )
            # Fallback to rules
            context = self._build_decision_context(opportunity)
            decision = self._select_strategy_rules(context)
            self._rule_selections += 1
            
            signal = Signal(
                symbol=symbol,
                strategy=decision.strategy.value,
                confidence=decision.confidence,
                direction=decision.direction.value,
                entry_price=context.get("price"),
                reasoning="Rule-based fallback: " + decision.reasoning,
                opportunity_score=context.get("opportunity_score", 0.0),
                llm_selected=False,
                adx=context.get("adx"),
                rsi=context.get("rsi"),
                funding_rate=context.get("funding_rate"),
                market_regime=self._current_regime,
            )
            
            await self._publish_signal(signal)
            self._signals_generated += 1
            
        except Exception as e:
            self._logger.error(
                "Error processing opportunity %s: %s",
                symbol,
                e,
                exc_info=True,
            )
    
    # =========================================================================
    # Strategy Selection Methods
    # =========================================================================
    
    def _build_decision_context(self, opportunity: Dict[str, Any]) -> Dict[str, Any]:
        """
        Build context dictionary for strategy decision.
        
        Args:
            opportunity: Raw opportunity data from ranker
            
        Returns:
            Formatted context for LLM or rules
        """
        # Build recent performance summary
        performance_lines = []
        for strategy, perf in self._strategy_performance.items():
            performance_lines.append(perf.to_summary_str())
        recent_performance = "\n".join(performance_lines) if performance_lines else "No recent data"
        
        return {
            "symbol": opportunity.get("symbol", "UNKNOWN"),
            "price": opportunity.get("price"),
            "market_regime": self._current_regime,
            "adx": opportunity.get("adx", 0.0),
            "rsi": opportunity.get("rsi", 50.0),
            "volatility_score": opportunity.get("volatility_score", 0.5),
            "volume_score": opportunity.get("volume_score", 0.5),
            "funding_rate": (opportunity.get("funding_rate", 0.0) * 100),  # Convert to %
            "opportunity_score": opportunity.get("score", 0.0),
            "trend_score": opportunity.get("trend_score", 0.0),
            "momentum_score": opportunity.get("momentum_score", 0.0),
            "recent_performance": recent_performance,
        }
    
    async def _select_strategy_llm(
        self,
        context: Dict[str, Any],
    ) -> StrategyDecision:
        """
        Use DeepSeek LLM to select strategy.
        
        Args:
            context: Decision context
            
        Returns:
            StrategyDecision from LLM
            
        Raises:
            LLMError: If LLM call fails
        """
        return await self._llm_client.select_strategy(context)
    
    def _select_strategy_rules(
        self,
        context: Dict[str, Any],
    ) -> StrategyDecision:
        """
        Rule-based strategy selection fallback.
        
        Rules:
        1. High funding rate -> funding_arb
        2. Strong trend (ADX > 25) -> momentum
        3. Weak trend + extreme RSI -> mean_reversion
        4. Otherwise -> fallback strategy
        
        Args:
            context: Decision context
            
        Returns:
            StrategyDecision based on rules
        """
        adx = context.get("adx", 0.0) or 0.0
        rsi = context.get("rsi", 50.0) or 50.0
        funding_rate = context.get("funding_rate", 0.0) or 0.0  # Already in %
        regime = context.get("market_regime", "neutral")
        
        # Default values
        strategy = StrategyType(self._selector_config.fallback_strategy)
        confidence = 0.5
        direction = DirectionType.NEUTRAL
        reasoning = "Using fallback strategy"
        
        # Rule 1: High funding rate -> funding arbitrage
        if abs(funding_rate) >= (self.HIGH_FUNDING_RATE * 100):  # Compare in %
            strategy = StrategyType.FUNDING_ARB
            confidence = min(0.6 + abs(funding_rate) * 2, 0.9)
            direction = DirectionType.SHORT if funding_rate > 0 else DirectionType.LONG
            reasoning = f"High funding rate ({funding_rate:.4f}%) favors funding arbitrage"
        
        # Rule 2: Strong trend -> momentum
        elif adx >= self.STRONG_TREND_ADX:
            strategy = StrategyType.MOMENTUM
            confidence = min(0.5 + (adx - 25) / 50, 0.85)
            
            # Determine direction from RSI and regime
            if rsi > 55 or regime == "bullish":
                direction = DirectionType.LONG
            elif rsi < 45 or regime == "bearish":
                direction = DirectionType.SHORT
            else:
                direction = DirectionType.NEUTRAL
            
            reasoning = f"Strong trend (ADX={adx:.1f}) supports momentum strategy"
        
        # Rule 3: Weak trend + extreme RSI -> mean reversion
        elif adx < self.WEAK_TREND_ADX and (rsi < self.OVERSOLD_RSI or rsi > self.OVERBOUGHT_RSI):
            strategy = StrategyType.MEAN_REVERSION
            
            if rsi < self.OVERSOLD_RSI:
                direction = DirectionType.LONG
                confidence = min(0.5 + (30 - rsi) / 60, 0.8)
                reasoning = f"Oversold conditions (RSI={rsi:.1f}) suggest mean reversion long"
            else:
                direction = DirectionType.SHORT
                confidence = min(0.5 + (rsi - 70) / 60, 0.8)
                reasoning = f"Overbought conditions (RSI={rsi:.1f}) suggest mean reversion short"
        
        # Rule 4: Moderate volatility + breakout potential
        elif context.get("volatility_score", 0) > 0.6 and adx >= self.WEAK_TREND_ADX:
            strategy = StrategyType.BREAKOUT
            confidence = context.get("volatility_score", 0.5)
            
            if rsi > 50:
                direction = DirectionType.LONG
            elif rsi < 50:
                direction = DirectionType.SHORT
            
            reasoning = "High volatility with forming trend suggests breakout"
        
        # Default: use fallback strategy
        else:
            # Use regime to set direction
            if regime == "bullish":
                direction = DirectionType.LONG
                confidence = 0.55
            elif regime == "bearish":
                direction = DirectionType.SHORT
                confidence = 0.55
            reasoning = f"Using {strategy.value} as fallback (regime: {regime})"
        
        return StrategyDecision(
            strategy=strategy,
            confidence=confidence,
            direction=direction,
            reasoning=reasoning,
            entry_conditions=[],
            risk_factors=[],
        )
    
    # =========================================================================
    # Helper Methods
    # =========================================================================
    
    def _should_process_symbol(self, symbol: str) -> bool:
        """
        Check if we should process this symbol.
        
        Avoids reprocessing symbols too frequently.
        """
        if symbol not in self._recent_decisions:
            return True
        
        last_decision = self._recent_decisions[symbol]
        min_interval = timedelta(minutes=self._selector_config.reselect_interval_minutes)
        
        return datetime.utcnow() - last_decision >= min_interval
    
    async def _publish_signal(self, signal: Signal) -> None:
        """Publish signal to message bus."""
        await self.publish(Topic.SIGNALS, signal.to_dict())
    
    async def _store_decision(
        self,
        signal: Signal,
        context: Dict[str, Any],
    ) -> None:
        """Store decision in database."""
        try:
            decision_data = {
                "symbol": signal.symbol,
                "selected_strategy": signal.strategy,
                "confidence": signal.confidence,
                "llm_reasoning": signal.reasoning if signal.llm_selected else None,
                "input_context": context,
            }
            
            await self.db.insert_strategy_decision(decision_data)
            
        except Exception as e:
            self._logger.error(
                "Failed to store decision in database: %s",
                e,
            )
    
    async def _load_strategy_performance(self) -> None:
        """Load recent strategy performance from database."""
        try:
            # Get recent trades grouped by strategy
            trades = await self.db.get_trades(is_closed=True, limit=100)
            
            # Reset performance tracking
            for perf in self._strategy_performance.values():
                perf.trades_24h = 0
                perf.wins_24h = 0
                perf.pnl_24h = Decimal("0")
            
            # Calculate performance for last 24h
            cutoff = datetime.utcnow() - timedelta(hours=24)
            
            for trade in trades:
                strategy = trade.get("strategy")
                exit_time = trade.get("exit_time")
                
                if not strategy or strategy not in self._strategy_performance:
                    continue
                
                if exit_time and exit_time >= cutoff:
                    perf = self._strategy_performance[strategy]
                    perf.trades_24h += 1
                    
                    net_pnl = trade.get("net_pnl", Decimal("0"))
                    perf.pnl_24h += net_pnl or Decimal("0")
                    
                    if net_pnl and net_pnl > 0:
                        perf.wins_24h += 1
            
            self._logger.info("Loaded strategy performance from database")
            
        except Exception as e:
            self._logger.warning(
                "Failed to load strategy performance: %s",
                e,
            )
    
    # =========================================================================
    # Properties
    # =========================================================================
    
    @property
    def stats(self) -> Dict[str, Any]:
        """Extended service statistics."""
        base_stats = super().stats
        
        llm_stats = {}
        if self._llm_client:
            llm_stats = self._llm_client.stats
        
        base_stats.update({
            "signals_generated": self._signals_generated,
            "llm_selections": self._llm_selections,
            "rule_selections": self._rule_selections,
            "opportunities_processed": self._opportunities_processed,
            "llm_enabled": self._llm_enabled,
            "current_regime": self._current_regime,
            "llm": llm_stats,
            "strategy_performance": {
                name: {
                    "trades_24h": perf.trades_24h,
                    "win_rate": round(perf.win_rate, 1),
                    "pnl_24h": float(perf.pnl_24h),
                }
                for name, perf in self._strategy_performance.items()
            },
        })
        return base_stats
    
    @property
    def current_regime(self) -> str:
        """Get current market regime."""
        return self._current_regime
    
    @property
    def llm_available(self) -> bool:
        """Check if LLM is available for selection."""
        if not self._llm_enabled or not self._llm_client:
            return False
        return self._llm_client.is_available


# =============================================================================
# Factory Function
# =============================================================================


def create_strategy_selector(
    bus: MessageBus,
    db: Optional[Any] = None,
    config: Optional[StrategySelectorConfig] = None,
) -> StrategySelectorService:
    """
    Create and configure a StrategySelectorService.
    
    Args:
        bus: MessageBus instance
        db: Optional Database instance
        config: Optional configuration
        
    Returns:
        Configured StrategySelectorService
    """
    return StrategySelectorService(bus=bus, db=db, config=config)
