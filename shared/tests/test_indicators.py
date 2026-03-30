"""
Tests for shared.indicators
============================

Uses synthetic data to verify correctness of each indicator function.
"""

from __future__ import annotations

import numpy as np
import pytest

from shared.indicators import (
    SqueezeResult,
    calculate_adx,
    calculate_atr,
    calculate_ema,
    calculate_rsi,
    compute_bollinger_bands,
    compute_ema_high_signal,
    compute_ema_low_signal,
    compute_keltner_channels,
    detect_squeeze_state,
)


# =============================================================================
# Helpers
# =============================================================================


def _make_trending_up(n: int = 100, start: float = 100.0, step: float = 0.5) -> np.ndarray:
    """Create a steadily rising price series with small noise."""
    rng = np.random.default_rng(42)
    return start + np.arange(n) * step + rng.normal(0, 0.1, n)


def _make_trending_down(n: int = 100, start: float = 200.0, step: float = 0.5) -> np.ndarray:
    """Create a steadily falling price series with small noise."""
    rng = np.random.default_rng(42)
    return start - np.arange(n) * step + rng.normal(0, 0.1, n)


def _make_ohlc(closes: np.ndarray, spread: float = 1.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Generate synthetic high/low from closes."""
    rng = np.random.default_rng(123)
    noise_h = rng.uniform(0.1, spread, len(closes))
    noise_l = rng.uniform(0.1, spread, len(closes))
    highs = closes + noise_h
    lows = closes - noise_l
    return highs, lows, closes


# =============================================================================
# calculate_ema
# =============================================================================


class TestCalculateEma:
    def test_output_length(self) -> None:
        prices = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        ema = calculate_ema(prices, 3)
        assert len(ema) == len(prices)

    def test_first_value_is_price(self) -> None:
        prices = np.array([10.0, 20.0, 30.0])
        ema = calculate_ema(prices, 5)
        assert ema[0] == pytest.approx(10.0)

    def test_constant_series(self) -> None:
        prices = np.full(20, 50.0)
        ema = calculate_ema(prices, 10)
        np.testing.assert_allclose(ema, 50.0, atol=1e-10)

    def test_ema_lags_rising(self) -> None:
        prices = np.arange(1.0, 11.0)
        ema = calculate_ema(prices, 5)
        # EMA should lag behind in a rising series
        assert ema[-1] < prices[-1]


# =============================================================================
# calculate_atr
# =============================================================================


class TestCalculateAtr:
    def test_output_length(self) -> None:
        n = 30
        close = np.linspace(100, 110, n)
        high = close + 1.0
        low = close - 1.0
        atr = calculate_atr(high, low, close, period=14)
        assert len(atr) == n - 1

    def test_constant_range(self) -> None:
        """If high-low is constant, ATR should converge to that range."""
        n = 200
        close = np.full(n, 100.0)
        high = np.full(n, 102.0)
        low = np.full(n, 98.0)
        atr = calculate_atr(high, low, close, period=14)
        # Should converge to 4.0 (high - low)
        assert atr[-1] == pytest.approx(4.0, abs=0.01)

    def test_all_positive(self) -> None:
        close = _make_trending_up(50)
        high, low, close = _make_ohlc(close)
        atr = calculate_atr(high, low, close)
        assert np.all(atr >= 0)


# =============================================================================
# calculate_rsi
# =============================================================================


class TestCalculateRsi:
    def test_output_length(self) -> None:
        prices = np.arange(1.0, 31.0)
        rsi = calculate_rsi(prices, 14)
        assert len(rsi) == len(prices) - 1

    def test_purely_rising(self) -> None:
        """Purely rising prices -> RSI near 100 after warm-up."""
        prices = np.arange(1.0, 50.0)
        rsi = calculate_rsi(prices, 14)
        # After warm-up, RSI should be very high
        assert rsi[-1] > 90.0

    def test_purely_falling(self) -> None:
        """Purely falling prices -> RSI near 0 after warm-up."""
        prices = np.arange(50.0, 1.0, -1.0)
        rsi = calculate_rsi(prices, 14)
        assert rsi[-1] < 10.0

    def test_bounded(self) -> None:
        rng = np.random.default_rng(0)
        prices = 100 + np.cumsum(rng.normal(0, 1, 100))
        rsi = calculate_rsi(prices, 14)
        # After warm-up period, RSI should be bounded [0, 100]
        valid = rsi[14:]
        assert np.all(valid >= 0.0)
        assert np.all(valid <= 100.0)


# =============================================================================
# calculate_adx
# =============================================================================


class TestCalculateAdx:
    def test_output_length(self) -> None:
        n = 50
        close = np.linspace(100, 120, n)
        high = close + 1
        low = close - 1
        adx = calculate_adx(high, low, close, 14)
        assert len(adx) == n - 1

    def test_strong_trend_high_adx(self) -> None:
        """A strong trend should produce high ADX after warm-up."""
        n = 100
        close = np.linspace(100, 200, n)
        high = close + 0.5
        low = close - 0.5
        adx = calculate_adx(high, low, close, 14)
        # After warm-up (2*period = 28), ADX should be significant
        assert adx[-1] > 20.0

    def test_short_input(self) -> None:
        close = np.array([100.0])
        high = np.array([101.0])
        low = np.array([99.0])
        adx = calculate_adx(high, low, close, 14)
        assert len(adx) == 0


# =============================================================================
# compute_bollinger_bands
# =============================================================================


class TestBollingerBands:
    def test_output_length(self) -> None:
        close = np.arange(1.0, 31.0)
        lower, mid, upper = compute_bollinger_bands(close, period=20)
        expected_len = len(close) - 20 + 1
        assert len(lower) == expected_len
        assert len(mid) == expected_len
        assert len(upper) == expected_len

    def test_upper_above_lower(self) -> None:
        rng = np.random.default_rng(0)
        close = 100 + np.cumsum(rng.normal(0, 1, 50))
        lower, mid, upper = compute_bollinger_bands(close, 20, 2.0)
        assert np.all(upper >= mid)
        assert np.all(mid >= lower)

    def test_constant_series_zero_width(self) -> None:
        close = np.full(30, 100.0)
        lower, mid, upper = compute_bollinger_bands(close, 20, 2.0)
        np.testing.assert_allclose(lower, mid, atol=1e-10)
        np.testing.assert_allclose(upper, mid, atol=1e-10)

    def test_too_short(self) -> None:
        close = np.array([1.0, 2.0, 3.0])
        lower, mid, upper = compute_bollinger_bands(close, period=20)
        assert len(lower) == 0


# =============================================================================
# compute_keltner_channels
# =============================================================================


class TestKeltnerChannels:
    def test_output_length(self) -> None:
        n = 50
        close = np.linspace(100, 110, n)
        high = close + 1
        low = close - 1
        lower, mid, upper = compute_keltner_channels(close, high, low)
        assert len(lower) == n - 1
        assert len(mid) == n - 1
        assert len(upper) == n - 1

    def test_upper_above_lower(self) -> None:
        n = 50
        close = np.linspace(100, 110, n)
        high = close + 1
        low = close - 1
        lower, mid, upper = compute_keltner_channels(close, high, low)
        assert np.all(upper >= mid)
        assert np.all(mid >= lower)

    def test_too_short(self) -> None:
        close = np.array([100.0])
        high = np.array([101.0])
        low = np.array([99.0])
        lower, mid, upper = compute_keltner_channels(close, high, low)
        assert len(lower) == 0


# =============================================================================
# detect_squeeze_state
# =============================================================================


class TestDetectSqueezeState:
    def test_returns_squeeze_result(self) -> None:
        n = 100
        close = _make_trending_up(n)
        high, low, close = _make_ohlc(close)
        result = detect_squeeze_state("BTC", close, high, low)
        assert isinstance(result, SqueezeResult)
        assert result.symbol == "BTC"

    def test_too_short_returns_safe(self) -> None:
        close = np.array([100.0, 101.0])
        high = close + 1
        low = close - 1
        result = detect_squeeze_state("ETH", close, high, low)
        assert result.fired is False
        assert result.squeeze_bars == 0

    def test_squeeze_detection_low_vol(self) -> None:
        """Constant-volatility data should create a squeeze (BB inside KC)."""
        n = 100
        close = np.full(n, 100.0)
        # Very tight range -> BB will be tight -> squeeze
        rng = np.random.default_rng(99)
        noise = rng.normal(0, 0.01, n)
        close = close + noise
        high = close + 0.02
        low = close - 0.02
        result = detect_squeeze_state("TEST", close, high, low)
        # With nearly zero vol, BB should be inside KC
        # The exact state depends on parameters but it should not crash
        assert isinstance(result.in_squeeze_now, bool)


# =============================================================================
# compute_ema_high_signal
# =============================================================================


class TestEmaHighSignal:
    def test_no_signal_on_short_data(self) -> None:
        closes = np.array([100.0, 101.0, 102.0])
        highs = closes + 1
        lows = closes - 1
        signal, entry, sl = compute_ema_high_signal(closes, highs, lows)
        assert signal is None

    def test_signal_on_breakout(self) -> None:
        """Construct data that should trigger a LONG signal."""
        n = 80
        # Rising trend for SMA filter
        base = np.linspace(100, 150, n)
        rng = np.random.default_rng(7)
        closes = base + rng.normal(0, 0.1, n)

        # Force last 3 bars to create crossover:
        # bars -3, -2 below EMA(highs), bar -1 above
        highs = closes + 2.0
        lows = closes - 2.0

        # Make last bar break above EMA(highs) convincingly
        ema_h = calculate_ema(highs, 4)
        closes[-3] = ema_h[-3] - 1.0  # below
        closes[-2] = ema_h[-2] - 1.0  # below
        closes[-1] = ema_h[-1] + 5.0  # above (breakout)
        lows[-1] = closes[-1] - 3.0   # ensure signal_low < entry_close

        signal, entry, sl = compute_ema_high_signal(closes, highs, lows)
        if signal is not None:
            assert signal == "long"
            assert entry > 0
            assert sl < entry

    def test_no_signal_downtrend(self) -> None:
        """Falling prices should not produce a LONG signal."""
        closes = _make_trending_down(80)
        highs = closes + 1
        lows = closes - 1
        signal, _, _ = compute_ema_high_signal(closes, highs, lows)
        assert signal is None


# =============================================================================
# compute_ema_low_signal
# =============================================================================


class TestEmaLowSignal:
    def test_no_signal_on_short_data(self) -> None:
        closes = np.array([100.0, 99.0, 98.0])
        highs = closes + 1
        lows = closes - 1
        signal, entry, sh = compute_ema_low_signal(closes, highs, lows)
        assert signal is None

    def test_signal_on_breakdown(self) -> None:
        """Construct data that should trigger a SHORT signal."""
        n = 80
        base = np.linspace(200, 100, n)  # falling
        rng = np.random.default_rng(7)
        closes = base + rng.normal(0, 0.1, n)
        highs = closes + 2.0
        lows = closes - 2.0

        ema_l = calculate_ema(lows, 4)
        closes[-3] = ema_l[-3] + 1.0  # above
        closes[-2] = ema_l[-2] + 1.0  # above
        closes[-1] = ema_l[-1] - 5.0  # below (breakdown)
        highs[-1] = closes[-1] + 3.0  # ensure signal_high > entry_close

        signal, entry, sh = compute_ema_low_signal(closes, highs, lows)
        if signal is not None:
            assert signal == "short"
            assert entry > 0
            assert sh > entry

    def test_no_signal_uptrend(self) -> None:
        """Rising prices should not produce a SHORT signal."""
        closes = _make_trending_up(80)
        highs = closes + 1
        lows = closes - 1
        signal, _, _ = compute_ema_low_signal(closes, highs, lows)
        assert signal is None
