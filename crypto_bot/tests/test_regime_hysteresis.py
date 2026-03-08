"""
Tests for regime detection level hysteresis
============================================

Tests the anti-whipsaw regime detection:
1. Level hysteresis: separate ADX entry/exit thresholds
2. EMA200 slope NOT used in TREND classification
3. Confirmation bars (N consecutive readings required)
4. init_confirmed_regime_for_symbols (restart safety)
5. Grace period in execution engine for new positions

Run:
    pytest crypto_bot/tests/test_regime_hysteresis.py -v
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, AsyncMock

from crypto_bot.core.models import Regime
from crypto_bot.services.market_state import MarketStateConfig, MarketStateService
from crypto_bot.services.execution_engine import (
    ExecutionEngineService,
    ExecutionPosition,
    PositionStatus,
)
from crypto_bot.services.message_bus import Message
from crypto_bot.core.enums import Topic


# =============================================================================
# Helpers
# =============================================================================

def _make_service(
    trend_adx_entry_min: float = 28.0,
    trend_adx_exit_min: float = 22.0,
    range_adx_max: float = 20.0,
    choppiness_range_min: float = 60.0,
    confirmation_bars: int = 3,
) -> MarketStateService:
    """Create a MarketStateService with test configuration."""
    config = MarketStateConfig(
        assets=["BTC", "ETH"],
        trend_adx_entry_min=trend_adx_entry_min,
        trend_adx_exit_min=trend_adx_exit_min,
        range_adx_max=range_adx_max,
        choppiness_range_min=choppiness_range_min,
        regime_confirmation_bars=confirmation_bars,
    )
    svc = MarketStateService.__new__(MarketStateService)
    svc._state_config = config
    svc._confirmed_regime = {}
    svc._regime_change_counter = {}
    svc._pending_regime = {}
    svc._logger = MagicMock()
    return svc


def _detect(svc: MarketStateService, symbol: str, adx: float,
            choppiness: float = 50.0, ema200_slope: float = 0.001) -> Regime:
    """Shortcut for calling _detect_regime."""
    return svc._detect_regime(
        adx=adx,
        ema200_slope=ema200_slope,
        choppiness=choppiness,
        symbol=symbol,
    )


def _make_engine(regime_exit_grace_minutes: int = 5) -> ExecutionEngineService:
    """Create an ExecutionEngineService with mocked dependencies."""
    engine = ExecutionEngineService.__new__(ExecutionEngineService)
    engine._logger = MagicMock()
    engine.active_positions = {}
    engine.client = AsyncMock()
    engine.client.cancel_order = AsyncMock()
    engine.client.close_position = AsyncMock()
    # Provide regime config for grace period
    regime_cfg = MagicMock()
    regime_cfg.regime_exit_grace_minutes = regime_exit_grace_minutes
    engine._bot_config = MagicMock()
    engine._bot_config.regime = regime_cfg
    return engine


def _make_position(
    symbol: str = "BTC",
    side: str = "long",
    entry_regime: str | None = "trend",
    status: PositionStatus = PositionStatus.OPEN,
    opened_at: datetime | None = None,
) -> ExecutionPosition:
    """Create a minimal ExecutionPosition for testing."""
    return ExecutionPosition(
        symbol=symbol,
        side=side,
        size=0.01,
        entry_price=95000.0,
        current_price=95100.0,
        status=status,
        opened_at=opened_at or datetime.now(timezone.utc),
        entry_regime=entry_regime,
    )


def _regime_message(symbol: str, regime: str) -> Message:
    """Create a regime change message."""
    return Message(
        topic=Topic.REGIME,
        payload={
            "symbol": symbol,
            "regime": regime,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "adx": 18.0,
            "trend_direction": "long",
        },
    )


# =============================================================================
# Level Hysteresis Tests
# =============================================================================

class TestLevelHysteresis:
    """Test that separate entry/exit ADX thresholds prevent whipsaw."""

    def test_entry_requires_stricter_threshold(self) -> None:
        """ADX=26 (between exit=22 and entry=28) should NOT enter TREND."""
        svc = _make_service()
        regime = _detect(svc, "BTC", adx=26.0)
        # ADX=26 > range_adx_max=20 but < entry=28 → CHAOS
        assert regime == Regime.CHAOS

    def test_entry_at_exact_threshold(self) -> None:
        """ADX=28 (exactly entry threshold) should enter TREND."""
        svc = _make_service()
        regime = _detect(svc, "BTC", adx=28.0)
        assert regime == Regime.TREND

    def test_entry_above_threshold(self) -> None:
        """ADX=35 (well above entry threshold) should enter TREND."""
        svc = _make_service()
        regime = _detect(svc, "BTC", adx=35.0)
        assert regime == Regime.TREND

    def test_stay_in_trend_with_lenient_threshold(self) -> None:
        """Once in TREND, ADX=24 (above exit=22 but below entry=28) stays TREND."""
        svc = _make_service(confirmation_bars=1)
        # First: enter TREND
        _detect(svc, "BTC", adx=30.0)
        assert svc._confirmed_regime["BTC"] == Regime.TREND

        # ADX drops to 24 — still above exit threshold of 22
        regime = _detect(svc, "BTC", adx=24.0)
        assert regime == Regime.TREND

    def test_exit_trend_when_below_exit_threshold(self) -> None:
        """ADX drops below exit threshold (22) should eventually leave TREND."""
        svc = _make_service(confirmation_bars=1)
        # Enter TREND
        _detect(svc, "BTC", adx=30.0)
        assert svc._confirmed_regime["BTC"] == Regime.TREND

        # ADX drops to 21 — below exit threshold and <= range_adx_max=20?
        # No, 21 > 20, so it becomes CHAOS after 1 bar
        regime = _detect(svc, "BTC", adx=21.0)
        assert regime == Regime.CHAOS

    def test_hysteresis_band_prevents_whipsaw(self) -> None:
        """ADX oscillating between 23-27 should not whipsaw after entering TREND."""
        svc = _make_service(confirmation_bars=1)
        # Enter TREND with strong ADX
        _detect(svc, "BTC", adx=30.0)
        assert svc._confirmed_regime["BTC"] == Regime.TREND

        # Oscillate in the hysteresis band (22-28)
        for adx_val in [25.0, 23.0, 27.0, 24.0, 26.0]:
            regime = _detect(svc, "BTC", adx=adx_val)
            assert regime == Regime.TREND, (
                f"ADX={adx_val} should stay TREND (above exit=22)"
            )

    def test_reentry_requires_stricter_threshold(self) -> None:
        """After losing TREND, re-entering requires the higher entry threshold."""
        svc = _make_service(confirmation_bars=1)
        # Enter TREND
        _detect(svc, "BTC", adx=30.0)
        assert svc._confirmed_regime["BTC"] == Regime.TREND

        # Drop below exit threshold — leaves TREND
        # ADX=15 <= range_adx_max=20 so regime becomes RANGE (not CHAOS)
        _detect(svc, "BTC", adx=15.0)
        assert svc._confirmed_regime["BTC"] == Regime.RANGE

        # ADX=25 — above range_adx_max but below entry threshold = CHAOS
        regime = _detect(svc, "BTC", adx=25.0)
        assert regime == Regime.CHAOS

        # ADX=28 — at entry threshold, re-enters TREND
        regime = _detect(svc, "BTC", adx=28.0)
        assert regime == Regime.TREND


# =============================================================================
# EMA200 Slope Tests
# =============================================================================

class TestEma200SlopeRemoved:
    """Test that ema200_slope is NOT used in TREND classification."""

    def test_trend_without_ema200_slope(self) -> None:
        """ADX=30 should be TREND even with ema200_slope=0 (flat EMA200)."""
        svc = _make_service()
        regime = _detect(svc, "BTC", adx=30.0, ema200_slope=0.0)
        assert regime == Regime.TREND

    def test_trend_with_negative_ema200_slope(self) -> None:
        """ADX=30 should be TREND regardless of negative ema200_slope."""
        svc = _make_service()
        regime = _detect(svc, "BTC", adx=30.0, ema200_slope=-0.001)
        assert regime == Regime.TREND

    def test_trend_with_tiny_ema200_slope(self) -> None:
        """ADX=30 should be TREND even with extremely small ema200_slope."""
        svc = _make_service()
        regime = _detect(svc, "BTC", adx=30.0, ema200_slope=0.00001)
        assert regime == Regime.TREND

    def test_trx_scenario_adx33_flat_ema200(self) -> None:
        """TRX-like scenario: ADX=33.5, flat EMA200 should stay TREND."""
        svc = _make_service(confirmation_bars=1)
        # Enter TREND
        _detect(svc, "TRX", adx=35.0, ema200_slope=0.001)
        assert svc._confirmed_regime["TRX"] == Regime.TREND

        # EMA200 flattens — should NOT drop to CHAOS
        regime = _detect(svc, "TRX", adx=33.5, ema200_slope=0.00005)
        assert regime == Regime.TREND


# =============================================================================
# Confirmation Bars Tests
# =============================================================================

class TestConfirmationBars:
    """Test that regime changes require N consecutive confirming bars."""

    def test_default_confirmation_is_3(self) -> None:
        """Default confirmation bars should be 3."""
        config = MarketStateConfig()
        assert config.regime_confirmation_bars == 3

    def test_single_bar_not_enough(self) -> None:
        """One bar of non-TREND should not change regime."""
        svc = _make_service(confirmation_bars=3)
        _detect(svc, "BTC", adx=30.0)
        assert svc._confirmed_regime["BTC"] == Regime.TREND

        # 1 bar of low ADX
        regime = _detect(svc, "BTC", adx=15.0)
        assert regime == Regime.TREND  # Not confirmed yet

    def test_two_bars_not_enough(self) -> None:
        """Two bars of non-TREND should not change regime with confirmation=3."""
        svc = _make_service(confirmation_bars=3)
        _detect(svc, "BTC", adx=30.0)

        _detect(svc, "BTC", adx=15.0)
        regime = _detect(svc, "BTC", adx=15.0)
        assert regime == Regime.TREND  # Still not confirmed

    def test_three_bars_confirms_change(self) -> None:
        """Three consecutive bars of non-TREND should change regime."""
        svc = _make_service(confirmation_bars=3)
        _detect(svc, "BTC", adx=30.0)

        # ADX=15 <= range_adx_max=20 so this transitions to RANGE (not CHAOS)
        _detect(svc, "BTC", adx=15.0)  # bar 1
        _detect(svc, "BTC", adx=15.0)  # bar 2
        regime = _detect(svc, "BTC", adx=15.0)  # bar 3 — confirmed
        assert regime == Regime.RANGE

    def test_interrupted_confirmation_resets(self) -> None:
        """If a confirming sequence is interrupted, counter resets."""
        svc = _make_service(confirmation_bars=3)
        _detect(svc, "BTC", adx=30.0)

        # ADX=15 <= range_adx_max=20 → pending RANGE (not CHAOS)
        _detect(svc, "BTC", adx=15.0)  # bar 1 — pending RANGE
        _detect(svc, "BTC", adx=15.0)  # bar 2 — pending RANGE
        _detect(svc, "BTC", adx=30.0)  # back to TREND — resets counter

        # Need 3 more consecutive bars
        _detect(svc, "BTC", adx=15.0)  # bar 1
        _detect(svc, "BTC", adx=15.0)  # bar 2
        regime = _detect(svc, "BTC", adx=15.0)  # bar 3
        assert regime == Regime.RANGE

    def test_different_pending_regime_resets_counter(self) -> None:
        """If pending regime changes (CHAOS -> RANGE), counter resets to 1."""
        svc = _make_service(confirmation_bars=3)
        _detect(svc, "BTC", adx=30.0)

        # ADX=21 > range_adx_max=20 → CHAOS pending
        _detect(svc, "BTC", adx=21.0)  # CHAOS pending
        _detect(svc, "BTC", adx=21.0)  # CHAOS bar 2

        # Now ADX=18 <= range_adx_max=20 → RANGE (different regime, resets counter)
        _detect(svc, "BTC", adx=18.0)  # RANGE bar 1
        regime = _detect(svc, "BTC", adx=18.0)  # RANGE bar 2
        assert regime == Regime.TREND  # Not yet confirmed (only 2 of 3)


# =============================================================================
# Init Confirmed Regime Tests (Restart Safety)
# =============================================================================

class TestInitConfirmedRegime:
    """Test init_confirmed_regime_for_symbols for restart safety."""

    def test_init_sets_trend_for_open_positions(self) -> None:
        """Symbols with open positions start with confirmed TREND."""
        svc = _make_service()
        svc.init_confirmed_regime_for_symbols(["BTC", "ETH"])

        assert svc._confirmed_regime["BTC"] == Regime.TREND
        assert svc._confirmed_regime["ETH"] == Regime.TREND

    def test_init_does_not_overwrite_existing(self) -> None:
        """If a symbol already has confirmed regime, don't overwrite it."""
        svc = _make_service()
        svc._confirmed_regime["BTC"] = Regime.CHAOS
        svc._regime_change_counter["BTC"] = 0

        svc.init_confirmed_regime_for_symbols(["BTC"])

        # Should NOT overwrite
        assert svc._confirmed_regime["BTC"] == Regime.CHAOS

    def test_init_prevents_immediate_regime_drop(self) -> None:
        """After init, first reading below entry but above exit stays TREND."""
        svc = _make_service(confirmation_bars=3)
        svc.init_confirmed_regime_for_symbols(["BTC"])

        # First reading: ADX=24 (below entry=28, above exit=22)
        regime = _detect(svc, "BTC", adx=24.0)
        assert regime == Regime.TREND  # Stays TREND because exit threshold is 22

    def test_without_init_first_reading_becomes_confirmed(self) -> None:
        """Without init, first reading becomes confirmed immediately (the bug)."""
        svc = _make_service(confirmation_bars=3)
        # No init — first reading at ADX=24 (> range_adx_max=20, < entry=28)
        # becomes CHAOS immediately
        regime = _detect(svc, "BTC", adx=24.0)
        assert regime == Regime.CHAOS  # Immediately confirmed as CHAOS

    def test_init_empty_list_is_noop(self) -> None:
        """Initializing with empty list is a no-op."""
        svc = _make_service()
        svc.init_confirmed_regime_for_symbols([])
        assert len(svc._confirmed_regime) == 0


# =============================================================================
# Grace Period Tests (Execution Engine)
# =============================================================================

class TestRegimeGracePeriod:
    """Test grace period for new positions in _handle_regime_change."""

    @pytest.mark.asyncio
    async def test_new_position_regime_change_ignored(self) -> None:
        """Position opened < 5 min ago ignores regime change (default grace)."""
        engine = _make_engine()  # default grace = 5 min
        # Position opened 2 minutes ago
        opened_at = datetime.now(timezone.utc) - timedelta(minutes=2)
        engine.active_positions["BTC"] = _make_position(
            "BTC", entry_regime="trend", opened_at=opened_at,
        )

        await engine._handle_regime_change(_regime_message("BTC", "chaos"))

        engine.client.close_position.assert_not_called()
        assert engine.active_positions["BTC"].status == PositionStatus.OPEN

    @pytest.mark.asyncio
    async def test_old_position_regime_change_closes(self) -> None:
        """Position opened > 5 min ago is closed on regime change."""
        engine = _make_engine()  # default grace = 5 min
        # Position opened 10 minutes ago
        opened_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        engine.active_positions["BTC"] = _make_position(
            "BTC", entry_regime="trend", opened_at=opened_at,
        )

        await engine._handle_regime_change(_regime_message("BTC", "chaos"))

        engine.client.close_position.assert_called_once_with("BTC")
        assert engine.active_positions["BTC"].exit_reason == "regime_change"

    @pytest.mark.asyncio
    async def test_position_at_exact_grace_boundary(self) -> None:
        """Position at exactly 5 min should be closed (>= boundary)."""
        engine = _make_engine()  # default grace = 5 min
        opened_at = datetime.now(timezone.utc) - timedelta(minutes=5, seconds=1)
        engine.active_positions["BTC"] = _make_position(
            "BTC", entry_regime="trend", opened_at=opened_at,
        )

        await engine._handle_regime_change(_regime_message("BTC", "range"))

        engine.client.close_position.assert_called_once_with("BTC")

    @pytest.mark.asyncio
    async def test_same_regime_no_action_regardless_of_age(self) -> None:
        """Same regime never triggers close, regardless of position age."""
        engine = _make_engine()
        opened_at = datetime.now(timezone.utc) - timedelta(hours=2)
        engine.active_positions["BTC"] = _make_position(
            "BTC", entry_regime="trend", opened_at=opened_at,
        )

        await engine._handle_regime_change(_regime_message("BTC", "trend"))

        engine.client.close_position.assert_not_called()


# =============================================================================
# Config Defaults Tests
# =============================================================================

class TestConfigDefaults:
    """Test MarketStateConfig default values."""

    def test_default_entry_threshold(self) -> None:
        """Default entry threshold should be 28."""
        config = MarketStateConfig()
        assert config.trend_adx_entry_min == 28.0

    def test_default_exit_threshold(self) -> None:
        """Default exit threshold should be 22."""
        config = MarketStateConfig()
        assert config.trend_adx_exit_min == 22.0

    def test_no_ema_slope_threshold(self) -> None:
        """MarketStateConfig should NOT have ema_slope_threshold field."""
        config = MarketStateConfig()
        assert not hasattr(config, "ema_slope_threshold")

    def test_confirmation_bars_default_is_3(self) -> None:
        """Default confirmation bars should be 3."""
        config = MarketStateConfig()
        assert config.regime_confirmation_bars == 3


# =============================================================================
# Integration-style: Full Regime Lifecycle
# =============================================================================

class TestRegimeLifecycle:
    """End-to-end regime lifecycle with hysteresis and confirmation."""

    def test_full_lifecycle_trend_to_range_and_back(self) -> None:
        """Full lifecycle: enter TREND -> stay -> exit to RANGE -> re-enter."""
        svc = _make_service(confirmation_bars=2)

        # 1. Enter TREND with ADX=30
        r = _detect(svc, "BTC", adx=30.0)
        assert r == Regime.TREND

        # 2. Stay in TREND with ADX=24 (in hysteresis band)
        r = _detect(svc, "BTC", adx=24.0)
        assert r == Regime.TREND

        # 3. ADX drops to 15 — first bar below exit threshold
        #    ADX=15 <= range_adx_max=20 → pending RANGE
        r = _detect(svc, "BTC", adx=15.0)
        assert r == Regime.TREND  # Only 1 of 2 bars

        # 4. Second bar at ADX=15 — confirmed RANGE
        r = _detect(svc, "BTC", adx=15.0)
        assert r == Regime.RANGE

        # 5. ADX=25 — above range_adx_max but below entry=28 → CHAOS
        #    1 of 2 bars for CHAOS confirmation
        r = _detect(svc, "BTC", adx=25.0)
        assert r == Regime.RANGE  # Not yet confirmed

        # 6. ADX=29 — above entry threshold, pending TREND (resets counter from CHAOS)
        r = _detect(svc, "BTC", adx=29.0)
        assert r == Regime.RANGE  # 1 of 2 bars

        # 7. Second bar at ADX=29 — confirmed TREND
        r = _detect(svc, "BTC", adx=29.0)
        assert r == Regime.TREND

    def test_independent_symbols(self) -> None:
        """Regime state is independent per symbol."""
        svc = _make_service(confirmation_bars=1)

        # BTC enters TREND
        _detect(svc, "BTC", adx=30.0)
        assert svc._confirmed_regime["BTC"] == Regime.TREND

        # ETH stays in RANGE (ADX=15 <= range_adx_max=20)
        _detect(svc, "ETH", adx=15.0)
        assert svc._confirmed_regime["ETH"] == Regime.RANGE

        # BTC regime unaffected by ETH
        r = _detect(svc, "BTC", adx=24.0)
        assert r == Regime.TREND
