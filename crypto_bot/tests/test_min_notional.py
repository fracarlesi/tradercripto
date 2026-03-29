"""
Tests for Hyperliquid minimum notional order check
====================================================

Ensures that the risk manager rejects orders with notional below the
exchange minimum ($10), preventing rejected order spam on ntfy.

Run:
    pytest crypto_bot/tests/test_min_notional.py -v
"""

from datetime import datetime, timezone
from decimal import Decimal

from crypto_bot.core.models import (
    Setup,
    Regime,
    Direction,
    SetupType,
)
from crypto_bot.services.risk_manager import (
    HYPERLIQUID_MIN_ORDER_NOTIONAL,
    RiskConfig,
    RiskManagerService,
)


# ── helpers ──────────────────────────────────────────────────────────────

_SETUP_DEFAULTS: dict[str, object] = dict(
    atr_pct=Decimal("0.5"),
    model_tp_pct=2.5,
    model_sl_pct=1.0,
    llm_approved=True,
    llm_confidence=Decimal("0.7"),
    llm_reason="test",
    entry_reason="test",
    entry_confidence=0.7,
    entry_trigger_details="",
)


def _make_setup(**overrides: object) -> Setup:
    defaults: dict[str, object] = dict(
        id="setup_test",
        symbol="WIF",
        timestamp=datetime.now(timezone.utc),
        setup_type=SetupType.TREND_BREAKOUT,
        direction=Direction.LONG,
        regime=Regime.TREND,
        entry_price=Decimal("0.17"),
        stop_price=Decimal("0.1674"),
        stop_distance_pct=Decimal("1.5"),
        atr=Decimal("0.005"),
        adx=Decimal("35"),
        rsi=Decimal("55"),
        setup_quality=Decimal("0.75"),
        confidence=Decimal("0.8"),
    )
    defaults.update(_SETUP_DEFAULTS)
    defaults.update(overrides)
    return Setup(**defaults)  # type: ignore[arg-type]


def _make_service(equity: Decimal, **config_kw: object) -> RiskManagerService:
    config = RiskConfig(**config_kw)  # type: ignore[arg-type]
    svc = RiskManagerService(config=config)
    svc._current_equity = equity
    return svc


# ── tests ────────────────────────────────────────────────────────────────


class TestMinNotionalCheck:
    """Risk manager rejects orders below Hyperliquid minimum notional."""

    def test_constant_is_ten_dollars(self):
        """Sanity: the constant matches Hyperliquid's documented minimum."""
        assert HYPERLIQUID_MIN_ORDER_NOTIONAL == Decimal("10")

    def test_reject_when_notional_below_minimum(self):
        """With tiny equity, position cap can push notional below $10 -> rejected."""
        # Equity $30, max_position_pct 25% -> cap = $7.50 < $10
        svc = _make_service(
            equity=Decimal("30"),
            max_position_pct=25.0,
        )
        setup = _make_setup()
        params = svc._calculate_risk_params(setup)

        assert params.size_approved is False
        assert "below exchange minimum" in (params.rejection_reason or "")
        assert params.notional_value == Decimal("0")

    def test_approve_when_notional_at_minimum(self):
        """Notional exactly at $10 is allowed (exchange accepts >= $10)."""
        # Equity $40, max_position_pct 25% -> cap = $10.00 == minimum
        svc = _make_service(
            equity=Decimal("40"),
            max_position_pct=25.0,
        )
        setup = _make_setup()
        params = svc._calculate_risk_params(setup)

        assert params.size_approved is True
        assert params.notional_value >= HYPERLIQUID_MIN_ORDER_NOTIONAL

    def test_approve_when_notional_well_above_minimum(self):
        """Normal equity: notional is well above $10 -> approved."""
        svc = _make_service(equity=Decimal("1000"))
        setup = _make_setup()
        params = svc._calculate_risk_params(setup)

        assert params.size_approved is True
        assert params.notional_value >= HYPERLIQUID_MIN_ORDER_NOTIONAL

    def test_reject_does_not_publish_intent(self):
        """Rejected setup should have zero position_size (no intent created)."""
        svc = _make_service(
            equity=Decimal("20"),
            max_position_pct=20.0,  # cap = $4 < $10
        )
        setup = _make_setup()
        params = svc._calculate_risk_params(setup)

        assert params.size_approved is False
        assert params.position_size == Decimal("0")

    def test_log_message_includes_notional(self, caplog):
        """Warning log should mention the actual notional value."""
        import logging

        svc = _make_service(
            equity=Decimal("30"),
            max_position_pct=25.0,
        )
        setup = _make_setup()

        with caplog.at_level(logging.WARNING, logger="crypto_bot.services.risk_manager"):
            svc._calculate_risk_params(setup)

        notional_warnings = [
            r for r in caplog.records
            if "below exchange minimum" in r.getMessage()
        ]
        assert len(notional_warnings) >= 1
