"""
New Token Detection Service - Analyze ultra-new tokens with <7 days of data

This service detects and scores newly listed tokens (3-7 days) that would be
filtered out by standard technical analysis (requires 70+ days).

Strategy:
- Volume Spike Detection: Identify abnormal volume (listing hype)
- Price Velocity: Calculate momentum even with minimal data
- Volatility: High volatility = opportunity (and risk)
- Listing Premium: New tokens often pump in first week

Risk: HIGH - Very limited data, high volatility, recommendation requires
      explicit DeepSeek approval before execution.
"""

import logging
from typing import Any

import pandas as pd

from services.market_data.hyperliquid_market_data import get_kline_data_from_hyperliquid

logger = logging.getLogger(__name__)


def detect_new_tokens(symbols: list[str], min_days: int = 3, max_days: int = 7) -> dict[str, Any]:
    """
    Detect and score newly listed tokens with 3-7 days of historical data.

    Args:
        symbols: List of all available symbols
        min_days: Minimum days required (default: 3)
        max_days: Maximum days to consider "new" (default: 7)

    Returns:
        Dictionary with structure:
        {
            "new_tokens": [
                {
                    "symbol": "MKR",
                    "days_available": 3,
                    "score": 0.75,
                    "volume_spike": 3.2,  # 3.2x average
                    "price_velocity": 0.15,  # +15% per day
                    "volatility": 0.08,  # 8% daily std dev
                    "risk_level": "EXTREME"
                },
                ...
            ]
        }
    """
    logger.info(f"🔍 Scanning for new tokens ({min_days}-{max_days} days old)...")

    new_tokens = []

    for symbol in symbols:
        try:
            # Fetch up to 10 days to check availability
            klines = get_kline_data_from_hyperliquid(symbol, period="1d", count=10)

            if not klines or len(klines) < min_days:
                continue

            days_available = len(klines)

            # Only process tokens with 3-7 days of data
            if days_available < min_days or days_available > max_days:
                continue

            # Convert to DataFrame for analysis
            df = pd.DataFrame(klines)
            df['close'] = df['close'].astype(float)
            df['volume'] = df['volume'].astype(float)

            # Calculate metrics
            volume_spike = _calculate_volume_spike(df)
            price_velocity = _calculate_price_velocity(df)
            volatility = _calculate_volatility(df)

            # Calculate combined score
            # Higher weight on volume (indicates real interest)
            score = (
                min(volume_spike / 3.0, 1.0) * 0.5 +  # Volume spike (capped at 3x)
                min(abs(price_velocity) * 5, 1.0) * 0.3 +  # Price movement
                min(volatility * 10, 1.0) * 0.2  # Volatility
            )

            # Listing premium bonus (newer = higher bonus)
            listing_bonus = (max_days - days_available) / max_days * 0.3
            score += listing_bonus

            # Cap at 1.0
            score = min(score, 1.0)

            # Determine risk level
            if days_available <= 3:
                risk_level = "EXTREME"
            elif days_available <= 5:
                risk_level = "VERY_HIGH"
            else:
                risk_level = "HIGH"

            new_tokens.append({
                "symbol": symbol,
                "days_available": days_available,
                "score": round(score, 4),
                "volume_spike": round(volume_spike, 2),
                "price_velocity": round(price_velocity, 4),
                "volatility": round(volatility, 4),
                "risk_level": risk_level,
                "current_price": float(df['close'].iloc[-1])
            })

            logger.info(
                f"🆕 Found new token: {symbol} ({days_available} days) - "
                f"score={score:.3f}, volume_spike={volume_spike:.2f}x, "
                f"velocity={price_velocity:.2%}/day"
            )

        except Exception as e:
            logger.debug(f"Skipped {symbol}: {str(e)[:50]}")
            continue

    # Sort by score (highest first)
    new_tokens.sort(key=lambda x: x['score'], reverse=True)

    logger.info(
        f"✅ Found {len(new_tokens)} new tokens with {min_days}-{max_days} days of data"
    )

    # Log top 3
    if new_tokens:
        logger.info("🏆 Top 3 new token signals:")
        for i, token in enumerate(new_tokens[:3], 1):
            logger.info(
                f"  {i}. {token['symbol']}: score={token['score']:.3f} "
                f"({token['days_available']} days, {token['risk_level']} risk)"
            )

    return {"new_tokens": new_tokens}


def _calculate_volume_spike(df: pd.DataFrame) -> float:
    """
    Calculate volume spike ratio (current vs average).

    Returns: Ratio (e.g., 3.2 = current volume is 3.2x average)
    """
    if len(df) < 2:
        return 1.0

    current_volume = df['volume'].iloc[-1]
    avg_volume = df['volume'].iloc[:-1].mean()  # Exclude current day

    if avg_volume == 0:
        return 1.0

    return current_volume / avg_volume


def _calculate_price_velocity(df: pd.DataFrame) -> float:
    """
    Calculate price velocity (% change per day).

    Returns: Daily percentage change (e.g., 0.15 = +15% per day)
    """
    if len(df) < 2:
        return 0.0

    first_price = df['close'].iloc[0]
    last_price = df['close'].iloc[-1]
    days = len(df) - 1

    if first_price == 0 or days == 0:
        return 0.0

    # Average daily percentage change
    total_change = (last_price - first_price) / first_price
    daily_velocity = total_change / days

    return daily_velocity


def _calculate_volatility(df: pd.DataFrame) -> float:
    """
    Calculate price volatility (standard deviation / mean).

    Returns: Coefficient of variation (e.g., 0.08 = 8% daily std dev)
    """
    if len(df) < 2:
        return 0.0

    prices = df['close']
    mean_price = prices.mean()
    std_dev = prices.std()

    if mean_price == 0:
        return 0.0

    return std_dev / mean_price


def format_new_tokens_for_ai(new_tokens_data: dict[str, Any]) -> str:
    """
    Format new token signals for AI prompt.

    Args:
        new_tokens_data: Output from detect_new_tokens()

    Returns:
        Formatted string for AI prompt
    """
    tokens = new_tokens_data.get("new_tokens", [])

    if not tokens:
        return ""

    lines = []
    lines.append("\n🆕 NEW TOKEN ALERTS (3-7 days old, HIGH RISK):")
    lines.append("")

    for i, token in enumerate(tokens[:5], 1):  # Top 5 only
        symbol = token['symbol']
        score = token['score']
        days = token['days_available']
        volume_spike = token['volume_spike']
        velocity = token['price_velocity']
        risk = token['risk_level']

        # Format velocity as percentage
        velocity_pct = velocity * 100
        velocity_sign = "+" if velocity > 0 else ""

        lines.append(
            f"{i}. {symbol}: score={score:.2f} ({days} days, {risk} RISK)"
        )
        lines.append(
            f"   Volume: {volume_spike:.1f}x average, "
            f"Momentum: {velocity_sign}{velocity_pct:.1f}%/day"
        )

    lines.append("")
    lines.append("⚠️  WARNING: These tokens are VERY NEW with limited data.")
    lines.append("⚠️  Only trade if you see STRONG volume + positive momentum.")
    lines.append("⚠️  Use smaller position size (5-10% instead of 20%).")

    return "\n".join(lines)
