"""
EMA Alignment Filter - Trend Confirmation using 25/50/100 EMAs

Based on Ezekiel Chew's strategy:
- EMA 25 (fast): Short-term momentum
- EMA 50 (medium): Intermediate trend
- EMA 100 (slow): Long-term direction

Alignment rules:
- BULLISH: Price > EMA25 > EMA50 > EMA100 (perfect alignment for LONG)
- BEARISH: Price < EMA25 < EMA50 < EMA100 (perfect alignment for SHORT)
- MIXED: EMAs not aligned (stay out or reduce position size)
"""

import logging
from typing import Dict, List, Optional
from dataclasses import dataclass

from services.market_data.websocket_candle_service import get_websocket_candle_service

logger = logging.getLogger(__name__)


@dataclass
class EMAData:
    """EMA alignment data for a symbol."""
    symbol: str
    ema_25: float
    ema_50: float
    ema_100: float
    current_price: float
    alignment: str  # "BULLISH", "BEARISH", "MIXED"
    alignment_score: float  # 0.0-1.0 (1.0 = perfect alignment)
    trend_strength: float  # Distance between EMAs (larger = stronger trend)


def calculate_ema(closes: List[float], period: int) -> float:
    """
    Calculate Exponential Moving Average for given period.

    Formula: EMA = Price(t) * k + EMA(y) * (1 - k)
    where k = 2 / (period + 1)

    Args:
        closes: List of close prices (most recent first)
        period: EMA period (e.g., 25, 50, 100)

    Returns:
        EMA value
    """
    if len(closes) < period:
        # Not enough data - use simple average of available data
        return sum(closes) / len(closes) if closes else 0.0

    # Reverse to chronological order (oldest first)
    prices = list(reversed(closes[:period * 2]))  # Get enough data for smoothing

    if len(prices) < period:
        return sum(prices) / len(prices) if prices else 0.0

    # Calculate multiplier
    k = 2 / (period + 1)

    # Start with SMA for initial EMA
    ema = sum(prices[:period]) / period

    # Apply EMA formula for remaining prices
    for price in prices[period:]:
        ema = price * k + ema * (1 - k)

    return ema


def calculate_ema_alignment(symbol: str, candles: List[dict]) -> Optional[EMAData]:
    """
    Calculate EMA alignment for a symbol.

    Args:
        symbol: Trading symbol (e.g., "BTC")
        candles: List of candles from WebSocket cache (most recent first)

    Returns:
        EMAData with alignment analysis or None if insufficient data
    """
    if not candles or len(candles) < 25:
        logger.debug(f"{symbol}: Insufficient candles ({len(candles) if candles else 0}) for EMA calculation")
        return None

    # Extract close prices
    closes = [float(c['c']) for c in candles]
    current_price = closes[0]

    # Calculate EMAs
    ema_25 = calculate_ema(closes, 25)
    ema_50 = calculate_ema(closes, 50)
    ema_100 = calculate_ema(closes, 100)

    # Determine alignment
    alignment, alignment_score = _determine_alignment(current_price, ema_25, ema_50, ema_100)

    # Calculate trend strength (spread between EMAs)
    trend_strength = _calculate_trend_strength(ema_25, ema_50, ema_100)

    return EMAData(
        symbol=symbol,
        ema_25=ema_25,
        ema_50=ema_50,
        ema_100=ema_100,
        current_price=current_price,
        alignment=alignment,
        alignment_score=alignment_score,
        trend_strength=trend_strength,
    )


def _determine_alignment(price: float, ema_25: float, ema_50: float, ema_100: float) -> tuple[str, float]:
    """
    Determine EMA alignment type and score.

    Returns:
        Tuple of (alignment_type, alignment_score)
    """
    # Check bullish alignment: Price > EMA25 > EMA50 > EMA100
    bullish_conditions = [
        price > ema_25,
        ema_25 > ema_50,
        ema_50 > ema_100,
    ]
    bullish_score = sum(bullish_conditions) / 3

    # Check bearish alignment: Price < EMA25 < EMA50 < EMA100
    bearish_conditions = [
        price < ema_25,
        ema_25 < ema_50,
        ema_50 < ema_100,
    ]
    bearish_score = sum(bearish_conditions) / 3

    # Determine alignment type
    if bullish_score >= 0.9:  # At least 3/3 conditions met
        return "BULLISH", bullish_score
    elif bearish_score >= 0.9:
        return "BEARISH", bearish_score
    elif bullish_score >= 0.67:  # 2/3 conditions met
        return "WEAK_BULLISH", bullish_score * 0.7
    elif bearish_score >= 0.67:
        return "WEAK_BEARISH", bearish_score * 0.7
    else:
        return "MIXED", 0.0


def _calculate_trend_strength(ema_25: float, ema_50: float, ema_100: float) -> float:
    """
    Calculate trend strength based on EMA spread.

    Higher spread = stronger trend momentum.
    Returns normalized value 0-1.
    """
    if ema_100 == 0:
        return 0.0

    # Calculate percentage spread between EMAs
    spread_25_50 = abs(ema_25 - ema_50) / ema_100 * 100
    spread_50_100 = abs(ema_50 - ema_100) / ema_100 * 100

    # Combine spreads (typical strong trend has 1-3% spread)
    total_spread = spread_25_50 + spread_50_100

    # Normalize to 0-1 (5% total spread = 1.0)
    normalized = min(total_spread / 5.0, 1.0)

    return normalized


async def get_ema_alignment_for_symbols(symbols: List[str]) -> Dict[str, EMAData]:
    """
    Get EMA alignment data for multiple symbols.

    Reads from WebSocket candle cache (0 API calls).

    Args:
        symbols: List of trading symbols

    Returns:
        Dict mapping symbol to EMAData
    """
    ws_service = get_websocket_candle_service()
    results: Dict[str, EMAData] = {}

    for symbol in symbols:
        try:
            # Get candles from cache (need at least 100 for EMA100)
            candles = ws_service.get_candles(symbol, limit=120)

            if not candles:
                continue

            ema_data = calculate_ema_alignment(symbol, candles)
            if ema_data:
                results[symbol] = ema_data

        except Exception as e:
            logger.error(f"Failed to calculate EMA for {symbol}: {e}", exc_info=True)
            continue

    logger.info(f"Calculated EMA alignment for {len(results)} symbols")
    return results


async def get_top_momentum_with_ema_filter(
    momentum_coins: List[Dict],
    min_alignment_score: float = 0.67,
) -> List[Dict]:
    """
    Filter momentum coins by EMA alignment.

    Only returns coins where EMA alignment confirms momentum direction:
    - LONG momentum + BULLISH EMA = confirmed
    - SHORT momentum + BEARISH EMA = confirmed

    Args:
        momentum_coins: List from calculate_hourly_momentum()
        min_alignment_score: Minimum alignment score (default 0.67 = 2/3 conditions)

    Returns:
        Filtered list with EMA data added
    """
    if not momentum_coins:
        return []

    symbols = [coin['symbol'] for coin in momentum_coins]
    ema_data = await get_ema_alignment_for_symbols(symbols)

    filtered_coins = []

    for coin in momentum_coins:
        symbol = coin['symbol']
        ema = ema_data.get(symbol)

        if not ema:
            # No EMA data - include with warning
            coin['ema_alignment'] = "UNKNOWN"
            coin['ema_aligned'] = False
            coin['alignment_score'] = 0.0
            filtered_coins.append(coin)
            continue

        # Determine if EMA confirms momentum direction
        momentum_pct = coin.get('momentum_pct', 0)

        if momentum_pct > 0:
            # Positive momentum - need bullish EMA
            is_aligned = ema.alignment in ["BULLISH", "WEAK_BULLISH"]
        elif momentum_pct < 0:
            # Negative momentum - need bearish EMA
            is_aligned = ema.alignment in ["BEARISH", "WEAK_BEARISH"]
        else:
            is_aligned = False

        # Check alignment score meets minimum
        meets_threshold = ema.alignment_score >= min_alignment_score

        # Add EMA data to coin
        coin['ema_alignment'] = ema.alignment
        coin['ema_25'] = ema.ema_25
        coin['ema_50'] = ema.ema_50
        coin['ema_100'] = ema.ema_100
        coin['alignment_score'] = ema.alignment_score
        coin['trend_strength'] = ema.trend_strength
        coin['ema_aligned'] = is_aligned and meets_threshold

        # Log alignment result
        if is_aligned and meets_threshold:
            logger.debug(
                f"EMA CONFIRMED {symbol}: {ema.alignment} "
                f"(score={ema.alignment_score:.2f}, momentum={momentum_pct:+.2f}%)"
            )
        else:
            logger.debug(
                f"EMA REJECTED {symbol}: {ema.alignment} "
                f"(score={ema.alignment_score:.2f}, momentum={momentum_pct:+.2f}%)"
            )

        filtered_coins.append(coin)

    # Count aligned coins
    aligned_count = sum(1 for c in filtered_coins if c.get('ema_aligned', False))
    logger.info(
        f"EMA Filter: {aligned_count}/{len(filtered_coins)} coins have confirmed alignment"
    )

    return filtered_coins


def get_ema_alignment_score_for_decision(
    symbol: str,
    direction: str,  # "LONG" or "SHORT"
) -> float:
    """
    Get EMA alignment score for a specific trade direction.

    Used by AI decision service to boost/penalize decisions based on EMA.

    Args:
        symbol: Trading symbol
        direction: "LONG" or "SHORT"

    Returns:
        Score 0.0-1.0:
        - 1.0 = Perfect alignment (confirms direction)
        - 0.5 = Weak/partial alignment
        - 0.0 = Counter-alignment (EMA contradicts direction)
    """
    ws_service = get_websocket_candle_service()

    try:
        candles = ws_service.get_candles(symbol, limit=120)
        if not candles:
            return 0.5  # Neutral if no data

        ema_data = calculate_ema_alignment(symbol, candles)
        if not ema_data:
            return 0.5

        # Score based on direction confirmation
        if direction == "LONG":
            if ema_data.alignment == "BULLISH":
                return 1.0
            elif ema_data.alignment == "WEAK_BULLISH":
                return 0.7
            elif ema_data.alignment == "MIXED":
                return 0.4
            elif ema_data.alignment == "WEAK_BEARISH":
                return 0.2
            else:  # BEARISH
                return 0.0
        else:  # SHORT
            if ema_data.alignment == "BEARISH":
                return 1.0
            elif ema_data.alignment == "WEAK_BEARISH":
                return 0.7
            elif ema_data.alignment == "MIXED":
                return 0.4
            elif ema_data.alignment == "WEAK_BULLISH":
                return 0.2
            else:  # BULLISH
                return 0.0

    except Exception as e:
        logger.error(f"Failed to get EMA score for {symbol}: {e}", exc_info=True)
        return 0.5  # Neutral on error
