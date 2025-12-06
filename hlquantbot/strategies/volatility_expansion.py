"""
Volatility Expansion Strategy

Trades breakouts after volatility compression.

Logic:
- Identify periods of low volatility (Bollinger Bands squeeze, low ATR)
- Wait for breakout from compression range
- Enter in direction of breakout with volume confirmation

This is a momentum strategy that captures the initial expansion move.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Tuple
import math

from ..core.models import ProposedTrade, AccountState, Bar, MarketContext, Position
from ..core.enums import StrategyId, Side, OrderType, MarketRegime
from ..config.settings import Settings, VolatilityExpansionConfig
from .base import BaseStrategy


logger = logging.getLogger(__name__)


class VolatilityExpansionStrategy(BaseStrategy):
    """
    Volatility Expansion Strategy.

    Entry Logic:
    - Detect volatility compression (BB width squeeze + low ATR percentile)
    - Wait for price to break out of compression range
    - Confirm with volume surge
    - Enter in direction of breakout

    Filters:
    - Active hours (US/EU overlap)
    - Regime filter (avoid already high volatility)
    - Minimum compression threshold

    Exit Logic:
    - Target based on compression range multiple
    - Trailing stop after initial move
    - Time-based exit if no expansion
    """

    def __init__(self, settings: Settings):
        super().__init__(settings, StrategyId.VOLATILITY_EXPANSION)
        self.config: VolatilityExpansionConfig = settings.strategies.volatility_expansion

        # Track compression state
        self._compression_start: dict[str, datetime] = {}
        self._compression_range: dict[str, Tuple[Decimal, Decimal]] = {}  # (high, low)
        self._atr_history: dict[str, List[Decimal]] = {}

    async def evaluate(
        self,
        symbol: str,
        bars: List[Bar],
        context: MarketContext,
        account: AccountState,
        position: Optional[Position] = None,
    ) -> Optional[ProposedTrade]:
        """Evaluate volatility expansion signals."""

        if not bars or len(bars) < self.config.bb_period + 10:
            return None

        # Check active hours
        if not self._is_active_hour():
            return None

        # Skip if we can't signal yet
        if not self.can_signal(symbol, min_interval_seconds=self.config.signal_cooldown_seconds):
            return None

        # If we have a position, check for exit
        if position:
            return await self._evaluate_exit(symbol, position, bars, context)

        # No position - check for entry
        return await self._evaluate_entry(symbol, bars, context, account)

    async def _evaluate_entry(
        self,
        symbol: str,
        bars: List[Bar],
        context: MarketContext,
        account: AccountState,
    ) -> Optional[ProposedTrade]:
        """Evaluate entry conditions."""

        # Skip if already in high volatility regime
        if self._current_regime == MarketRegime.HIGH_VOLATILITY:
            return None

        current_price = context.mid_price

        # 1. Calculate Bollinger Bands
        bb_upper, bb_middle, bb_lower, bb_width = self.calculate_bollinger_bands(
            bars,
            period=self.config.bb_period,
            std_dev=self.config.bb_std,
        )

        if bb_width is None:
            return None

        # 2. Calculate ATR and percentile
        atr = self.calculate_atr(bars, period=self.config.atr_period)
        atr_percentile = self._calculate_atr_percentile(symbol, atr)

        # 3. Check for compression
        is_compressed = (
            bb_width < self.config.bb_width_threshold and
            atr_percentile < self.config.atr_percentile_threshold
        )

        if is_compressed:
            # Track compression start and range
            if symbol not in self._compression_start:
                self._compression_start[symbol] = datetime.now(timezone.utc)
                recent_high = max(b.high for b in bars[-10:])
                recent_low = min(b.low for b in bars[-10:])
                self._compression_range[symbol] = (recent_high, recent_low)
                logger.debug(f"{symbol}: Compression detected, BB width: {bb_width:.4f}")

            return None  # Wait for breakout

        # 4. Check for breakout from compression
        if symbol not in self._compression_start:
            return None  # No prior compression

        comp_high, comp_low = self._compression_range.get(symbol, (current_price, current_price))
        compression_started = self._compression_start[symbol]

        # Minimum compression duration (avoid false compressions)
        compression_duration = (datetime.now(timezone.utc) - compression_started).total_seconds()
        if compression_duration < 300:  # 5 minutes minimum
            return None

        # Determine breakout direction
        breakout_threshold = atr * self.config.breakout_atr_multiplier
        side = None
        reason = ""

        if current_price > comp_high + breakout_threshold:
            side = Side.LONG
            reason = f"Breakout above compression range {comp_high:.2f}"

        elif current_price < comp_low - breakout_threshold:
            side = Side.SHORT
            reason = f"Breakdown below compression range {comp_low:.2f}"

        if not side:
            return None

        # 5. Volume confirmation
        vol_sma = self.calculate_volume_sma(bars, period=20)
        current_vol = bars[-1].volume if bars else Decimal(0)

        if vol_sma > 0:
            vol_ratio = current_vol / vol_sma
            if vol_ratio < self.config.volume_confirmation_multiplier:
                logger.debug(f"{symbol}: Volume {vol_ratio:.2f}x not enough for breakout")
                # Don't require strict volume - just lower confidence
                confidence_penalty = Decimal("0.1")
            else:
                confidence_penalty = Decimal(0)
        else:
            confidence_penalty = Decimal(0)

        # 6. Calculate stop loss and take profit
        compression_range_size = comp_high - comp_low
        stop_loss, take_profit = self._calculate_levels(
            side, current_price, compression_range_size, atr
        )

        # 7. Calculate confidence
        confidence = self._calculate_confidence(
            bb_width, atr_percentile, compression_duration
        ) - confidence_penalty

        logger.info(
            f"VOL EXPANSION {symbol}: {side.value} signal - {reason} "
            f"(BB width: {bb_width:.4f}, ATR pct: {atr_percentile}, confidence: {confidence:.2f})"
        )

        # Clear compression state
        self._compression_start.pop(symbol, None)
        self._compression_range.pop(symbol, None)

        self.record_signal(symbol)

        return self.create_proposal(
            symbol=symbol,
            side=side,
            context=context,
            confidence=confidence,
            stop_loss_price=stop_loss,
            take_profit_price=take_profit,
            reason=reason,
        )

    async def _evaluate_exit(
        self,
        symbol: str,
        position: Position,
        bars: List[Bar],
        context: MarketContext,
    ) -> Optional[ProposedTrade]:
        """Evaluate exit for existing position."""

        # Check if expansion happened
        atr = self.calculate_atr(bars, period=14)
        move_size = abs(context.mid_price - position.entry_price)

        # If we've made a good move, consider trailing stop
        if move_size > atr * Decimal("1.5"):
            # Check if momentum is fading
            bb_upper, bb_middle, bb_lower, bb_width = self.calculate_bollinger_bands(
                bars, period=20, std_dev=Decimal(2)
            )

            if bb_width and bb_width > self.config.bb_width_threshold * 2:
                # Volatility has expanded significantly - consider exit
                # Check if price is reversing
                last_3_bars = bars[-3:]
                if len(last_3_bars) >= 3:
                    if position.side == Side.LONG:
                        # Check for bearish candles
                        bearish_count = sum(1 for b in last_3_bars if not b.is_bullish)
                        if bearish_count >= 2:
                            return self._create_exit_proposal(
                                symbol, position, context, "Momentum fading after expansion"
                            )
                    else:
                        # Check for bullish candles
                        bullish_count = sum(1 for b in last_3_bars if b.is_bullish)
                        if bullish_count >= 2:
                            return self._create_exit_proposal(
                                symbol, position, context, "Momentum fading after expansion"
                            )

        return None

    def _is_active_hour(self) -> bool:
        """Check if current hour is in active trading hours."""
        current_hour = datetime.now(timezone.utc).hour
        return current_hour in self.config.active_hours_utc

    def _calculate_atr_percentile(self, symbol: str, current_atr: Decimal) -> int:
        """Calculate ATR percentile rank over historical ATRs."""
        if symbol not in self._atr_history:
            self._atr_history[symbol] = []

        self._atr_history[symbol].append(current_atr)

        # Keep last 100 ATR values
        if len(self._atr_history[symbol]) > 100:
            self._atr_history[symbol] = self._atr_history[symbol][-100:]

        history = self._atr_history[symbol]
        if len(history) < 10:
            return 50  # Not enough data

        # Calculate percentile
        count_below = sum(1 for atr in history if atr < current_atr)
        percentile = int((count_below / len(history)) * 100)

        return percentile

    def _calculate_levels(
        self,
        side: Side,
        entry_price: Decimal,
        compression_range: Decimal,
        atr: Decimal,
    ) -> Tuple[Decimal, Decimal]:
        """Calculate stop loss and take profit levels."""
        sl_mult = self.config.stop_loss_atr_multiplier
        tp_mult = self.config.take_profit_range_multiplier

        # Stop loss based on ATR
        stop_distance = atr * sl_mult

        # Take profit based on compression range
        # Expect move of 2x the compression range
        target_distance = compression_range * tp_mult

        if side == Side.LONG:
            stop_loss = entry_price - stop_distance
            take_profit = entry_price + target_distance
        else:
            stop_loss = entry_price + stop_distance
            take_profit = entry_price - target_distance

        return stop_loss, take_profit

    def _calculate_confidence(
        self,
        bb_width: Decimal,
        atr_percentile: int,
        compression_duration: float,
    ) -> Decimal:
        """Calculate signal confidence."""
        confidence = Decimal("0.5")

        # Tighter compression = higher confidence
        if bb_width < Decimal("0.015"):
            confidence += Decimal("0.15")
        elif bb_width < Decimal("0.02"):
            confidence += Decimal("0.1")

        # Lower ATR percentile = higher confidence
        if atr_percentile < 10:
            confidence += Decimal("0.15")
        elif atr_percentile < 20:
            confidence += Decimal("0.1")

        # Longer compression = higher confidence (up to a point)
        if compression_duration > 1800:  # 30 min
            confidence += Decimal("0.1")
        elif compression_duration > 900:  # 15 min
            confidence += Decimal("0.05")

        return min(confidence, Decimal("0.9"))

    def _create_exit_proposal(
        self,
        symbol: str,
        position: Position,
        context: MarketContext,
        reason: str,
    ) -> ProposedTrade:
        """Create exit proposal."""
        return ProposedTrade(
            strategy_id=self.strategy_id,
            symbol=symbol,
            side=Side.FLAT,
            entry_type=OrderType.MARKET,
            confidence=Decimal("0.9"),
            reason=reason,
            market_context=context,
        )
