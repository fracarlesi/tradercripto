"""
Simple Trading Strategies
=========================

Three simple strategies:
1. Momentum: Follow the trend (EMA crossover + RSI confirmation)
2. Mean Reversion: Fade extremes (RSI oversold/overbought)
3. Breakout: Trade range breaks (price vs recent high/low)

Each strategy returns: "long", "short", or None
"""

import logging
from dataclasses import dataclass
from typing import Optional, List
from enum import Enum

logger = logging.getLogger(__name__)


class Signal(Enum):
    LONG = "long"
    SHORT = "short"
    NONE = None


@dataclass
class StrategyConfig:
    """Configuration for a strategy."""
    name: str
    params: dict


# =============================================================================
# Technical Indicators (Simple implementations)
# =============================================================================

def calculate_ema(prices: List[float], period: int) -> Optional[float]:
    """Calculate Exponential Moving Average."""
    if len(prices) < period:
        return None
    
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period  # Start with SMA
    
    for price in prices[period:]:
        ema = (price * multiplier) + (ema * (1 - multiplier))
    
    return ema


def calculate_sma(prices: List[float], period: int) -> Optional[float]:
    """Calculate Simple Moving Average."""
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period


def calculate_rsi(prices: List[float], period: int = 14) -> Optional[float]:
    """Calculate Relative Strength Index."""
    if len(prices) < period + 1:
        return None
    
    # Calculate price changes
    changes = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    
    # Separate gains and losses
    gains = [max(0, c) for c in changes[-period:]]
    losses = [abs(min(0, c)) for c in changes[-period:]]
    
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    
    return rsi


def calculate_bollinger_bands(
    prices: List[float], 
    period: int = 20, 
    std_dev: float = 2.0
) -> Optional[tuple]:
    """Calculate Bollinger Bands. Returns (lower, middle, upper)."""
    if len(prices) < period:
        return None
    
    recent_prices = prices[-period:]
    middle = sum(recent_prices) / period
    
    # Calculate standard deviation
    variance = sum((p - middle) ** 2 for p in recent_prices) / period
    std = variance ** 0.5
    
    lower = middle - (std_dev * std)
    upper = middle + (std_dev * std)
    
    return (lower, middle, upper)


def calculate_atr(prices: List[float], period: int = 14) -> Optional[float]:
    """
    Calculate Average True Range (ATR) using close prices only.
    
    For a simplified version with only close prices:
    True Range = |Price[i] - Price[i-1]|
    ATR = Exponential Moving Average of True Range over N periods
    
    Args:
        prices: List of closing prices (oldest first)
        period: ATR period (default 14)
    
    Returns:
        ATR value or None if insufficient data
    """
    if len(prices) < period + 1:
        return None
    
    # Calculate True Range values (simplified: using close-to-close)
    true_ranges = []
    for i in range(1, len(prices)):
        tr = abs(prices[i] - prices[i - 1])
        true_ranges.append(tr)
    
    if len(true_ranges) < period:
        return None
    
    # Calculate initial ATR as SMA of first 'period' true ranges
    atr = sum(true_ranges[:period]) / period
    
    # Apply smoothing (Wilder's smoothing method)
    # ATR = ((Prior ATR * (period - 1)) + Current TR) / period
    for tr in true_ranges[period:]:
        atr = ((atr * (period - 1)) + tr) / period
    
    return atr


def get_high_low(prices: List[float], period: int) -> Optional[tuple]:
    """Get highest and lowest prices in period. Returns (low, high)."""
    if len(prices) < period:
        return None
    
    recent = prices[-period:]
    return (min(recent), max(recent))


def calculate_adx(prices: List[float], period: int = 14) -> Optional[float]:
    """
    Calculate Average Directional Index (ADX).
    
    ADX measures trend strength (not direction):
    - ADX > 25: Strong trend (good for momentum strategies)
    - ADX < 20: Weak trend / ranging (good for mean reversion)
    
    Formula:
    - +DM = max(high[i] - high[i-1], 0) if > -DM else 0
    - -DM = max(low[i-1] - low[i], 0) if > +DM else 0
    - +DI = Smoothed(+DM) / ATR * 100
    - -DI = Smoothed(-DM) / ATR * 100
    - DX = |+DI - -DI| / (+DI + -DI) * 100
    - ADX = Smoothed(DX)
    
    Since we only have close prices, we approximate high/low from consecutive closes.
    """
    min_required = period * 2 + 1
    if len(prices) < min_required:
        return None
    
    # Calculate directional movements and true range
    plus_dm = []
    minus_dm = []
    tr_values = []
    
    for i in range(1, len(prices)):
        current = prices[i]
        previous = prices[i - 1]
        
        # Approximate high/low from close prices
        # Use the higher close as high, lower as low
        up_move = current - previous if current > previous else 0
        down_move = previous - current if previous > current else 0
        
        # Directional movement
        if up_move > down_move and up_move > 0:
            plus_dm.append(up_move)
            minus_dm.append(0)
        elif down_move > up_move and down_move > 0:
            plus_dm.append(0)
            minus_dm.append(down_move)
        else:
            plus_dm.append(0)
            minus_dm.append(0)
        
        # True range (simplified for close-only)
        tr = abs(current - previous)
        tr_values.append(max(tr, 0.0001))  # Avoid division by zero
    
    # Wilder's smoothing alpha
    alpha = 1.0 / period
    
    # Initial smoothed values (SMA of first 'period' values)
    smoothed_plus_dm = sum(plus_dm[:period]) / period
    smoothed_minus_dm = sum(minus_dm[:period]) / period
    smoothed_tr = sum(tr_values[:period]) / period
    
    # Apply Wilder's smoothing for remaining values
    dx_values = []
    
    for i in range(period, len(plus_dm)):
        smoothed_plus_dm = alpha * plus_dm[i] + (1 - alpha) * smoothed_plus_dm
        smoothed_minus_dm = alpha * minus_dm[i] + (1 - alpha) * smoothed_minus_dm
        smoothed_tr = alpha * tr_values[i] + (1 - alpha) * smoothed_tr
        
        # Calculate +DI and -DI
        if smoothed_tr > 0:
            plus_di = (smoothed_plus_dm / smoothed_tr) * 100
            minus_di = (smoothed_minus_dm / smoothed_tr) * 100
        else:
            plus_di = 0
            minus_di = 0
        
        # Calculate DX
        di_sum = plus_di + minus_di
        if di_sum > 0:
            dx = (abs(plus_di - minus_di) / di_sum) * 100
        else:
            dx = 0
        
        dx_values.append(dx)
    
    if len(dx_values) < period:
        return None
    
    # Calculate ADX as smoothed DX
    adx = sum(dx_values[:period]) / period  # Initial SMA
    
    for dx in dx_values[period:]:
        adx = alpha * dx + (1 - alpha) * adx
    
    return adx


# =============================================================================
# Strategy Implementations
# =============================================================================

class MomentumStrategy:
    """
    Momentum Strategy
    ----------------
    - Long when EMA(fast) > EMA(slow) AND RSI > threshold
    - Short when EMA(fast) < EMA(slow) AND RSI < threshold
    - Follows the trend
    """
    
    def __init__(self, config: dict):
        self.ema_fast_period = config.get("ema_fast", 20)
        self.ema_slow_period = config.get("ema_slow", 50)
        self.rsi_period = config.get("rsi_period", 14)
        self.rsi_long_threshold = config.get("rsi_long_threshold", 55)
        self.rsi_short_threshold = config.get("rsi_short_threshold", 45)
        
        logger.info(
            f"MomentumStrategy initialized: EMA({self.ema_fast_period}/{self.ema_slow_period}), "
            f"RSI({self.rsi_period}), thresholds({self.rsi_long_threshold}/{self.rsi_short_threshold})"
        )
    
    def evaluate(self, prices: List[float]) -> Signal:
        """Evaluate momentum strategy. Returns Signal.
        
        Only generates signals when ADX > 25 (trending market).
        """
        # Need enough data for ADX calculation
        adx_period = 14
        min_required = max(self.ema_slow_period, self.rsi_period + 1, adx_period * 2 + 1)
        if len(prices) < min_required:
            logger.debug(f"Not enough data: {len(prices)} < {min_required}")
            return Signal.NONE
        
        # Calculate ADX for regime detection
        adx = calculate_adx(prices, adx_period)
        
        # ADX filter: Only trade in trending markets (ADX > 25)
        adx_threshold = 25.0
        if adx is None or adx < adx_threshold:
            logger.debug(f"Momentum: ADX={adx if adx else 0:.1f} < {adx_threshold} (not trending)")
            return Signal.NONE
        
        # Calculate indicators
        ema_fast = calculate_ema(prices, self.ema_fast_period)
        ema_slow = calculate_ema(prices, self.ema_slow_period)
        rsi = calculate_rsi(prices, self.rsi_period)
        
        if ema_fast is None or ema_slow is None or rsi is None:
            return Signal.NONE
        
        current_price = prices[-1]
        
        logger.debug(
            f"Momentum: price={current_price:.2f}, ADX={adx:.1f}, "
            f"EMA_fast={ema_fast:.2f}, EMA_slow={ema_slow:.2f}, RSI={rsi:.1f}"
        )
        
        # Long: uptrend (fast > slow) + RSI confirms
        if ema_fast > ema_slow and rsi > self.rsi_long_threshold:
            logger.info(f"LONG signal: ADX={adx:.1f}, EMA_fast > EMA_slow, RSI={rsi:.1f} > {self.rsi_long_threshold}")
            return Signal.LONG
        
        # Short: downtrend (fast < slow) + RSI confirms
        if ema_fast < ema_slow and rsi < self.rsi_short_threshold:
            logger.info(f"SHORT signal: ADX={adx:.1f}, EMA_fast < EMA_slow, RSI={rsi:.1f} < {self.rsi_short_threshold}")
            return Signal.SHORT
        
        return Signal.NONE


class MeanReversionStrategy:
    """
    Mean Reversion Strategy
    ----------------------
    - Long when RSI is oversold (< 30) or price below lower Bollinger Band
    - Short when RSI is overbought (> 70) or price above upper Bollinger Band
    - Fades extremes expecting reversion to mean
    """
    
    def __init__(self, config: dict):
        self.rsi_period = config.get("rsi_period", 14)
        self.rsi_oversold = config.get("rsi_oversold", 30)
        self.rsi_overbought = config.get("rsi_overbought", 70)
        self.bb_period = config.get("bb_period", 20)
        self.bb_std = config.get("bb_std", 2.0)
        
        logger.info(
            f"MeanReversionStrategy initialized: RSI({self.rsi_period}) "
            f"oversold<{self.rsi_oversold}, overbought>{self.rsi_overbought}, "
            f"BB({self.bb_period}, {self.bb_std})"
        )
    
    def evaluate(self, prices: List[float]) -> Signal:
        """Evaluate mean reversion strategy. Returns Signal.
        
        Only generates signals when ADX < 20 (ranging/choppy market).
        """
        adx_period = 14
        min_required = max(self.rsi_period + 1, self.bb_period, adx_period * 2 + 1)
        if len(prices) < min_required:
            logger.debug(f"Not enough data: {len(prices)} < {min_required}")
            return Signal.NONE
        
        # Calculate ADX for regime detection
        adx = calculate_adx(prices, adx_period)
        
        # ADX filter: Only trade in ranging markets (ADX < 20)
        adx_threshold = 20.0
        if adx is None or adx >= adx_threshold:
            logger.debug(f"MeanRev: ADX={adx if adx else 0:.1f} >= {adx_threshold} (trending, skip)")
            return Signal.NONE
        
        rsi = calculate_rsi(prices, self.rsi_period)
        bb = calculate_bollinger_bands(prices, self.bb_period, self.bb_std)
        
        if rsi is None or bb is None:
            return Signal.NONE
        
        current_price = prices[-1]
        bb_lower, bb_middle, bb_upper = bb
        
        logger.debug(
            f"MeanRev: price={current_price:.2f}, ADX={adx:.1f}, RSI={rsi:.1f}, "
            f"BB=[{bb_lower:.2f}, {bb_middle:.2f}, {bb_upper:.2f}]"
        )
        
        # Long: oversold conditions
        if rsi < self.rsi_oversold:
            logger.info(f"LONG signal: ADX={adx:.1f}, RSI={rsi:.1f} < {self.rsi_oversold} (oversold)")
            return Signal.LONG
        
        if current_price < bb_lower:
            logger.info(f"LONG signal: ADX={adx:.1f}, price={current_price:.2f} < BB_lower={bb_lower:.2f}")
            return Signal.LONG
        
        # Short: overbought conditions
        if rsi > self.rsi_overbought:
            logger.info(f"SHORT signal: ADX={adx:.1f}, RSI={rsi:.1f} > {self.rsi_overbought} (overbought)")
            return Signal.SHORT
        
        if current_price > bb_upper:
            logger.info(f"SHORT signal: ADX={adx:.1f}, price={current_price:.2f} > BB_upper={bb_upper:.2f}")
            return Signal.SHORT
        
        return Signal.NONE


class BreakoutStrategy:
    """
    Breakout Strategy
    ----------------
    - Long when price breaks above recent high
    - Short when price breaks below recent low
    - Trades range expansions
    """
    
    def __init__(self, config: dict):
        self.lookback_bars = config.get("lookback_bars", 20)
        self.min_breakout_pct = config.get("min_breakout_pct", 0.002)  # 0.2%
        self.volatility_multiplier = config.get("volatility_multiplier", 1.5)  # ATR surge threshold
        
        logger.info(
            f"BreakoutStrategy initialized: lookback={self.lookback_bars} bars, "
            f"min_breakout={self.min_breakout_pct*100:.2f}%, "
            f"volatility_multiplier={self.volatility_multiplier}x"
        )
    
    def evaluate(self, prices: List[float]) -> Signal:
        """Evaluate breakout strategy with ATR volatility confirmation. Returns Signal."""
        # Need lookback + 1 for the current bar
        if len(prices) < self.lookback_bars + 1:
            logger.debug(f"Not enough data: {len(prices)} < {self.lookback_bars + 1}")
            return Signal.NONE
        
        # Calculate high/low of lookback period (excluding current bar)
        lookback_prices = prices[-(self.lookback_bars + 1):-1]
        current_price = prices[-1]
        
        period_high = max(lookback_prices)
        period_low = min(lookback_prices)
        
        # Calculate ATR (Average True Range) as volatility proxy
        # Since we only have close prices, use price range as TR approximation
        current_atr = self._calculate_atr(prices, period=min(14, self.lookback_bars))
        avg_atr = self._calculate_avg_atr(prices, period=min(14, self.lookback_bars), lookback=self.lookback_bars)
        
        # Check volatility surge condition
        volatility_surge = current_atr > avg_atr * self.volatility_multiplier if avg_atr > 0 else False
        
        logger.debug(
            f"Breakout: price={current_price:.2f}, "
            f"period_high={period_high:.2f}, period_low={period_low:.2f}, "
            f"current_atr={current_atr:.4f}, avg_atr={avg_atr:.4f}, "
            f"volatility_surge={volatility_surge}"
        )
        
        # Calculate breakout percentages
        breakout_above = (current_price - period_high) / period_high
        breakout_below = (period_low - current_price) / period_low
        
        # Long: price breaks above high by min_breakout_pct AND volatility confirms
        if breakout_above > self.min_breakout_pct:
            if volatility_surge:
                logger.info(
                    f"LONG signal: price={current_price:.2f} broke above "
                    f"{period_high:.2f} by {breakout_above*100:.2f}% "
                    f"(ATR surge: {current_atr/avg_atr:.2f}x)"
                )
                return Signal.LONG
            else:
                logger.debug(
                    f"Breakout above rejected: no volatility confirmation "
                    f"(ATR ratio: {current_atr/avg_atr:.2f}x < {self.volatility_multiplier}x)"
                )
        
        # Short: price breaks below low by min_breakout_pct AND volatility confirms
        if breakout_below > self.min_breakout_pct:
            if volatility_surge:
                logger.info(
                    f"SHORT signal: price={current_price:.2f} broke below "
                    f"{period_low:.2f} by {breakout_below*100:.2f}% "
                    f"(ATR surge: {current_atr/avg_atr:.2f}x)"
                )
                return Signal.SHORT
            else:
                logger.debug(
                    f"Breakout below rejected: no volatility confirmation "
                    f"(ATR ratio: {current_atr/avg_atr:.2f}x < {self.volatility_multiplier}x)"
                )
        
        return Signal.NONE

    def _calculate_atr(self, prices: List[float], period: int = 14) -> float:
        """
        Calculate current ATR (Average True Range) using close prices.
        Since we only have close prices, we approximate TR as |close - prev_close|.
        """
        if len(prices) < period + 1:
            return 0.0
        
        # Calculate True Range approximation using close-to-close changes
        recent_prices = prices[-(period + 1):]
        true_ranges = []
        for i in range(1, len(recent_prices)):
            tr = abs(recent_prices[i] - recent_prices[i-1])
            true_ranges.append(tr)
        
        return sum(true_ranges) / len(true_ranges) if true_ranges else 0.0
    
    def _calculate_avg_atr(self, prices: List[float], period: int = 14, lookback: int = 20) -> float:
        """
        Calculate average ATR over a lookback period (excluding current bar).
        This gives us the baseline volatility to compare against.
        """
        if len(prices) < lookback + period + 1:
            # Not enough data for full lookback, use what we have
            lookback = max(1, len(prices) - period - 1)
        
        # Calculate ATR for each point in the lookback period (excluding current)
        atrs = []
        for i in range(lookback):
            # Get prices up to (but not including) current bar minus i
            end_idx = -(i + 1) if i > 0 else -1
            if end_idx == -1:
                subset = prices[:-1]
            else:
                subset = prices[:end_idx]
            
            if len(subset) >= period + 1:
                atr = self._calculate_atr(subset, period)
                if atr > 0:
                    atrs.append(atr)
        
        return sum(atrs) / len(atrs) if atrs else 0.0


# =============================================================================
# Strategy Factory
# =============================================================================

def create_strategy(name: str, config: dict):
    """Create a strategy instance by name."""
    strategies = {
        "momentum": MomentumStrategy,
        "mean_reversion": MeanReversionStrategy,
        "breakout": BreakoutStrategy,
    }
    
    if name not in strategies:
        raise ValueError(f"Unknown strategy: {name}. Available: {list(strategies.keys())}")
    
    # Get strategy-specific config
    strategy_config = config.get(name, {})
    
    return strategies[name](strategy_config)
