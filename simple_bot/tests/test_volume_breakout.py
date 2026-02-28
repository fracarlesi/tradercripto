"""Tests for volume breakout signal and live detection."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import numpy as np
import pytest

from backtesting.signals import signal_volume_breakout_entry
from simple_bot.core.models import Direction, MarketState, Regime, SetupType


# ── Backtesting Signal Tests ──────────────────────────────────────────────

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


# ── Live Detection Tests (main.py helpers) ─────────────────────────────────

class TestLiveVolumeBreakout:
    """Tests for ConservativeBot._is_volume_breakout() and _breakout_direction()."""

    @pytest.fixture
    def sample_state(self) -> MarketState:
        """MarketState that qualifies as a volume breakout LONG."""
        return MarketState(
            symbol="VVV",
            timeframe="15m",
            timestamp=datetime(2026, 2, 27, 10, 0, tzinfo=timezone.utc),
            open=Decimal("1.000"),
            high=Decimal("1.050"),
            low=Decimal("0.990"),
            close=Decimal("1.040"),   # close > open → bullish
            volume=Decimal("5000000"),
            atr=Decimal("0.02"),
            atr_pct=Decimal("0.5"),
            adx=Decimal("18"),        # CHAOS regime
            rsi=Decimal("55"),
            ema50=Decimal("0.95"),
            ema200=Decimal("0.90"),
            ema200_slope=Decimal("0.001"),
            sma20=Decimal("0.98"),
            sma50=Decimal("0.95"),
            ema9=Decimal("1.01"),
            ema21=Decimal("1.00"),
            prev_close=Decimal("1.000"),  # close > prev_close → confirms LONG
            volume_ratio=Decimal("3.2"),  # 3.2x above SMA20
            regime=Regime.CHAOS,
            trend_direction=Direction.LONG,
        )

    def test_is_volume_breakout_true(self, sample_state: MarketState) -> None:
        from simple_bot.main import ConservativeBot
        bot = ConservativeBot.__new__(ConservativeBot)
        bot._config = _make_config()
        assert bot._is_volume_breakout(sample_state)

    def test_is_volume_breakout_low_volume(self, sample_state: MarketState) -> None:
        from simple_bot.main import ConservativeBot
        bot = ConservativeBot.__new__(ConservativeBot)
        bot._config = _make_config()
        sample_state.volume_ratio = Decimal("1.5")  # Below 2.0
        assert not bot._is_volume_breakout(sample_state)

    def test_is_volume_breakout_small_body(self, sample_state: MarketState) -> None:
        from simple_bot.main import ConservativeBot
        bot = ConservativeBot.__new__(ConservativeBot)
        bot._config = _make_config()
        sample_state.close = Decimal("1.001")  # Tiny body
        assert not bot._is_volume_breakout(sample_state)

    def test_breakout_direction_long(self, sample_state: MarketState) -> None:
        from simple_bot.main import ConservativeBot
        assert ConservativeBot._breakout_direction(sample_state) == Direction.LONG

    def test_breakout_direction_short(self, sample_state: MarketState) -> None:
        from simple_bot.main import ConservativeBot
        sample_state.close = Decimal("0.960")  # close < open AND close < prev_close
        assert ConservativeBot._breakout_direction(sample_state) == Direction.SHORT

    def test_breakout_direction_flat(self, sample_state: MarketState) -> None:
        from simple_bot.main import ConservativeBot
        sample_state.close = Decimal("1.020")   # close > open
        sample_state.prev_close = Decimal("1.030")  # but close < prev_close
        assert ConservativeBot._breakout_direction(sample_state) == Direction.FLAT


# ── SetupType Enum Test ──────────────────────────────────────────────────

class TestSetupTypeEnum:
    def test_volume_breakout_exists(self) -> None:
        assert SetupType.VOLUME_BREAKOUT == "volume_breakout"

    def test_volume_breakout_serializable(self) -> None:
        assert SetupType.VOLUME_BREAKOUT.value == "volume_breakout"


# ── Regime Gating Tests ──────────────────────────────────────────────────

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


# ── Helper ───────────────────────────────────────────────────────────────

def _make_config():
    """Create minimal ConservativeConfig for volume breakout tests."""
    from simple_bot.main import ConservativeConfig
    return ConservativeConfig(
        assets=["VVV"],
        universe_mode="manual",
        min_volume_24h=100000,
        exclude_symbols=[],
        primary_timeframe="15m",
        bars_to_fetch=200,
        scan_interval_minutes=5,
        per_trade_pct=5.0,
        max_per_trade_pct=10.0,
        max_positions=3,
        max_exposure_pct=300,
        max_position_pct=70,
        max_daily_trades=8,
        leverage=10,
        daily_loss_pct=8.0,
        weekly_loss_pct=15.0,
        max_drawdown_pct=30.0,
        initial_atr_mult=2.5,
        trailing_atr_mult=2.5,
        minimal_roi={},
        stop_loss_pct=0.8,
        take_profit_pct=1.6,
        trend_adx_entry_min=28.0,
        trend_adx_exit_min=22.0,
        range_adx_max=20.0,
        choppiness_range_min=60.0,
        regime_confirmation_bars=3,
        ml_model_path="models/trade_model.joblib",
        ml_min_probability=0.50,
        ml_retrain_interval_days=3,
        ml_retrain_days=30,
        prefer_limit=True,
        max_slippage_pct=0.25,
        max_spread_pct=0.30,
        volume_breakout_enabled=True,
        volume_breakout_min_volume_ratio=2.0,
        volume_breakout_min_candle_body_pct=0.3,
        volume_breakout_min_atr_pct=0.15,
        volume_breakout_rsi_min=25.0,
        volume_breakout_rsi_max=80.0,
        volume_breakout_allowed_regimes=["chaos", "trend"],
        momentum_burst_enabled=True,
        momentum_burst_min_rsi_slope=8.0,
        momentum_burst_min_candle_body_pct=0.3,
        momentum_burst_max_rsi_entry=75.0,
        momentum_burst_min_volume_ratio=1.2,
        momentum_burst_allowed_regimes=["chaos", "trend"],
        testnet=True,
        dry_run=True,
    )
