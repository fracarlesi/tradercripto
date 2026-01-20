"""
Trend Following Strategy - SMA Crossover with Candle Confirmation
===================================================================

Simplified strategy using Simple Moving Average crossover with candlestick
pattern confirmation to avoid false signals.

Entry conditions:
- LONG: Price > SMA20 AND SMA20 > SMA50 (golden cross setup)
        + Bullish engulfing candle pattern for confirmation
- SHORT: Price < SMA20 AND SMA20 < SMA50 (death cross setup)
         + Bearish engulfing candle pattern for confirmation

Candlestick Pattern Confirmation:
- Bullish Engulfing: Current bullish candle body completely engulfs
  previous bearish candle body - confirms buyer control
- Bearish Engulfing: Current bearish candle body completely engulfs
  previous bullish candle body - confirms seller control

Exit:
- When price crosses SMA20 in opposite direction
- Initial stop: 2.5 ATR from entry

This strategy focuses on clear, simple signals for BTC trading with
additional candle confirmation to filter out low-probability setups.
"""

import logging
from decimal import Decimal
from datetime import datetime, timezone

from .base import BaseStrategy, StrategyResult
from ..core.models import MarketState, Setup, Regime, Direction, SetupType


logger = logging.getLogger(__name__)


class TrendFollowStrategy(BaseStrategy):
    """
    Simple SMA crossover strategy.

    Uses SMA20/SMA50 crossover for trend direction and price
    position relative to SMA20 for entry timing.
    """

    def __init__(self, config: dict = None):
        """
        Initialize SMA crossover strategy.

        Config options:
            stop_atr_mult: ATR multiplier for stop (default: 2.5)
            allow_short: Allow short positions (default: False)
            min_atr_pct: Minimum ATR% for entry (default: 0.3)
            require_candle_confirm: Require engulfing candle pattern (default: True)
        """
        super().__init__(config)

        # Configuration
        self.stop_atr_mult = self.config.get("stop_atr_mult", 2.5)
        self.allow_short = self.config.get("allow_short", False)
        self.min_atr_pct = self.config.get("min_atr_pct", 0.3)
        self.require_candle_confirm = self.config.get("require_candle_confirm", True)

        self._logger.info(
            "TrendFollowStrategy (SMA Crossover) initialized: stop_atr=%.1f, allow_short=%s, candle_confirm=%s",
            self.stop_atr_mult,
            self.allow_short,
            self.require_candle_confirm,
        )

    @property
    def name(self) -> str:
        return "trend_follow"

    @property
    def required_regime(self) -> Regime:
        # SMA strategy works in all regimes - signals are clearer
        return Regime.TREND

    def can_trade(self, state: MarketState) -> bool:
        """
        Override to allow trading in any regime.

        The SMA crossover strategy doesn't rely on regime detection
        since SMA signals are self-contained.
        """
        return True

    def evaluate(self, state: MarketState) -> StrategyResult:
        """
        Evaluate market state for SMA crossover setup with candle confirmation.

        Entry conditions:
        - LONG: Price > SMA20 AND SMA20 > SMA50 (golden cross setup)
                + Bullish engulfing candle (if require_candle_confirm=True)
        - SHORT: Price < SMA20 AND SMA20 < SMA50 (death cross setup)
                 + Bearish engulfing candle (if require_candle_confirm=True)
        """
        # Check minimum volatility
        if float(state.atr_pct) < self.min_atr_pct:
            return self.reject(f"ATR too low: {state.atr_pct:.2f}% < {self.min_atr_pct}%")

        # Determine direction based on SMA crossover
        direction = self._determine_sma_direction(state)
        if direction == Direction.FLAT:
            return self.reject("No SMA crossover signal")

        # Check if shorts are disabled
        if direction == Direction.SHORT and not self.allow_short:
            return self.reject("Short positions disabled in configuration")

        # Check candle confirmation if required
        if self.require_candle_confirm:
            candle_confirmed = self._check_candle_confirmation(state, direction)
            if not candle_confirmed:
                expected_pattern = "bullish engulfing" if direction == Direction.LONG else "bearish engulfing"
                return self.reject(f"No {expected_pattern} candle confirmation")

        # Calculate entry and stop prices
        entry_price = state.close
        stop_price = self.calculate_stop_price(
            entry_price=entry_price,
            atr=state.atr,
            direction=direction,
            atr_mult=self.stop_atr_mult,
        )
        stop_distance_pct = self.calculate_stop_distance_pct(entry_price, stop_price)

        # Calculate setup quality based on SMA alignment strength
        quality = self._calculate_sma_quality(state, direction)

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
            confidence=quality,  # Use quality as confidence for simplicity
        )

        crossover_type = "Golden Cross" if direction == Direction.LONG else "Death Cross"
        candle_pattern = "bullish engulfing" if direction == Direction.LONG else "bearish engulfing"
        candle_status = f" + {candle_pattern}" if self.require_candle_confirm else ""

        self._logger.info(
            "SETUP: %s %s @ %.2f (%s%s), SMA20=%.2f, SMA50=%.2f, stop=%.2f (%.2f%%)",
            direction.value.upper(),
            state.symbol,
            float(entry_price),
            crossover_type,
            candle_status,
            float(state.sma20),
            float(state.sma50),
            float(stop_price),
            float(stop_distance_pct),
        )

        reason_parts = [f"{crossover_type} setup: Price {'>' if direction == Direction.LONG else '<'} SMA20 {'>' if direction == Direction.LONG else '<'} SMA50"]
        if self.require_candle_confirm:
            reason_parts.append(f"confirmed by {candle_pattern}")

        return StrategyResult(
            has_setup=True,
            setup=setup,
            reason=" ".join(reason_parts)
        )

    def _determine_sma_direction(self, state: MarketState) -> Direction:
        """
        Determine trade direction based on SMA crossover.

        Golden Cross (LONG): Price > SMA20 AND SMA20 > SMA50
        Death Cross (SHORT): Price < SMA20 AND SMA20 < SMA50

        Returns FLAT if no clear signal.
        """
        price = state.close
        sma20 = state.sma20
        sma50 = state.sma50

        # Golden Cross setup: Price > SMA20 > SMA50
        if price > sma20 and sma20 > sma50:
            return Direction.LONG

        # Death Cross setup: Price < SMA20 < SMA50
        if price < sma20 and sma20 < sma50:
            return Direction.SHORT

        return Direction.FLAT

    def _check_candle_confirmation(self, state: MarketState, direction: Direction) -> bool:
        """
        Check if there's a confirming candlestick pattern for the trade direction.

        Candle Confirmation Rules:
        ==========================

        For LONG trades, we want a BULLISH ENGULFING pattern:
        - Shows buyers have taken control
        - Previous bearish candle completely engulfed by bullish candle
        - Confirms momentum reversal in favor of longs

        For SHORT trades, we want a BEARISH ENGULFING pattern:
        - Shows sellers have taken control
        - Previous bullish candle completely engulfed by bearish candle
        - Confirms momentum reversal in favor of shorts

        Args:
            state: Current market state with engulfing pattern flags
            direction: Trade direction (LONG or SHORT)

        Returns:
            True if candle pattern confirms the trade direction
        """
        if direction == Direction.LONG:
            return state.bullish_engulfing
        elif direction == Direction.SHORT:
            return state.bearish_engulfing
        return False

    def _calculate_sma_quality(self, state: MarketState, direction: Direction) -> Decimal:
        """
        Calculate setup quality score 0-1 based on SMA alignment.

        Factors:
        - Distance between SMA20 and SMA50 (more separation = stronger trend)
        - Price distance from SMA20 (closer = better entry)
        - RSI confirmation (not extreme)
        """
        score = Decimal("0.5")  # Base score

        # SMA separation bonus (max +0.2)
        # More separation between SMAs indicates stronger trend
        sma_diff_pct = abs(float(state.sma20) - float(state.sma50)) / float(state.sma50) * 100
        sma_bonus = min(0.2, sma_diff_pct / 5)  # Max bonus at 5% separation
        score += Decimal(str(sma_bonus))

        # Price proximity to SMA20 bonus (max +0.15)
        # Entries closer to SMA20 have better risk/reward
        price_to_sma20_pct = abs(float(state.close) - float(state.sma20)) / float(state.sma20) * 100
        if price_to_sma20_pct < 1:
            score += Decimal("0.15")
        elif price_to_sma20_pct < 2:
            score += Decimal("0.10")
        elif price_to_sma20_pct < 3:
            score += Decimal("0.05")

        # RSI confirmation (max +0.15)
        rsi = float(state.rsi)
        if direction == Direction.LONG:
            if 40 <= rsi <= 65:  # Not overbought
                score += Decimal("0.15")
            elif 35 <= rsi < 40 or 65 < rsi <= 70:
                score += Decimal("0.08")
        else:  # SHORT
            if 35 <= rsi <= 60:  # Not oversold
                score += Decimal("0.15")
            elif 30 <= rsi < 35 or 60 < rsi <= 65:
                score += Decimal("0.08")

        # Cap at 1.0
        return min(Decimal("1.0"), max(Decimal("0.0"), score))
