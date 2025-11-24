"""
Price Forecaster - Simple ML-free price predictions based on momentum.

This service provides short-term price forecasts (15min, 1h, 6h) using:
- Momentum extrapolation from recent candles
- Linear regression on price data
- Volatility-adjusted confidence intervals

No external ML libraries needed - pure Python with numpy for math.

Key Features:
- 15min forecast: Based on micro-momentum (last 3 candles)
- 1h forecast: Based on hourly momentum (last 6 candles)
- 6h forecast: Based on medium-term trend (last 24 candles)
- Confidence score: Based on trend consistency and volatility
"""

import logging
import numpy as np
from typing import Dict, List, Optional, Literal
from datetime import datetime

logger = logging.getLogger(__name__)


def calculate_forecast(
    candles: List[Dict],
    current_price: float,
    candle_interval: str = "15m",
) -> Optional[Dict]:
    """
    Calculate price forecasts for 15min, 1h, and 6h horizons.

    Args:
        candles: List of OHLCV candles (most recent first)
                 Format: [{"o": open, "h": high, "l": low, "c": close, "v": volume, "t": timestamp}, ...]
        current_price: Current market price
        candle_interval: Candle timeframe ("15m" or "1h")

    Returns:
        Forecast dict or None if insufficient data:
        {
            "current_price": 50000.0,
            "forecast_15min": 50050.0,
            "forecast_1h": 50200.0,
            "forecast_6h": 51000.0,
            "change_pct_15min": 0.1,
            "change_pct_1h": 0.4,
            "change_pct_6h": 2.0,
            "trend": "up",
            "confidence": 0.75,
            "confidence_interval_1h": [49800.0, 50600.0],
        }
    """
    # Adjust candle counts based on interval
    # 15m: 4 candles = 1 hour, 24 candles = 6 hours, 96 candles = 24 hours
    # 1h: 1 candle = 1 hour, 6 candles = 6 hours, 24 candles = 24 hours
    if candle_interval == "15m":
        min_candles = 4  # 1 hour of data minimum
        candles_per_hour = 4
        candles_1h = 4
        candles_6h = 24
        candles_24h = 96
    else:  # 1h
        min_candles = 2
        candles_per_hour = 1
        candles_1h = 1
        candles_6h = 6
        candles_24h = 24

    if not candles or len(candles) < min_candles:
        return None

    try:
        # Extract close prices (most recent first)
        max_candles = min(len(candles), candles_24h)
        closes = [float(c.get("c", 0)) for c in candles[:max_candles] if c.get("c")]

        if len(closes) < min_candles:
            return None

        # Reverse to have oldest first for regression
        closes = closes[::-1]

        # Calculate 15min forecast (next candle for 15m, extrapolate for 1h)
        if candle_interval == "15m" and len(closes) >= 4:
            # Use last 4 candles (1 hour) to predict next 15min
            x = np.arange(4)
            y = np.array(closes[-4:])
            slope, _ = np.polyfit(x, y, 1)
            forecast_15min = current_price + slope
        else:
            # Extrapolate from hourly momentum
            momentum_1h = (closes[-1] - closes[-2]) / closes[-2] if len(closes) >= 2 and closes[-2] > 0 else 0
            forecast_15min = current_price * (1 + momentum_1h / 4)

        # Calculate 1h forecast
        if len(closes) >= candles_1h * 2:
            x = np.arange(candles_1h * 2)
            y = np.array(closes[-(candles_1h * 2):])
            slope, _ = np.polyfit(x, y, 1)
            forecast_1h = current_price + (slope * candles_1h)
        else:
            forecast_1h = current_price

        # Calculate 6h forecast
        if len(closes) >= candles_6h:
            x = np.arange(candles_6h)
            y = np.array(closes[-candles_6h:])
            slope, _ = np.polyfit(x, y, 1)
            forecast_6h = current_price + (slope * candles_6h)
        else:
            # Use available data with extrapolation
            x = np.arange(len(closes))
            y = np.array(closes)
            slope, _ = np.polyfit(x, y, 1)
            forecast_6h = current_price + (slope * candles_6h)

        # Calculate percent changes
        change_pct_15min = ((forecast_15min - current_price) / current_price) * 100
        change_pct_1h = ((forecast_1h - current_price) / current_price) * 100
        change_pct_6h = ((forecast_6h - current_price) / current_price) * 100

        # Determine trend
        if change_pct_1h > 0.5:
            trend: Literal["up", "down", "neutral"] = "up"
        elif change_pct_1h < -0.5:
            trend = "down"
        else:
            trend = "neutral"

        # Calculate confidence based on trend consistency
        # Higher confidence if recent candles are all in same direction
        recent_moves = [closes[i] - closes[i-1] for i in range(1, min(6, len(closes)))]
        positive_moves = sum(1 for m in recent_moves if m > 0)
        negative_moves = sum(1 for m in recent_moves if m < 0)

        # Confidence = how consistent is the direction
        direction_consistency = max(positive_moves, negative_moves) / len(recent_moves) if recent_moves else 0

        # Also factor in volatility (lower volatility = higher confidence)
        if len(closes) >= 6:
            returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes)) if closes[i-1] > 0]
            volatility = np.std(returns) if returns else 0.1
            volatility_factor = max(0.3, 1 - volatility * 10)  # High vol reduces confidence
        else:
            volatility_factor = 0.5

        confidence = min(0.95, direction_consistency * 0.6 + volatility_factor * 0.4)

        # Calculate confidence interval for 1h (based on ATR-like measure)
        if len(closes) >= 6:
            high_prices = [float(c.get("h", 0)) for c in candles[:6] if c.get("h")]
            low_prices = [float(c.get("l", 0)) for c in candles[:6] if c.get("l")]
            if high_prices and low_prices:
                avg_range = np.mean([h - l for h, l in zip(high_prices, low_prices)])
            else:
                avg_range = current_price * 0.01  # Default 1%
        else:
            avg_range = current_price * 0.01

        confidence_interval_1h = [
            round(forecast_1h - avg_range, 4),
            round(forecast_1h + avg_range, 4),
        ]

        return {
            "current_price": round(current_price, 4),
            "forecast_15min": round(forecast_15min, 4),
            "forecast_1h": round(forecast_1h, 4),
            "forecast_6h": round(forecast_6h, 4),
            "change_pct_15min": round(change_pct_15min, 3),
            "change_pct_1h": round(change_pct_1h, 3),
            "change_pct_6h": round(change_pct_6h, 3),
            "trend": trend,
            "confidence": round(confidence, 3),
            "confidence_interval_1h": confidence_interval_1h,
        }

    except Exception as e:
        logger.error(f"Forecast calculation failed: {e}", exc_info=True)
        return None


async def get_forecasts_for_symbols(
    symbols: List[str],
    prices: Dict[str, float],
) -> Dict[str, Dict]:
    """
    Calculate forecasts for multiple symbols.

    Args:
        symbols: List of symbols to forecast
        prices: Current prices dict

    Returns:
        Dict mapping symbol to forecast:
        {
            "BTC": {...forecast...},
            "ETH": {...forecast...},
        }
    """
    from services.market_data.websocket_candle_service import get_websocket_candle_service

    ws_service = get_websocket_candle_service()
    forecasts = {}

    for symbol in symbols:
        try:
            # Get candles from WebSocket cache
            candles = ws_service.get_candles(symbol, limit=24)
            current_price = prices.get(symbol, 0)

            if not candles or current_price <= 0:
                continue

            forecast = calculate_forecast(candles, current_price)
            if forecast:
                forecasts[symbol] = forecast

        except Exception as e:
            logger.warning(f"Forecast failed for {symbol}: {e}")
            continue

    logger.info(f"Generated forecasts for {len(forecasts)}/{len(symbols)} symbols")
    return forecasts


def get_forecast_structured(
    symbol: str,
    candles: List[Dict],
    current_price: float,
    candle_interval: str = "15m",
) -> Optional[Dict]:
    """
    Get forecast in structured format for JSON builder.

    Matches the ProphetForecast schema structure with added 15min forecast.

    Args:
        symbol: Symbol name
        candles: OHLCV candles (most recent first)
        current_price: Current price
        candle_interval: Candle timeframe ("15m" or "1h")

    Returns:
        Dict matching ProphetForecast schema (with extended fields):
        {
            "current_price": 50000.0,
            "forecast_15min": 50050.0,
            "forecast_6h": 50500.0,
            "forecast_24h": 51000.0,  # Extrapolated from 6h
            "change_pct_15min": 0.1,
            "change_pct_6h": 1.0,
            "change_pct_24h": 2.0,
            "trend": "up",
            "confidence": 0.75,
            "confidence_interval_24h": [49000.0, 53000.0],
        }
    """
    forecast = calculate_forecast(candles, current_price, candle_interval)

    if not forecast:
        return None

    # Extrapolate 24h from 6h trend
    change_6h = forecast["change_pct_6h"]
    change_24h = change_6h * 4  # Simple extrapolation
    forecast_24h = current_price * (1 + change_24h / 100)

    # Widen confidence interval for 24h
    ci_1h = forecast["confidence_interval_1h"]
    range_1h = ci_1h[1] - ci_1h[0]
    confidence_interval_24h = [
        round(forecast_24h - range_1h * 4, 4),
        round(forecast_24h + range_1h * 4, 4),
    ]

    return {
        "current_price": forecast["current_price"],
        "forecast_15min": forecast["forecast_15min"],
        "forecast_6h": forecast["forecast_6h"],
        "forecast_24h": round(forecast_24h, 4),
        "change_pct_15min": forecast["change_pct_15min"],
        "change_pct_6h": forecast["change_pct_6h"],
        "change_pct_24h": round(change_24h, 3),
        "trend": forecast["trend"],
        "confidence": forecast["confidence"],
        "confidence_interval_24h": confidence_interval_24h,
    }
