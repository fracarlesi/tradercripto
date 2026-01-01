"""
Opportunity Ranker Service
==========================

Ranks trading opportunities across all symbols based on multiple factors:
- Trend strength (ADX)
- Volatility (ATR vs 7-day average)
- Volume (24h volume vs 7-day average)
- Funding rate edge
- Liquidity (spread + depth)
- Momentum (RSI direction + MACD alignment)

Publishes top N opportunities to Topic.OPPORTUNITIES for downstream services.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from simple_bot.config.loader import (
    OpportunityRankerConfig,
    OpportunityWeights,
    get_config,
)
from simple_bot.services.base import BaseService
from simple_bot.services.message_bus import Message, MessageBus, Topic
from simple_bot.strategies import (
    calculate_adx,
    calculate_atr,
    calculate_ema,
    calculate_rsi,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Data Models
# =============================================================================


@dataclass
class SymbolMetrics:
    """Metrics for a single symbol used for opportunity scoring."""
    
    symbol: str
    price: float
    prices_history: List[float] = field(default_factory=list)
    volume_24h: float = 0.0
    avg_volume_7d: float = 0.0
    funding_rate: float = 0.0
    spread_pct: float = 0.0
    bid_depth: float = 0.0
    ask_depth: float = 0.0
    
    # Calculated indicators
    adx: Optional[float] = None
    atr: Optional[float] = None
    avg_atr_7d: Optional[float] = None
    rsi: Optional[float] = None
    rsi_prev: Optional[float] = None
    ema_50: Optional[float] = None
    
    def has_sufficient_data(self) -> bool:
        """Check if we have enough price history for indicator calculation."""
        return len(self.prices_history) >= 50


@dataclass
class OpportunityScore:
    """Detailed opportunity score breakdown for a symbol."""
    
    symbol: str
    score: float
    rank: int = 0
    
    # Component scores (all normalized 0-1)
    trend_score: float = 0.0
    volatility_score: float = 0.0
    volume_score: float = 0.0
    funding_score: float = 0.0
    liquidity_score: float = 0.0
    momentum_score: float = 0.0
    
    # Raw values for transparency
    adx: Optional[float] = None
    atr: Optional[float] = None
    rsi: Optional[float] = None
    funding_rate: float = 0.0
    spread_pct: float = 0.0
    volume_24h: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary for message bus and database."""
        return {
            "rank": self.rank,
            "symbol": self.symbol,
            "score": round(self.score, 4),
            "trend_score": round(self.trend_score, 4),
            "volatility_score": round(self.volatility_score, 4),
            "volume_score": round(self.volume_score, 4),
            "funding_score": round(self.funding_score, 4),
            "liquidity_score": round(self.liquidity_score, 4),
            "momentum_score": round(self.momentum_score, 4),
            "adx": round(self.adx, 2) if self.adx else None,
            "atr": round(self.atr, 6) if self.atr else None,
            "rsi": round(self.rsi, 2) if self.rsi else None,
            "funding_rate": round(self.funding_rate, 6),
            "spread_pct": round(self.spread_pct, 4),
            "volume_24h": round(self.volume_24h, 2),
        }


# =============================================================================
# Opportunity Ranker Service
# =============================================================================


class OpportunityRankerService(BaseService):
    """
    Service that ranks trading opportunities across all symbols.
    
    Subscribes to Topic.MARKET_DATA for price/volume updates.
    Publishes ranked opportunities to Topic.OPPORTUNITIES.
    
    Scoring is based on weighted factors:
    - Trend strength (ADX normalized)
    - Volatility (ATR vs 7d average)
    - Volume (24h vs 7d average)
    - Funding rate edge
    - Liquidity (spread + depth)
    - Momentum (RSI direction)
    
    The market regime is detected from BTC data to provide context.
    """
    
    # Maximum acceptable spread for liquidity scoring
    MAX_ACCEPTABLE_SPREAD_PCT = 0.5  # 0.5%
    
    def __init__(
        self,
        bus: MessageBus,
        db: Optional[Any] = None,
        config: Optional[OpportunityRankerConfig] = None,
    ) -> None:
        """
        Initialize OpportunityRankerService.
        
        Args:
            bus: MessageBus instance for pub/sub communication
            db: Optional Database instance for persistence
            config: Optional configuration (loads from global config if None)
        """
        # Load config from global config if not provided
        if config is None:
            global_config = get_config()
            config = global_config.services.opportunity_ranker
        
        super().__init__(
            name="opportunity_ranker",
            bus=bus,
            db=db,
            loop_interval_seconds=1.0,  # Process on market data, not timer
        )
        
        self._ranker_config = config
        self._weights = config.weights
        
        # Symbol metrics storage
        self._symbol_metrics: Dict[str, SymbolMetrics] = {}
        
        # Last rankings for publishing
        self._last_rankings: List[OpportunityScore] = []
        self._last_regime: str = "neutral"
        self._last_ranking_time: Optional[datetime] = None
        
        # BTC reference for regime detection
        self._btc_metrics: Optional[SymbolMetrics] = None
        
        # Statistics
        self._rankings_published: int = 0
        self._messages_processed: int = 0
    
    # =========================================================================
    # Lifecycle Methods
    # =========================================================================
    
    async def _on_start(self) -> None:
        """Subscribe to market data topic on service start."""
        self._logger.info(
            "Starting OpportunityRankerService with weights: %s",
            self._weights
        )
        await self.subscribe(Topic.MARKET_DATA, self._on_market_data)
    
    async def _on_stop(self) -> None:
        """Cleanup on service stop."""
        self._logger.info(
            "OpportunityRankerService stopped. "
            "Processed %d messages, published %d rankings",
            self._messages_processed,
            self._rankings_published,
        )
    
    async def _run_iteration(self) -> None:
        """
        Main loop iteration - not used since we're event-driven.
        
        Rankings are computed on each market data update.
        """
        pass
    
    async def _health_check_impl(self) -> bool:
        """Check service-specific health."""
        # Consider unhealthy if no rankings in last 5 minutes
        if self._last_ranking_time:
            age_seconds = (datetime.utcnow() - self._last_ranking_time).total_seconds()
            if age_seconds > 300:
                self._logger.warning(
                    "No rankings published in %.0f seconds",
                    age_seconds
                )
                return False
        return True
    
    # =========================================================================
    # Market Data Handling
    # =========================================================================
    
    async def _on_market_data(self, message: Message) -> None:
        """
        Handle incoming market data messages.
        
        Expected payload format:
        {
            "symbol": "BTC",
            "price": 42000.0,
            "prices": [41900, 41950, 42000, ...],  # Historical prices
            "volume_24h": 1000000.0,
            "avg_volume_7d": 950000.0,
            "funding_rate": 0.0001,
            "spread_pct": 0.01,
            "bid_depth": 100000.0,
            "ask_depth": 100000.0,
        }
        """
        try:
            self._messages_processed += 1
            payload = message.payload
            
            if not isinstance(payload, dict):
                self._logger.warning("Invalid market data payload type: %s", type(payload))
                return
            
            symbol = payload.get("symbol")
            if not symbol:
                return
            
            # Update symbol metrics
            self._update_symbol_metrics(symbol, payload)
            
            # Track BTC for regime detection
            if symbol.upper() in ("BTC", "BTCUSD", "BTCUSDT"):
                self._btc_metrics = self._symbol_metrics.get(symbol)
            
            # Compute and publish rankings periodically
            # Only rank when we receive BTC data or every 10 messages
            should_rank = (
                symbol.upper() in ("BTC", "BTCUSD", "BTCUSDT") or
                self._messages_processed % 10 == 0
            )
            
            if should_rank and len(self._symbol_metrics) >= 3:
                await self._compute_and_publish_rankings()
                
        except Exception as e:
            self._logger.error(
                "Error processing market data: %s",
                e,
                exc_info=True
            )
    
    def _update_symbol_metrics(self, symbol: str, data: Dict[str, Any]) -> None:
        """Update metrics for a symbol from market data."""
        if symbol not in self._symbol_metrics:
            self._symbol_metrics[symbol] = SymbolMetrics(
                symbol=symbol,
                price=data.get("price", 0.0),
            )
        
        metrics = self._symbol_metrics[symbol]
        
        # Update basic data
        metrics.price = data.get("price", metrics.price)
        metrics.volume_24h = data.get("volume_24h", metrics.volume_24h)
        metrics.avg_volume_7d = data.get("avg_volume_7d", metrics.avg_volume_7d)
        metrics.funding_rate = data.get("funding_rate", metrics.funding_rate)
        metrics.spread_pct = data.get("spread_pct", metrics.spread_pct)
        metrics.bid_depth = data.get("bid_depth", metrics.bid_depth)
        metrics.ask_depth = data.get("ask_depth", metrics.ask_depth)
        
        # Update price history
        if "prices" in data and isinstance(data["prices"], list):
            metrics.prices_history = data["prices"]
        elif metrics.price > 0:
            # Append current price to history (keep last 200)
            metrics.prices_history.append(metrics.price)
            if len(metrics.prices_history) > 200:
                metrics.prices_history = metrics.prices_history[-200:]
        
        # Update ATR history for 7d average
        if "avg_atr_7d" in data:
            metrics.avg_atr_7d = data["avg_atr_7d"]
        
        # Compute indicators if we have enough data
        self._compute_indicators(metrics)
    
    def _compute_indicators(self, metrics: SymbolMetrics) -> None:
        """Compute technical indicators for a symbol."""
        prices = metrics.prices_history
        
        if len(prices) < 15:
            return
        
        # ADX (trend strength)
        metrics.adx = calculate_adx(prices, period=14)
        
        # ATR (volatility)
        metrics.atr = calculate_atr(prices, period=14)
        
        # Compute 7d ATR average if not provided
        if metrics.avg_atr_7d is None and len(prices) >= 100:
            # Use older ATR as proxy for 7d average
            old_atr = calculate_atr(prices[:-50], period=14)
            if old_atr:
                metrics.avg_atr_7d = old_atr
        
        # RSI
        metrics.rsi_prev = metrics.rsi
        metrics.rsi = calculate_rsi(prices, period=14)
        
        # EMA 50
        metrics.ema_50 = calculate_ema(prices, period=50)
    
    # =========================================================================
    # Opportunity Scoring
    # =========================================================================
    
    async def _compute_and_publish_rankings(self) -> None:
        """Compute opportunity scores and publish rankings."""
        try:
            # Compute scores for all symbols
            scores: List[OpportunityScore] = []
            
            for symbol, metrics in self._symbol_metrics.items():
                if not metrics.has_sufficient_data():
                    continue
                
                score = self._compute_opportunity_score(metrics)
                if score.score >= self._ranker_config.min_score:
                    scores.append(score)
            
            # Sort by score descending
            scores.sort(key=lambda s: s.score, reverse=True)
            
            # Take top N
            top_n = self._ranker_config.top_n
            top_scores = scores[:top_n]
            
            # Assign ranks
            for i, score in enumerate(top_scores, start=1):
                score.rank = i
            
            # Detect market regime
            regime = self._detect_market_regime()
            
            # Store rankings
            self._last_rankings = top_scores
            self._last_regime = regime
            self._last_ranking_time = datetime.utcnow()
            
            # Publish to message bus
            await self._publish_rankings(top_scores, regime)
            
            # Store in database
            if self.db and top_scores:
                await self._store_rankings(top_scores, regime)
            
            self._rankings_published += 1
            
            self._logger.debug(
                "Published rankings: regime=%s, top_symbol=%s, score=%.3f",
                regime,
                top_scores[0].symbol if top_scores else "none",
                top_scores[0].score if top_scores else 0,
            )
            
        except Exception as e:
            self._logger.error(
                "Error computing rankings: %s",
                e,
                exc_info=True
            )
    
    def _compute_opportunity_score(self, metrics: SymbolMetrics) -> OpportunityScore:
        """
        Compute opportunity score for a symbol.
        
        Score = weighted sum of normalized component scores:
        - trend_strength: ADX / 50 (capped at 1.0)
        - volatility_score: current_ATR / avg_ATR_7d (normalized 0-1)
        - volume_score: volume_24h / avg_volume_7d (normalized 0-1)
        - funding_edge: abs(funding_rate) * 10 (capped at 1.0)
        - liquidity_score: 1 - (spread_pct / max_acceptable_spread)
        - momentum_score: RSI direction alignment with price
        """
        weights = self._weights
        
        # Trend strength: ADX / 50, capped at 1.0
        # ADX > 25 indicates strong trend, > 50 is very strong
        trend_score = 0.0
        if metrics.adx is not None:
            trend_score = min(metrics.adx / 50.0, 1.0)
        
        # Volatility score: current ATR vs 7d average ATR
        # Higher volatility = more opportunity
        volatility_score = 0.0
        if metrics.atr is not None and metrics.avg_atr_7d and metrics.avg_atr_7d > 0:
            ratio = metrics.atr / metrics.avg_atr_7d
            # Normalize: 0.5x avg = 0, 1x avg = 0.5, 2x avg = 1.0
            volatility_score = min(max((ratio - 0.5) / 1.5, 0.0), 1.0)
        elif metrics.atr is not None:
            # No 7d average, use reasonable assumption
            volatility_score = 0.5
        
        # Volume score: 24h volume vs 7d average
        volume_score = 0.0
        if metrics.volume_24h > 0 and metrics.avg_volume_7d > 0:
            ratio = metrics.volume_24h / metrics.avg_volume_7d
            # Normalize: 0.5x avg = 0, 1x avg = 0.5, 2x avg = 1.0
            volume_score = min(max((ratio - 0.5) / 1.5, 0.0), 1.0)
        elif metrics.volume_24h > 0:
            volume_score = 0.5
        
        # Funding rate edge: higher funding = more opportunity for arb
        # Multiply by 10 because typical funding rates are 0.01% - 0.1%
        funding_score = min(abs(metrics.funding_rate) * 10, 1.0)
        
        # Liquidity score: based on spread (lower spread = better liquidity)
        liquidity_score = 0.0
        if metrics.spread_pct >= 0:
            spread_ratio = metrics.spread_pct / self.MAX_ACCEPTABLE_SPREAD_PCT
            liquidity_score = max(1.0 - spread_ratio, 0.0)
        
        # Momentum score: RSI direction alignment
        # Score higher if RSI is moving in a clear direction
        momentum_score = 0.0
        if metrics.rsi is not None:
            # RSI < 30 or > 70 = strong momentum signal
            if metrics.rsi < 30:
                # Oversold - bullish momentum potential
                momentum_score = (30 - metrics.rsi) / 30
            elif metrics.rsi > 70:
                # Overbought - bearish momentum potential
                momentum_score = (metrics.rsi - 70) / 30
            else:
                # Neutral zone - check direction
                if metrics.rsi_prev is not None:
                    rsi_change = abs(metrics.rsi - metrics.rsi_prev)
                    momentum_score = min(rsi_change / 10, 0.5)  # Cap at 0.5 in neutral
        
        # Compute weighted total score
        total_score = (
            trend_score * weights.trend_strength +
            volatility_score * weights.volatility +
            volume_score * weights.volume +
            funding_score * weights.funding +
            liquidity_score * weights.liquidity +
            momentum_score * weights.momentum
        )
        
        return OpportunityScore(
            symbol=metrics.symbol,
            score=total_score,
            trend_score=trend_score,
            volatility_score=volatility_score,
            volume_score=volume_score,
            funding_score=funding_score,
            liquidity_score=liquidity_score,
            momentum_score=momentum_score,
            adx=metrics.adx,
            atr=metrics.atr,
            rsi=metrics.rsi,
            funding_rate=metrics.funding_rate,
            spread_pct=metrics.spread_pct,
            volume_24h=metrics.volume_24h,
        )
    
    # =========================================================================
    # Market Regime Detection
    # =========================================================================
    
    def _detect_market_regime(self) -> str:
        """
        Detect overall market regime from BTC data.
        
        Returns:
            "bullish": BTC ADX > 25 and price > EMA50
            "bearish": BTC ADX > 25 and price < EMA50
            "volatile": Overall market ATR > 1.5x average
            "neutral": Otherwise
        """
        # Check for volatile regime first (affects all symbols)
        if self._is_high_volatility_regime():
            return "volatile"
        
        # Use BTC as market proxy
        if self._btc_metrics is None:
            return "neutral"
        
        btc = self._btc_metrics
        
        # Need ADX and EMA50 for trend detection
        if btc.adx is None or btc.ema_50 is None:
            return "neutral"
        
        # Strong trend threshold
        STRONG_TREND_ADX = 25
        
        if btc.adx > STRONG_TREND_ADX:
            if btc.price > btc.ema_50:
                return "bullish"
            else:
                return "bearish"
        
        return "neutral"
    
    def _is_high_volatility_regime(self) -> bool:
        """
        Check if overall market is in high volatility regime.
        
        Returns True if average ATR ratio across symbols > 1.5.
        """
        atr_ratios: List[float] = []
        
        for metrics in self._symbol_metrics.values():
            if metrics.atr is not None and metrics.avg_atr_7d and metrics.avg_atr_7d > 0:
                ratio = metrics.atr / metrics.avg_atr_7d
                atr_ratios.append(ratio)
        
        if len(atr_ratios) < 3:
            return False
        
        avg_ratio = sum(atr_ratios) / len(atr_ratios)
        return avg_ratio > 1.5
    
    # =========================================================================
    # Publishing & Storage
    # =========================================================================
    
    async def _publish_rankings(
        self,
        rankings: List[OpportunityScore],
        regime: str
    ) -> None:
        """Publish rankings to Topic.OPPORTUNITIES."""
        payload = {
            "timestamp": datetime.utcnow().isoformat(),
            "market_regime": regime,
            "rankings": [r.to_dict() for r in rankings],
        }
        
        await self.publish(Topic.OPPORTUNITIES, payload)
    
    async def _store_rankings(
        self,
        rankings: List[OpportunityScore],
        regime: str
    ) -> None:
        """Store rankings in database."""
        try:
            # Prepare rankings as JSON-serializable list
            rankings_data = [r.to_dict() for r in rankings]
            
            # Get BTC price for context
            btc_price = None
            if self._btc_metrics:
                btc_price = Decimal(str(self._btc_metrics.price))
            
            # Calculate total 24h volume
            total_volume = sum(
                m.volume_24h 
                for m in self._symbol_metrics.values()
            )
            total_volume_decimal = Decimal(str(total_volume)) if total_volume > 0 else None
            
            await self.db.insert_opportunity_ranking(
                rankings=rankings_data,
                regime=regime,
                btc_price=btc_price,
                total_volume_24h=total_volume_decimal,
            )
            
        except Exception as e:
            self._logger.error(
                "Failed to store rankings in database: %s",
                e,
                exc_info=True
            )
    
    # =========================================================================
    # Properties & Stats
    # =========================================================================
    
    @property
    def stats(self) -> Dict[str, Any]:
        """Extended service statistics."""
        base_stats = super().stats
        base_stats.update({
            "messages_processed": self._messages_processed,
            "rankings_published": self._rankings_published,
            "symbols_tracked": len(self._symbol_metrics),
            "last_regime": self._last_regime,
            "last_ranking_time": (
                self._last_ranking_time.isoformat()
                if self._last_ranking_time else None
            ),
            "top_symbol": (
                self._last_rankings[0].symbol
                if self._last_rankings else None
            ),
            "top_score": (
                round(self._last_rankings[0].score, 4)
                if self._last_rankings else None
            ),
        })
        return base_stats
    
    @property
    def current_rankings(self) -> List[Dict[str, Any]]:
        """Get current rankings as list of dicts."""
        return [r.to_dict() for r in self._last_rankings]
    
    @property
    def current_regime(self) -> str:
        """Get current market regime."""
        return self._last_regime
