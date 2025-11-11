"""
JSON Schema Definitions for Market Data Snapshot.

Uses Python 3.11+ TypedDict for type safety and validation.
All schemas map directly to the JSON structure sent to DeepSeek.
"""

from typing import TypedDict, List, Literal, Optional
from datetime import datetime


# ============================================
# Technical Analysis Types
# ============================================

class TechnicalAnalysis(TypedDict):
    """Technical analysis indicators (momentum + support)."""
    score: float  # 0.0-1.0 combined score
    momentum: float  # 0.0-1.0 trend strength
    support: float  # 0.0-1.0 support quality
    signal: Literal["STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"]
    rank: int  # 1-142 (1 = best)


# ============================================
# Pivot Points Types
# ============================================

class PivotPoints(TypedDict):
    """Pivot points for support/resistance analysis."""
    PP: float  # Pivot point
    R1: float  # Resistance 1
    R2: float  # Resistance 2
    R3: float  # Resistance 3
    S1: float  # Support 1
    S2: float  # Support 2
    S3: float  # Support 3
    current_zone: Literal["above_R1", "bullish", "neutral", "bearish", "below_S1"]
    signal: Literal["long_opportunity", "short_opportunity", "bullish_zone", "bearish_zone", "neutral"]
    distance_to_support_pct: float  # Negative if below support
    distance_to_resistance_pct: float  # Negative if below resistance


# ============================================
# Prophet Forecast Types
# ============================================

class ProphetForecast(TypedDict):
    """Prophet ML price forecast (6h and 24h ahead)."""
    current_price: float
    forecast_6h: float
    forecast_24h: float
    change_pct_6h: float  # Expected % change in 6h
    change_pct_24h: float  # Expected % change in 24h
    trend: Literal["up", "down", "neutral"]
    confidence: float  # 0.0-1.0
    confidence_interval_24h: List[float]  # [lower, upper] bounds


# ============================================
# Market Data Types
# ============================================

class MarketData(TypedDict):
    """Additional market data for symbol."""
    volume_24h: Optional[float]
    market_cap: Optional[float]
    rank_by_market_cap: Optional[int]


# ============================================
# Symbol Data (Complete)
# ============================================

class SymbolData(TypedDict):
    """
    Complete data for a single symbol.

    Contains all indicators from all microservices.
    """
    symbol: str  # e.g., "BTC"
    price: float  # Current market price
    technical_analysis: TechnicalAnalysis
    pivot_points: PivotPoints
    prophet_forecast: Optional[ProphetForecast]  # None if not in top symbols
    market_data: MarketData


# ============================================
# Global Indicators Types
# ============================================

class Sentiment(TypedDict):
    """Fear & Greed sentiment index (global)."""
    value: int  # 0-100
    label: Literal["EXTREME_FEAR", "FEAR", "NEUTRAL", "GREED", "EXTREME_GREED"]
    signal: Literal["contrarian_long", "contrarian_short", "neutral"]
    last_updated: str  # ISO 8601 timestamp


class WhaleAlert(TypedDict):
    """Large transaction alert (>$10M)."""
    symbol: str
    amount_usd: float
    transaction_type: Literal["transfer", "exchange_inflow", "exchange_outflow"]
    from_address: str
    to_address: str
    timestamp: str  # ISO 8601
    signal: Literal["sell_pressure", "buy_pressure", "neutral"]


class NewsArticle(TypedDict):
    """News article from CoinJournal or other sources."""
    headline: str
    summary: Optional[str]
    url: str
    published_at: str  # ISO 8601
    sentiment: Optional[Literal["positive", "neutral", "negative"]]
    mentioned_symbols: List[str]


class GlobalIndicators(TypedDict):
    """Global market indicators (not per-symbol)."""
    sentiment: Sentiment
    whale_alerts: List[WhaleAlert]
    news: List[NewsArticle]


# ============================================
# Portfolio Types
# ============================================

class Position(TypedDict):
    """Open trading position."""
    symbol: str
    quantity: float
    side: Literal["LONG", "SHORT"]
    entry_price: float
    current_price: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    market_value: float


class StrategyWeights(TypedDict):
    """Indicator weights for AI decision-making."""
    prophet: float  # 0.0-1.0
    pivot_points: float
    technical_analysis: float
    whale_alerts: float
    sentiment: float
    news: float


class PortfolioState(TypedDict):
    """Current portfolio state."""
    total_assets: float
    available_cash: float
    positions_value: float
    unrealized_pnl: float
    positions: List[Position]
    strategy_weights: StrategyWeights


# ============================================
# Metadata Types
# ============================================

class Metadata(TypedDict):
    """Snapshot metadata."""
    timestamp: str  # ISO 8601
    version: str  # Schema version (e.g., "2.0.0")
    symbols_analyzed: int  # Total symbols with data
    cycle_duration_ms: int  # Time to generate snapshot


# ============================================
# Complete Market Data Snapshot
# ============================================

class MarketDataSnapshot(TypedDict):
    """
    Complete market data snapshot sent to DeepSeek AI.

    Contains:
    - Metadata about snapshot generation
    - Per-symbol data (technical, pivot, prophet)
    - Global indicators (sentiment, whale, news)
    - Portfolio state and strategy weights

    This structure replaces the narrative prompt with structured JSON.
    """
    metadata: Metadata
    symbols: List[SymbolData]  # 142 symbols with complete data
    global_indicators: GlobalIndicators
    portfolio: PortfolioState


# ============================================
# Validation Helpers
# ============================================

def validate_snapshot(snapshot: MarketDataSnapshot) -> bool:
    """
    Validate market data snapshot structure.

    Checks:
    - Required fields present
    - Value ranges (e.g., scores 0-1)
    - Data types correct

    Args:
        snapshot: Market data snapshot to validate

    Returns:
        True if valid, raises ValueError otherwise

    Raises:
        ValueError: If validation fails with detailed error message
    """
    # Check metadata
    if not snapshot.get("metadata"):
        raise ValueError("Missing metadata")

    metadata = snapshot["metadata"]
    if not isinstance(metadata["symbols_analyzed"], int) or metadata["symbols_analyzed"] < 0:
        raise ValueError(f"Invalid symbols_analyzed: {metadata['symbols_analyzed']}")

    # Check symbols array
    if not snapshot.get("symbols"):
        raise ValueError("Missing symbols array")

    if len(snapshot["symbols"]) != metadata["symbols_analyzed"]:
        raise ValueError(
            f"Symbols count mismatch: metadata says {metadata['symbols_analyzed']}, "
            f"but got {len(snapshot['symbols'])} symbols"
        )

    # Validate each symbol
    for i, symbol_data in enumerate(snapshot["symbols"]):
        _validate_symbol_data(symbol_data, index=i)

    # Check global indicators
    if not snapshot.get("global_indicators"):
        raise ValueError("Missing global_indicators")

    # Check portfolio
    if not snapshot.get("portfolio"):
        raise ValueError("Missing portfolio")

    return True


def _validate_symbol_data(symbol_data: SymbolData, index: int) -> None:
    """Validate a single symbol's data."""
    symbol = symbol_data.get("symbol", f"<symbol at index {index}>")

    # Check technical analysis
    tech = symbol_data.get("technical_analysis")
    if not tech:
        raise ValueError(f"{symbol}: Missing technical_analysis")

    if not (0 <= tech["score"] <= 1):
        raise ValueError(f"{symbol}: Invalid technical score {tech['score']} (must be 0-1)")

    if not (0 <= tech["momentum"] <= 1):
        raise ValueError(f"{symbol}: Invalid momentum {tech['momentum']} (must be 0-1)")

    if not (0 <= tech["support"] <= 1):
        raise ValueError(f"{symbol}: Invalid support {tech['support']} (must be 0-1)")

    # Check pivot points
    pivots = symbol_data.get("pivot_points")
    if not pivots:
        raise ValueError(f"{symbol}: Missing pivot_points")

    # Pivot levels must be ordered: S3 < S2 < S1 < PP < R1 < R2 < R3
    levels = [pivots["S3"], pivots["S2"], pivots["S1"], pivots["PP"], pivots["R1"], pivots["R2"], pivots["R3"]]
    if levels != sorted(levels):
        raise ValueError(f"{symbol}: Pivot levels not properly ordered")

    # Check prophet forecast (if present)
    prophet = symbol_data.get("prophet_forecast")
    if prophet:
        if not (0 <= prophet["confidence"] <= 1):
            raise ValueError(f"{symbol}: Invalid prophet confidence {prophet['confidence']} (must be 0-1)")

    # Check price is positive
    if symbol_data["price"] <= 0:
        raise ValueError(f"{symbol}: Invalid price {symbol_data['price']} (must be positive)")
