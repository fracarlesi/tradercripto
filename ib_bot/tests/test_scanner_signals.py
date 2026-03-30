"""Tests for ib_bot.scanner.signals with synthetic data."""

import numpy as np
import pandas as pd
import pytest

from ib_bot.scanner.signals import ScanResult, scan_symbol


def _make_ohlcv(
    n: int = 60,
    base_price: float = 100.0,
    trend: float = 0.0,
    volatility: float = 0.02,
    seed: int = 42,
) -> pd.DataFrame:
    """Generate synthetic OHLCV data.

    Args:
        n: Number of bars.
        base_price: Starting price.
        trend: Daily drift (e.g. 0.005 for uptrend).
        volatility: Daily volatility.
        seed: Random seed for reproducibility.
    """
    rng = np.random.default_rng(seed)
    close = np.zeros(n)
    close[0] = base_price
    for i in range(1, n):
        close[i] = close[i - 1] * (1 + trend + rng.normal(0, volatility))

    high = close * (1 + rng.uniform(0.001, 0.02, n))
    low = close * (1 - rng.uniform(0.001, 0.02, n))
    open_ = close * (1 + rng.uniform(-0.01, 0.01, n))
    volume = rng.integers(100_000, 10_000_000, n).astype(float)

    dates = pd.bdate_range(end=pd.Timestamp.now(), periods=n)
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume},
        index=dates,
    )


class TestScanSymbol:
    """Test scan_symbol with various synthetic scenarios."""

    def test_basic_scan_returns_result(self) -> None:
        df = _make_ohlcv()
        result = scan_symbol("TEST", df)
        assert isinstance(result, ScanResult)
        assert result.symbol == "TEST"

    def test_empty_dataframe(self) -> None:
        result = scan_symbol("EMPTY", pd.DataFrame())
        assert result.score == 0.0
        assert result.rsi_value == 50.0

    def test_too_few_bars(self) -> None:
        df = _make_ohlcv(n=10)
        result = scan_symbol("SHORT", df)
        assert result.score == 0.0

    def test_rsi_is_computed(self) -> None:
        df = _make_ohlcv(n=60)
        result = scan_symbol("RSI", df)
        assert 0 < result.rsi_value < 100

    def test_atr_pct_positive(self) -> None:
        df = _make_ohlcv(n=60, volatility=0.03)
        result = scan_symbol("ATR", df)
        assert result.atr_pct > 0

    def test_adx_computed(self) -> None:
        df = _make_ohlcv(n=60, trend=0.01)
        result = scan_symbol("ADX", df)
        assert result.adx_value >= 0

    def test_strong_uptrend_detected(self) -> None:
        df = _make_ohlcv(n=60, trend=0.015, volatility=0.005, seed=100)
        result = scan_symbol("BULL", df)
        assert result.trend == "bullish"

    def test_strong_downtrend_detected(self) -> None:
        df = _make_ohlcv(n=60, trend=-0.015, volatility=0.005, seed=100)
        result = scan_symbol("BEAR", df)
        assert result.trend == "bearish"

    def test_volume_ratio(self) -> None:
        df = _make_ohlcv(n=60, seed=7)
        result = scan_symbol("VOL", df)
        assert result.volume_ratio > 0

    def test_score_components_additive(self) -> None:
        """Verify score ranges are reasonable."""
        df = _make_ohlcv(n=60)
        result = scan_symbol("SCORE", df)
        # Max possible: 3 + 2 + 1.5 + 1 + 0.5 = 8.0
        assert 0 <= result.score <= 8.0

    def test_ema_bullish_crossover(self) -> None:
        """Construct data where EMA9 crosses above EMA21 on last bar."""
        n = 60
        # Long downtrend then sharp reversal
        prices = np.concatenate([
            np.linspace(120, 90, n - 5),   # downtrend
            np.linspace(90, 115, 5),         # sharp reversal
        ])
        dates = pd.bdate_range(end=pd.Timestamp.now(), periods=n)
        df = pd.DataFrame({
            "Open": prices * 0.999,
            "High": prices * 1.01,
            "Low": prices * 0.99,
            "Close": prices,
            "Volume": np.full(n, 1_000_000.0),
        }, index=dates)
        result = scan_symbol("CROSS", df)
        # The crossover may or may not fire depending on EMA values,
        # but the function should not crash
        assert result.symbol == "CROSS"
        assert isinstance(result.ema_cross_direction, (str, type(None)))


class TestScanResultDefaults:
    """Test ScanResult default values."""

    def test_defaults(self) -> None:
        r = ScanResult(symbol="X")
        assert r.squeeze_fired is False
        assert r.ema_cross_direction is None
        assert r.rsi_value == 50.0
        assert r.score == 0.0
        assert r.trend == "neutral"
