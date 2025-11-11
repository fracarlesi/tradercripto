"""
Orchestrator Module - Coordinates all microservices for unified JSON market data.

This module provides:
- JSON schema definitions (TypedDict)
- Market data aggregation and validation
- Unified caching layer
- Parallel microservice execution
"""

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
)

from .json_builder import MarketDataBuilder
from .cache_manager import CacheManager

__all__ = [
    # Schema types
    "MarketDataSnapshot",
    "SymbolData",
    "TechnicalAnalysis",
    "PivotPoints",
    "ProphetForecast",
    "GlobalIndicators",
    "PortfolioState",
    "Sentiment",
    "WhaleAlert",
    "NewsArticle",
    "Position",
    "StrategyWeights",
    "Metadata",
    # Builder
    "MarketDataBuilder",
    # Cache
    "CacheManager",
]
