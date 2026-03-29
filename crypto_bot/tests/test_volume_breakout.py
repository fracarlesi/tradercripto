"""Tests for volume breakout signal and live detection."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import numpy as np
import pytest

from backtesting.signals import signal_volume_breakout_entry
from crypto_bot.core.models import Direction, MarketState, Regime, SetupType


# -- Backtesting Signal Tests --

class TestSignalVolumeBreakoutEntry:
    """Tests for signal_volume_breakout_entry() in backtesting/signals.py."""

    def _make_ind(
        self,
        n: int = 25,
        close: float = 100.0,
        open_price: float = 99.0,
        prev_close: float = 99.5,
        volume: float = 200.0,
        vol_sma: float = 100.0,
        rsi: float = 50.0,
        atr_pct: float = 0.5,
    ) -> dict:
        """Build minimal indicator dict for volume breakout signal."""
        closes = np.full(n, close)
        opens = np.full(n, open_price)
        volumes = np.full(n, volume)
        vol_sma20 = np.full(n, vol_sma)
        rsi_arr = np.full(n, rsi)
        atr_pct_arr = np.full(n, atr_pct)

        # Set prev bar
        if n > 1:
            closes[n - 2] = prev_close

        return {
            "closes": closes,
            "opens": opens,
            "volumes": volumes,
            "vol_sma20": vol_sma20,
            "rsi": rsi_arr,
            "atr_pct": atr_pct_arr,
        }

    def test_long_breakout(self) -> None:
        """Should return 1 when bullish volume spike detected."""
        ind = self._make_ind(
            close=101.0, open_price=99.0, prev_close=99.5,
            volume=250.0, vol_sma=100.0,
        )
        assert signal_volume_breakout_entry(ind, 24) == 1

    def test_short_breakout(self) -> None:
        """Should return -1 when bearish volume spike detected."""
        ind = self._make_ind(
            close=98.0, open_price=100.0, prev_close=99.5,
            volume=250.0, vol_sma=100.0,
        )
        assert signal_volume_breakout_entry(ind, 24) == -1

    def test_no_signal_low_volume(self) -> None:
        """Should return 0 when volume is below threshold."""
        ind = self._make_ind(
            close=101.0, open_price=99.0, prev_close=99.5,
            volume=150.0, vol_sma=100.0,  # ratio = 1.5 < 2.0
        )
        assert signal_volume_breakout_entry(ind, 24) == 0

    def test_no_signal_small_body(self) -> None:
        """Should return 0 when candle body is too small."""
        ind = self._make_ind(
            close=100.1, open_price=100.0, prev_close=99.5,  # body = 0.1%
            volume=250.0, vol_sma=100.0,
        )
        assert signal_volume_breakout_entry(ind, 24) == 0

    def test_no_signal_low_atr(self) -> None:
        """Should return 0 when ATR is below threshold (dead market)."""
        ind = self._make_ind(
            close=101.0, open_price=99.0, prev_close=99.5,
            volume=250.0, vol_sma=100.0, atr_pct=0.05,  # < 0.15
        )
        assert signal_volume_breakout_entry(ind, 24) == 0

    def test_no_signal_rsi_overbought(self) -> None:
        """Should return 0 when RSI is too high."""
        ind = self._make_ind(
            close=101.0, open_price=99.0, prev_close=99.5,
            volume=250.0, vol_sma=100.0, rsi=85.0,  # > 80
        )
        assert signal_volume_breakout_entry(ind, 24) == 0

    def test_no_signal_rsi_oversold(self) -> None:
        """Should return 0 when RSI is too low."""
        ind = self._make_ind(
            close=101.0, open_price=99.0, prev_close=99.5,
            volume=250.0, vol_sma=100.0, rsi=20.0,  # < 25
        )
        assert signal_volume_breakout_entry(ind, 24) == 0

    def test_no_signal_mixed_direction(self) -> None:
        """Should return 0 when close > open but close < prev_close (no clear direction)."""
        ind = self._make_ind(
            close=100.5, open_price=100.0, prev_close=101.0,  # bullish candle but down from prev
            volume=250.0, vol_sma=100.0,
        )
        assert signal_volume_breakout_entry(ind, 24) == 0

    def test_idx_zero_returns_zero(self) -> None:
        """Cannot detect breakout at idx=0 (no previous bar)."""
        ind = self._make_ind()
        assert signal_volume_breakout_entry(ind, 0) == 0

    def test_nan_volume_sma_returns_zero(self) -> None:
        """Should return 0 when vol_sma20 is NaN."""
        ind = self._make_ind(volume=250.0)
        ind["vol_sma20"][24] = np.nan
        assert signal_volume_breakout_entry(ind, 24) == 0

    def test_custom_thresholds(self) -> None:
        """Should respect custom threshold parameters."""
        ind = self._make_ind(
            close=101.0, open_price=99.0, prev_close=99.5,
            volume=280.0, vol_sma=100.0,
        )
        # With higher volume threshold, should fail
        assert signal_volume_breakout_entry(ind, 24, min_volume_ratio=3.0) == 0
        # With lower threshold, should pass
        assert signal_volume_breakout_entry(ind, 24, min_volume_ratio=2.0) == 1

    def test_missing_required_key_returns_zero(self) -> None:
        """Should return 0 when a required indicator key is missing."""
        ind = {"closes": np.array([100.0, 101.0]), "opens": np.array([99.0, 99.0])}
        assert signal_volume_breakout_entry(ind, 1) == 0


# -- SetupType Enum Test --

class TestSetupTypeEnum:
    def test_volume_breakout_exists(self) -> None:
        assert SetupType.VOLUME_BREAKOUT == "volume_breakout"

    def test_volume_breakout_serializable(self) -> None:
        assert SetupType.VOLUME_BREAKOUT.value == "volume_breakout"


# -- Regime Gating Tests --

class TestRegimeGating:
    """Verify volume breakout works in CHAOS but EMA crossover requires TREND."""

    def test_chaos_regime_not_in_trend_allowed(self) -> None:
        """CHAOS should be in volume_breakout allowed_regimes but not in crossover."""
        allowed = {"chaos", "trend"}
        assert "chaos" in allowed
        assert Regime.CHAOS.value.lower() in allowed

    def test_range_regime_blocked(self) -> None:
        """RANGE should NOT be in volume_breakout allowed_regimes."""
        allowed = {"chaos", "trend"}
        assert "range" not in allowed
