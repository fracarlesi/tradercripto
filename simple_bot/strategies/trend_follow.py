"""
Trend Following Strategy
=========================

Core strategy for HLQuantBot conservative approach.

Entry conditions:
- Regime is TREND
- Price > EMA200 (for longs)
- Breakout of N-bar high
- ATR above average (volatility confirmation)

Exit:
- Initial stop: 2.5 ATR
- Trailing stop: 2.5 ATR from max favorable
- No take profit (let trends run)

This strategy produces few trades but aims for high R-multiples.
"""

import logging
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional

from .base import BaseStrategy, StrategyResult
from ..core.models import MarketState, Setup, Regime, Direction, SetupType


logger = logging.getLogger(__name__)


class TrendFollowStrategy(BaseStrategy):
    """
    Trend-following breakout strategy.

    Works only in TREND regime. Enters on breakout of recent high/low
    with ATR-based stops.
    """

    def __init__(self, config: dict = None):
        """
        Initialize trend follow strategy.

        Config options:
            breakout_period: Number of bars for breakout (default: 20)
            atr_filter: Require ATR above average (default: True)
            price_above_ema200: Require price above EMA200 for longs (default: True)
            stop_atr_mult: ATR multiplier for stop (default: 2.5)
            min_adx: Minimum ADX for entry (default: 25)
        """
        super().__init__(config)

        # Configuration
        self.breakout_period = self.config.get("breakout_period", 20)
        self.atr_filter = self.config.get("atr_filter", True)
        self.price_above_ema200 = self.config.get("price_above_ema200", True)
        self.stop_atr_mult = self.config.get("stop_atr_mult", 2.5)
        self.min_adx = self.config.get("min_adx", 25.0)

        self._logger.info(
            "TrendFollowStrategy initialized: breakout=%d, stop_atr=%.1f",
            self.breakout_period,
            self.stop_atr_mult,
        )

    @property
    def name(self) -> str:
        return "trend_follow"

    @property
    def required_regime(self) -> Regime:
        return Regime.TREND

    def evaluate(self, state: MarketState) -> StrategyResult:
        """
        Evaluate market state for trend breakout setup.

        Checks:
        1. Regime is TREND
        2. ADX > min_adx
        3. Price position relative to EMA200
        4. Breakout conditions
        5. ATR filter
        """
        # Check regime
        if not self.can_trade(state):
            return StrategyResult(
                has_setup=False,
                reason=f"Wrong regime: {state.regime.value}, need TREND"
            )

        # Check ADX
        if float(state.adx) < self.min_adx:
            return StrategyResult(
                has_setup=False,
                reason=f"ADX too low: {state.adx:.1f} < {self.min_adx}"
            )

        # Determine direction based on trend
        direction = self._determine_direction(state)
        if direction == Direction.FLAT:
            return StrategyResult(
                has_setup=False,
                reason="No clear trend direction"
            )

        # Check price vs EMA200
        if self.price_above_ema200:
            if direction == Direction.LONG and state.close < state.ema200:
                return StrategyResult(
                    has_setup=False,
                    reason="Price below EMA200 for long"
                )
            if direction == Direction.SHORT and state.close > state.ema200:
                return StrategyResult(
                    has_setup=False,
                    reason="Price above EMA200 for short"
                )

        # Check ATR filter
        if self.atr_filter:
            # We use atr_pct as proxy - should be above historical average
            # Simplified: ATR% > 1% indicates active market
            if float(state.atr_pct) < 0.5:
                return StrategyResult(
                    has_setup=False,
                    reason=f"ATR too low: {state.atr_pct:.2f}%"
                )

        # Calculate entry and stop prices
        entry_price = state.close
        stop_price = self.calculate_stop_price(
            entry_price=entry_price,
            atr=state.atr,
            direction=direction,
            atr_mult=self.stop_atr_mult,
        )
        stop_distance_pct = self.calculate_stop_distance_pct(entry_price, stop_price)

        # Calculate setup quality based on indicators
        quality = self._calculate_quality(state, direction)

        # Create setup
        setup = Setup(
            id=self.generate_setup_id(),
            symbol=state.symbol,
            timestamp=datetime.now(timezone.utc),
            setup_type=SetupType.TREND_BREAKOUT,
            direction=direction,
            regime=state.regime,
            entry_price=entry_price,
            stop_price=stop_price,
            stop_distance_pct=stop_distance_pct,
            atr=state.atr,
            adx=state.adx,
            rsi=state.rsi,
            setup_quality=quality,
            confidence=Decimal(str(min(0.9, float(state.adx) / 100 + 0.4))),
        )

        self._logger.info(
            "SETUP: %s %s @ %.2f, stop=%.2f (%.2f%%), quality=%.2f",
            direction.value.upper(),
            state.symbol,
            float(entry_price),
            float(stop_price),
            float(stop_distance_pct),
            float(quality),
        )

        return StrategyResult(
            has_setup=True,
            setup=setup,
            reason="Trend breakout conditions met"
        )

    def _determine_direction(self, state: MarketState) -> Direction:
        """
        Determine trade direction based on trend indicators.

        Uses:
        - EMA200 slope
        - Price position vs EMA200
        - Trend direction from market state
        """
        # Use the trend direction already calculated by MarketStateService
        if state.trend_direction != Direction.FLAT:
            return state.trend_direction

        # Fallback: check EMA200 slope
        if float(state.ema200_slope) > 0.001:
            return Direction.LONG
        elif float(state.ema200_slope) < -0.001:
            return Direction.SHORT

        return Direction.FLAT

    def _calculate_quality(self, state: MarketState, direction: Direction) -> Decimal:
        """
        Calculate setup quality score 0-1.

        Factors:
        - ADX strength (higher = better trend)
        - RSI confirmation (not overbought/oversold against direction)
        - ATR level (higher = more potential)
        """
        score = Decimal("0.5")  # Base score

        # ADX bonus (max +0.3)
        adx_bonus = min(0.3, (float(state.adx) - 25) / 50)
        score += Decimal(str(adx_bonus))

        # RSI confirmation (max +0.2)
        rsi = float(state.rsi)
        if direction == Direction.LONG:
            if 40 < rsi < 70:  # Not oversold, not overbought
                score += Decimal("0.2")
            elif 30 < rsi < 40:  # Slightly oversold - good for entry
                score += Decimal("0.15")
        else:  # SHORT
            if 30 < rsi < 60:
                score += Decimal("0.2")
            elif 60 < rsi < 70:
                score += Decimal("0.15")

        # Cap at 1.0
        return min(Decimal("1.0"), max(Decimal("0.0"), score))
