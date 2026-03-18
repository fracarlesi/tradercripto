"""
Regime Detector
================

Deterministic market regime classification using VWAP, ATR, and price data.
Initially observation-only: records regime per bar but does NOT block trades.

Classification priority:
1. ATR > 1.5x avg -> HIGH_VOLATILITY
2. ATR < 0.5x avg -> LOW_VOLATILITY
3. >3 VWAP crosses in 10 bars -> CHOPPY
4. Price > VWAP 7+/10 bars -> TREND_UP
5. Price < VWAP 7+/10 bars -> TREND_DOWN
6. Default -> MEAN_REVERT
"""

from __future__ import annotations

import logging
from collections import deque
from decimal import Decimal
from enum import Enum

logger = logging.getLogger(__name__)


class MarketRegime(str, Enum):
    """Market regime classifications."""

    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    MEAN_REVERT = "mean_revert"
    HIGH_VOLATILITY = "high_vol"
    LOW_VOLATILITY = "low_vol"
    CHOPPY = "choppy"


class RegimeDetector:
    """Deterministic regime classifier.

    Call update() on each bar with close, VWAP, and ATR values.
    The current regime is available via the `regime` property.
    """

    def __init__(
        self,
        atr_lookback: int = 20,
        price_window: int = 10,
        vwap_cross_threshold: int = 3,
        trend_threshold: int = 7,
        high_vol_mult: float = 1.5,
        low_vol_mult: float = 0.5,
    ) -> None:
        self._atr_lookback = atr_lookback
        self._price_window = price_window
        self._vwap_cross_threshold = vwap_cross_threshold
        self._trend_threshold = trend_threshold
        self._high_vol_mult = Decimal(str(high_vol_mult))
        self._low_vol_mult = Decimal(str(low_vol_mult))

        # Rolling buffers
        self._atr_history: deque[Decimal] = deque(maxlen=atr_lookback)
        self._close_history: deque[Decimal] = deque(maxlen=price_window)
        self._vwap_history: deque[Decimal] = deque(maxlen=price_window)

        self._regime: MarketRegime = MarketRegime.MEAN_REVERT

    @property
    def regime(self) -> MarketRegime:
        return self._regime

    def reset(self) -> None:
        """Reset state for a new trading day."""
        self._close_history.clear()
        self._vwap_history.clear()
        self._regime = MarketRegime.MEAN_REVERT
        # Keep ATR history across days for avg calculation

    def update(
        self,
        close: Decimal,
        vwap: Decimal,
        atr: Decimal,
    ) -> MarketRegime:
        """Update with new bar data and return current regime.

        Args:
            close: Bar close price.
            vwap: Current VWAP value.
            atr: Current ATR value.

        Returns:
            Updated MarketRegime classification.
        """
        self._close_history.append(close)
        self._vwap_history.append(vwap)
        self._atr_history.append(atr)

        self._regime = self._classify(atr)
        return self._regime

    def _classify(self, current_atr: Decimal) -> MarketRegime:
        """Apply classification rules in priority order."""
        # Need enough history for ATR average
        if len(self._atr_history) >= 5:
            avg_atr = sum(self._atr_history) / Decimal(str(len(self._atr_history)))

            if avg_atr > 0:
                if current_atr > avg_atr * self._high_vol_mult:
                    return MarketRegime.HIGH_VOLATILITY
                if current_atr < avg_atr * self._low_vol_mult:
                    return MarketRegime.LOW_VOLATILITY

        # Need price_window bars for VWAP/trend analysis
        if len(self._close_history) < self._price_window:
            return MarketRegime.MEAN_REVERT

        closes = list(self._close_history)
        vwaps = list(self._vwap_history)

        # Count VWAP crosses
        crosses = 0
        for i in range(1, len(closes)):
            prev_above = closes[i - 1] > vwaps[i - 1]
            curr_above = closes[i] > vwaps[i]
            if prev_above != curr_above:
                crosses += 1

        if crosses > self._vwap_cross_threshold:
            return MarketRegime.CHOPPY

        # Trend detection
        above_count = sum(1 for c, v in zip(closes, vwaps) if c > v)
        below_count = len(closes) - above_count

        if above_count >= self._trend_threshold:
            return MarketRegime.TREND_UP
        if below_count >= self._trend_threshold:
            return MarketRegime.TREND_DOWN

        return MarketRegime.MEAN_REVERT
