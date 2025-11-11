"""
Market Data JSON Builder - Aggregates data from all microservices.

Coordinates data collection from:
- Technical Analysis (momentum + support)
- Pivot Points (support/resistance)
- Prophet Forecaster (ML predictions)
- Sentiment Tracker (Fear & Greed)
- Whale Tracker (large transactions)
- News Feed (headlines)
- Portfolio Manager (positions, cash)

Output: Complete MarketDataSnapshot JSON for DeepSeek AI
"""

import logging
import time
from datetime import datetime
from typing import Dict, List, Optional

from .schemas import (
    MarketDataSnapshot,
    SymbolData,
    TechnicalAnalysis,
    PivotPoints,
    ProphetForecast,
    GlobalIndicators,
    PortfolioState,
    Sentiment,
    WhaleAlert,
    NewsArticle,
    Position,
    StrategyWeights,
    Metadata,
    MarketData,
    validate_snapshot,
)

logger = logging.getLogger(__name__)


class MarketDataBuilder:
    """
    Builder for complete market data snapshot.

    Usage:
        builder = MarketDataBuilder()
        builder.set_prices(prices_dict)
        builder.set_technical_analysis(technical_results)
        builder.set_pivot_points(pivot_results)
        # ... set other data sources
        snapshot = builder.build()
    """

    def __init__(self):
        """Initialize empty builder."""
        self._prices: Dict[str, float] = {}
        self._technical: Dict[str, dict] = {}
        self._pivots: Dict[str, dict] = {}
        self._prophet: Dict[str, dict] = {}
        self._sentiment: Optional[dict] = None
        self._whale_alerts: List[dict] = []
        self._news: List[dict] = []
        self._portfolio: Optional[dict] = None
        self._start_time = time.time()

    def set_prices(self, prices: Dict[str, float]) -> "MarketDataBuilder":
        """
        Set market prices for all symbols.

        Args:
            prices: Dictionary mapping symbol to price

        Returns:
            Self for method chaining
        """
        self._prices = prices
        logger.debug(f"Set prices for {len(prices)} symbols")
        return self

    def set_technical_analysis(self, technical_results: Dict[str, dict]) -> "MarketDataBuilder":
        """
        Set technical analysis results.

        Expected format:
            {
                "BTC": {
                    "score": 0.75,
                    "momentum": 0.72,
                    "support": 0.78,
                    "signal": "BUY",
                    "rank": 5
                },
                ...
            }

        Args:
            technical_results: Technical analysis for all symbols

        Returns:
            Self for method chaining
        """
        self._technical = technical_results
        logger.debug(f"Set technical analysis for {len(technical_results)} symbols")
        return self

    def set_pivot_points(self, pivot_results: Dict[str, dict]) -> "MarketDataBuilder":
        """
        Set pivot points results.

        Expected format:
            {
                "BTC": {
                    "PP": 101200,
                    "R1": 103700,
                    "S1": 98700,
                    "signal": "bullish_zone",
                    ...
                },
                ...
            }

        Args:
            pivot_results: Pivot points for all symbols

        Returns:
            Self for method chaining
        """
        self._pivots = pivot_results
        logger.debug(f"Set pivot points for {len(pivot_results)} symbols")
        return self

    def set_prophet_forecasts(self, prophet_results: Dict[str, dict]) -> "MarketDataBuilder":
        """
        Set Prophet ML forecasts.

        Expected format:
            {
                "BTC": {
                    "current_price": 102450.0,
                    "forecast_24h": 103120.0,
                    "confidence": 0.885,
                    ...
                },
                ...
            }

        Args:
            prophet_results: Prophet forecasts (may be subset of symbols)

        Returns:
            Self for method chaining
        """
        self._prophet = prophet_results
        logger.debug(f"Set Prophet forecasts for {len(prophet_results)} symbols")
        return self

    def set_sentiment(self, sentiment: dict) -> "MarketDataBuilder":
        """
        Set global sentiment index.

        Expected format:
            {
                "value": 68,
                "label": "GREED",
                "signal": "contrarian_short",
                "last_updated": "2025-11-10T14:30:00Z"
            }

        Args:
            sentiment: Fear & Greed sentiment data

        Returns:
            Self for method chaining
        """
        self._sentiment = sentiment
        logger.debug(f"Set sentiment: {sentiment.get('label', 'unknown')}")
        return self

    def set_whale_alerts(self, whale_alerts: List[dict]) -> "MarketDataBuilder":
        """
        Set whale transaction alerts.

        Expected format:
            [
                {
                    "symbol": "BTC",
                    "amount_usd": 15200000,
                    "transaction_type": "transfer",
                    ...
                },
                ...
            ]

        Args:
            whale_alerts: List of large transactions

        Returns:
            Self for method chaining
        """
        self._whale_alerts = whale_alerts
        logger.debug(f"Set {len(whale_alerts)} whale alerts")
        return self

    def set_news(self, news: List[dict]) -> "MarketDataBuilder":
        """
        Set news articles.

        Expected format:
            [
                {
                    "headline": "Bitcoin ETF sees $2.3B inflows",
                    "url": "https://...",
                    "published_at": "2025-11-10T13:00:00Z",
                    ...
                },
                ...
            ]

        Args:
            news: List of news articles

        Returns:
            Self for method chaining
        """
        self._news = news
        logger.debug(f"Set {len(news)} news articles")
        return self

    def set_portfolio(self, portfolio: dict) -> "MarketDataBuilder":
        """
        Set portfolio state.

        Expected format:
            {
                "total_assets": 10000.0,
                "available_cash": 8000.0,
                "positions": [...],
                "strategy_weights": {...}
            }

        Args:
            portfolio: Current portfolio state

        Returns:
            Self for method chaining
        """
        self._portfolio = portfolio
        logger.debug(f"Set portfolio: ${portfolio.get('total_assets', 0):.2f}")
        return self

    def build(self, validate: bool = True) -> MarketDataSnapshot:
        """
        Build complete market data snapshot.

        Aggregates all data sources into unified JSON structure.

        Args:
            validate: If True, validate snapshot structure (recommended)

        Returns:
            Complete MarketDataSnapshot

        Raises:
            ValueError: If required data is missing or invalid
        """
        logger.info("Building market data snapshot...")

        # Check required data
        if not self._prices:
            raise ValueError("Prices not set - call set_prices() first")

        if not self._technical:
            raise ValueError("Technical analysis not set - call set_technical_analysis() first")

        if not self._portfolio:
            raise ValueError("Portfolio not set - call set_portfolio() first")

        # Build symbols array (only symbols with technical data)
        symbols_data = self._build_symbols_data()

        # Build global indicators
        global_indicators = self._build_global_indicators()

        # Build portfolio
        portfolio = self._build_portfolio()

        # Build metadata
        metadata = self._build_metadata(len(symbols_data))

        # Assemble snapshot
        snapshot: MarketDataSnapshot = {
            "metadata": metadata,
            "symbols": symbols_data,
            "global_indicators": global_indicators,
            "portfolio": portfolio,
        }

        # Validate if requested
        if validate:
            try:
                validate_snapshot(snapshot)
                logger.info("✅ Snapshot validation passed")
            except ValueError as e:
                logger.error(f"❌ Snapshot validation failed: {e}", exc_info=True)
                raise

        logger.info(
            f"✅ Built snapshot: {len(symbols_data)} symbols, "
            f"{len(self._news)} news, {len(self._whale_alerts)} whale alerts"
        )

        return snapshot

    def _build_symbols_data(self) -> List[SymbolData]:
        """Build symbols array with complete data for each symbol."""
        symbols_data: List[SymbolData] = []

        # Iterate over symbols with technical analysis (142 symbols)
        for symbol, tech in self._technical.items():
            # Get price
            price = self._prices.get(symbol, 0.0)
            if price <= 0:
                logger.warning(f"Skipping {symbol}: invalid price {price}")
                continue

            # Get pivot points
            pivot = self._pivots.get(symbol)
            if not pivot:
                logger.warning(f"Skipping {symbol}: no pivot points data")
                continue

            # Get prophet forecast (may be None)
            prophet = self._prophet.get(symbol)

            # Build symbol data
            symbol_data: SymbolData = {
                "symbol": symbol,
                "price": price,
                "technical_analysis": TechnicalAnalysis(
                    score=tech["score"],
                    momentum=tech["momentum"],
                    support=tech["support"],
                    signal=tech["signal"],
                    rank=tech["rank"],
                ),
                "pivot_points": PivotPoints(
                    PP=pivot["PP"],
                    R1=pivot["R1"],
                    R2=pivot["R2"],
                    R3=pivot["R3"],
                    S1=pivot["S1"],
                    S2=pivot["S2"],
                    S3=pivot["S3"],
                    current_zone=pivot["current_zone"],
                    signal=pivot["signal"],
                    distance_to_support_pct=pivot["distance_to_support_pct"],
                    distance_to_resistance_pct=pivot["distance_to_resistance_pct"],
                ),
                "prophet_forecast": (
                    ProphetForecast(
                        current_price=prophet["current_price"],
                        forecast_6h=prophet["forecast_6h"],
                        forecast_24h=prophet["forecast_24h"],
                        change_pct_6h=prophet["change_pct_6h"],
                        change_pct_24h=prophet["change_pct_24h"],
                        trend=prophet["trend"],
                        confidence=prophet["confidence"],
                        confidence_interval_24h=prophet["confidence_interval_24h"],
                    )
                    if prophet
                    else None
                ),
                "market_data": MarketData(
                    volume_24h=None,  # TODO: Add volume data
                    market_cap=None,  # TODO: Add market cap data
                    rank_by_market_cap=None,  # TODO: Add ranking
                ),
            }

            symbols_data.append(symbol_data)

        # Sort by technical analysis rank (best first)
        symbols_data.sort(key=lambda x: x["technical_analysis"]["rank"])

        return symbols_data

    def _build_global_indicators(self) -> GlobalIndicators:
        """Build global indicators section."""
        # Sentiment (required)
        if not self._sentiment:
            logger.warning("No sentiment data - using default")
            sentiment = Sentiment(
                value=50,
                label="NEUTRAL",
                signal="neutral",
                last_updated=datetime.utcnow().isoformat(),
            )
        else:
            sentiment = Sentiment(
                value=self._sentiment["value"],
                label=self._sentiment["label"],
                signal=self._sentiment["signal"],
                last_updated=self._sentiment.get("last_updated", datetime.utcnow().isoformat()),
            )

        # Whale alerts
        whale_alerts = [
            WhaleAlert(
                symbol=alert["symbol"],
                amount_usd=alert["amount_usd"],
                transaction_type=alert["transaction_type"],
                from_address=alert["from_address"],
                to_address=alert["to_address"],
                timestamp=alert["timestamp"],
                signal=alert["signal"],
            )
            for alert in self._whale_alerts
        ]

        # News
        news = [
            NewsArticle(
                headline=article["headline"],
                summary=article.get("summary"),
                url=article["url"],
                published_at=article["published_at"],
                sentiment=article.get("sentiment"),
                mentioned_symbols=article.get("mentioned_symbols", []),
            )
            for article in self._news
        ]

        return GlobalIndicators(
            sentiment=sentiment,
            whale_alerts=whale_alerts,
            news=news,
        )

    def _build_portfolio(self) -> PortfolioState:
        """Build portfolio state section."""
        # Convert positions
        positions = [
            Position(
                symbol=pos["symbol"],
                quantity=pos["quantity"],
                side=pos["side"],
                entry_price=pos["entry_price"],
                current_price=pos["current_price"],
                unrealized_pnl=pos["unrealized_pnl"],
                unrealized_pnl_pct=pos["unrealized_pnl_pct"],
                market_value=pos["market_value"],
            )
            for pos in self._portfolio.get("positions", [])
        ]

        # Strategy weights
        weights = self._portfolio.get("strategy_weights", {})
        strategy_weights = StrategyWeights(
            prophet=weights.get("prophet", 0.5),
            pivot_points=weights.get("pivot_points", 0.8),
            technical_analysis=weights.get("technical_analysis", 0.7),
            whale_alerts=weights.get("whale_alerts", 0.4),
            sentiment=weights.get("sentiment", 0.3),
            news=weights.get("news", 0.2),
        )

        return PortfolioState(
            total_assets=self._portfolio.get("total_assets", 0.0),
            available_cash=self._portfolio.get("available_cash", 0.0),
            positions_value=self._portfolio.get("positions_value", 0.0),
            unrealized_pnl=self._portfolio.get("unrealized_pnl", 0.0),
            positions=positions,
            strategy_weights=strategy_weights,
        )

    def _build_metadata(self, symbols_analyzed: int) -> Metadata:
        """Build metadata section."""
        cycle_duration_ms = int((time.time() - self._start_time) * 1000)

        return Metadata(
            timestamp=datetime.utcnow().isoformat(),
            version="2.0.0",  # Schema version
            symbols_analyzed=symbols_analyzed,
            cycle_duration_ms=cycle_duration_ms,
        )
