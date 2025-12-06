"""Momentum Scalping Strategy - Trend following for HFT.

This strategy trades momentum in trending markets, using EMA crossovers,
RSI confirmation, and volume analysis to identify high-probability entries.
"""

import logging
from decimal import Decimal
from typing import List, Optional

from .hft_base import HFTBaseStrategy
from ...core.models import (
    ProposedTrade,
    Bar,
    MarketContext,
    AccountState,
    Position,
)
from ...core.enums import StrategyId, Side, MarketRegime
from ...config.settings import Settings


logger = logging.getLogger(__name__)


class MomentumScalpingStrategy(HFTBaseStrategy):
    """
    Momentum scalping strategy for trending markets.

    Active ONLY when market regime is TREND_UP or TREND_DOWN.
    Uses EMA crossovers + RSI + volume confirmation for entries.

    Key features:
    - Trend following (not mean reversion)
    - Only trades with the trend direction
    - RSI confirmation to avoid chasing extended moves
    - Volume confirmation for conviction
    - Fee-aware TP/SL calculation
    """

    def __init__(self, settings: Settings):
        super().__init__(settings, StrategyId.MOMENTUM_SCALPING)

        # Load config parameters
        self.min_rsi_up = self._get_param('min_rsi_up', Decimal("60"))
        self.max_rsi_down = self._get_param('max_rsi_down', Decimal("40"))
        self.min_volume_ratio = self._get_param('min_volume_ratio', Decimal("1.2"))
        self.ema_fast = self._get_param('ema_fast', 20)
        self.ema_slow = self._get_param('ema_slow', 50)

        # Max volatility threshold (skip extreme volatility)
        self.max_volatility_pct = Decimal("0.05")  # 5%

        logger.info(
            f"MomentumScalpingStrategy initialized: "
            f"RSI_up>{self.min_rsi_up}, RSI_down<{self.max_rsi_down}, "
            f"vol_ratio>{self.min_volume_ratio}"
        )

    def _get_param(self, name: str, default):
        """Get parameter from HFT config."""
        if self._hft_config:
            return getattr(self._hft_config, name, default)
        return default

    async def evaluate(
        self,
        symbol: str,
        bars: List[Bar],
        context: MarketContext,
        account: AccountState,
        position: Optional[Position] = None,
    ) -> Optional[ProposedTrade]:
        """
        Evaluate momentum scalping conditions.

        Entry conditions for LONG (TREND_UP regime):
        - Price > EMA20 > EMA50
        - RSI > 60 (confirming momentum)
        - Volume >= 1.2x average
        - Volatility < 5% (not extreme)

        Entry conditions for SHORT (TREND_DOWN regime):
        - Price < EMA20 < EMA50
        - RSI < 40 (confirming momentum)
        - Volume >= 1.2x average
        - Volatility < 5% (not extreme)
        """
        # 1. Check market regime - ONLY trade in trends
        if self._current_regime not in (MarketRegime.TREND_UP, MarketRegime.TREND_DOWN):
            return None

        # 2. Check volatility - skip extreme volatility
        if context.volatility_1h and context.volatility_1h > self.max_volatility_pct:
            return None

        # 3. Skip if position exists (no pyramiding)
        if position:
            return None

        # 4. Check signal cooldown
        if not self.can_signal_hft(symbol):
            return None

        # 5. Validate bar data
        if len(bars) < max(self.ema_slow + 10, 60):
            return None

        current_price = context.mid_price
        if not current_price or current_price <= 0:
            return None

        # 6. Calculate indicators
        ema_fast_val = self._calculate_ema(bars, self.ema_fast)
        ema_slow_val = self._calculate_ema(bars, self.ema_slow)
        rsi = self._calculate_rsi(bars, 14)
        volume_ratio = self._calculate_volume_ratio(bars)

        if not all([ema_fast_val, ema_slow_val, rsi]):
            return None

        # 7. LONG signal (trend up)
        if self._current_regime == MarketRegime.TREND_UP:
            if (current_price > ema_fast_val and
                ema_fast_val > ema_slow_val and
                rsi > self.min_rsi_up and
                volume_ratio >= self.min_volume_ratio):

                confidence = self._calculate_confidence(
                    rsi, volume_ratio, is_long=True
                )

                logger.info(
                    f"[MOMENTUM] LONG signal {symbol}: "
                    f"RSI={rsi:.1f}, Vol={volume_ratio:.2f}x, conf={confidence:.2f}"
                )

                return self.create_hft_proposal(
                    symbol=symbol,
                    side=Side.LONG,
                    entry_price=current_price,
                    context=context,
                    confidence=confidence,
                    reason=f"Momentum LONG: RSI={rsi:.1f}, Vol={volume_ratio:.2f}x"
                )

        # 8. SHORT signal (trend down)
        if self._current_regime == MarketRegime.TREND_DOWN:
            if (current_price < ema_fast_val and
                ema_fast_val < ema_slow_val and
                rsi < self.max_rsi_down and
                volume_ratio >= self.min_volume_ratio):

                confidence = self._calculate_confidence(
                    rsi, volume_ratio, is_long=False
                )

                logger.info(
                    f"[MOMENTUM] SHORT signal {symbol}: "
                    f"RSI={rsi:.1f}, Vol={volume_ratio:.2f}x, conf={confidence:.2f}"
                )

                return self.create_hft_proposal(
                    symbol=symbol,
                    side=Side.SHORT,
                    entry_price=current_price,
                    context=context,
                    confidence=confidence,
                    reason=f"Momentum SHORT: RSI={rsi:.1f}, Vol={volume_ratio:.2f}x"
                )

        return None

    def _calculate_ema(self, bars: List[Bar], period: int) -> Optional[Decimal]:
        """Calculate Exponential Moving Average."""
        if len(bars) < period:
            return None

        multiplier = Decimal(2) / (Decimal(period) + 1)
        ema = bars[0].close

        for bar in bars[1:]:
            ema = (bar.close - ema) * multiplier + ema

        return ema

    def _calculate_rsi(self, bars: List[Bar], period: int = 14) -> Optional[Decimal]:
        """Calculate Relative Strength Index."""
        if len(bars) < period + 1:
            return None

        gains = []
        losses = []

        for i in range(1, len(bars)):
            change = bars[i].close - bars[i-1].close
            if change > 0:
                gains.append(change)
                losses.append(Decimal(0))
            else:
                gains.append(Decimal(0))
                losses.append(abs(change))

        # Use last 'period' values
        recent_gains = gains[-period:]
        recent_losses = losses[-period:]

        avg_gain = sum(recent_gains) / period
        avg_loss = sum(recent_losses) / period

        if avg_loss == 0:
            return Decimal(100)

        rs = avg_gain / avg_loss
        rsi = Decimal(100) - (Decimal(100) / (1 + rs))

        return rsi

    def _calculate_volume_ratio(self, bars: List[Bar], lookback: int = 20) -> Decimal:
        """Calculate current volume vs N-bar average."""
        if len(bars) < lookback + 1:
            return Decimal("1.0")

        avg_vol = sum(b.volume for b in bars[-lookback-1:-1]) / lookback
        current_vol = bars[-1].volume

        if avg_vol <= 0:
            return Decimal("1.0")

        return current_vol / avg_vol

    def _calculate_confidence(
        self,
        rsi: Decimal,
        volume_ratio: Decimal,
        is_long: bool
    ) -> Decimal:
        """
        Calculate confidence score based on signal strength.

        Factors:
        - RSI distance from threshold (stronger momentum = higher confidence)
        - Volume ratio (higher volume = higher conviction)
        """
        # Base confidence
        confidence = Decimal("0.6")

        # RSI component (0-0.2)
        if is_long:
            rsi_strength = (rsi - self.min_rsi_up) / Decimal("40")  # 60-100 range
        else:
            rsi_strength = (self.max_rsi_down - rsi) / Decimal("40")  # 0-40 range

        rsi_component = min(max(rsi_strength, Decimal("0")), Decimal("1")) * Decimal("0.2")
        confidence += rsi_component

        # Volume component (0-0.2)
        if volume_ratio >= Decimal("2.0"):
            volume_component = Decimal("0.2")
        elif volume_ratio >= Decimal("1.5"):
            volume_component = Decimal("0.1")
        else:
            volume_component = Decimal("0.05")
        confidence += volume_component

        # Cap at 0.9
        return min(confidence, Decimal("0.9"))
