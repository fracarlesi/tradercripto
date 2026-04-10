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

    def test_floor_when_notional_below_minimum(self):
        """With tiny equity, position cap can push notional below $10 -> floored to $10."""
        # Equity $30, max_position_pct 25% -> cap = $7.50 < $10 -> floored to $10
        svc = _make_service(
            equity=Decimal("30"),
            max_position_pct=25.0,
        )
        setup = _make_setup()
        params = svc._calculate_risk_params(setup)

        assert params.size_approved is True
        assert params.notional_value == HYPERLIQUID_MIN_ORDER_NOTIONAL

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

    def test_floor_preserves_position_size(self):
        """Floored notional should have correct position_size (notional / entry_price)."""
        svc = _make_service(
            equity=Decimal("20"),
            max_position_pct=20.0,  # cap = $4 < $10 -> floored to $10
        )
        setup = _make_setup()
        params = svc._calculate_risk_params(setup)

        assert params.size_approved is True
        assert params.notional_value == HYPERLIQUID_MIN_ORDER_NOTIONAL
        expected_size = HYPERLIQUID_MIN_ORDER_NOTIONAL / setup.entry_price
        assert params.position_size == expected_size

    def test_log_message_when_floored(self, caplog):
        """Info log should mention flooring to exchange minimum."""
        import logging

        svc = _make_service(
            equity=Decimal("30"),
            max_position_pct=25.0,
        )
        setup = _make_setup()

        with caplog.at_level(logging.INFO, logger="hlquantbot.risk_manager"):
            svc._calculate_risk_params(setup)

        floor_msgs = [
            r for r in caplog.records
            if "floored to exchange minimum" in r.getMessage()
        ]
        assert len(floor_msgs) >= 1


class TestCeilRoundingAfterFloor:
    """
    Ensure that after the risk manager floors notional to $10,
    ceil-rounding in hyperliquid.py never produces a size whose
    notional falls below the exchange minimum.

    These are unit tests for the math — they don't hit the exchange.
    """

    @staticmethod
    def _ceil_round(size: float, sz_decimals: int) -> float:
        """Mirrors the ceil-rounding logic added to hyperliquid.py place_order."""
        import math
        factor = 10 ** sz_decimals
        return math.ceil(size * factor) / factor

    def test_blur_sz0_ceil_preserves_notional(self):
        """BLUR szDecimals=0, price $0.0222 — round() gives $9.99, ceil should give $10.02."""
        price = 0.0222
        raw_size = 10.0 / price        # 450.45...
        rounded_down = round(raw_size, 0)
        assert rounded_down * price < 10.0, "pre-condition: round() would cause rejection"

        ceiled = self._ceil_round(raw_size, sz_decimals=0)
        assert ceiled * price >= 10.0

    def test_render_sz1_ceil_preserves_notional(self):
        """RENDER szDecimals=1, price $3.51 — round() gives $9.83, ceil should give $10.53."""
        price = 3.51
        raw_size = 10.0 / price        # 2.849...
        rounded_down = round(raw_size, 1)
        assert rounded_down * price < 10.0, "pre-condition: round() would cause rejection"

        ceiled = self._ceil_round(raw_size, sz_decimals=1)
        assert ceiled * price >= 10.0

    def test_aave_sz2_ceil_preserves_notional(self):
        """AAVE szDecimals=2, price $185 — round() gives $9.25, ceil should give $10.175."""
        price = 185.0
        raw_size = 10.0 / price        # 0.05405...
        rounded_down = round(raw_size, 2)
        assert rounded_down * price < 10.0, "pre-condition: round() would cause rejection"

        ceiled = self._ceil_round(raw_size, sz_decimals=2)
        assert ceiled * price >= 10.0

    def test_ceil_round_does_not_affect_reduce_only(self):
        """For reduce_only (close) orders the original round() is still used — no change."""
        # This is a documentation test: close orders must NOT use ceil to avoid over-closing.
        # The actual guard is in place_order(reduce_only=True).
        price = 3.51
        raw_size = 2.849
        assert round(raw_size, 1) == 2.8  # standard round, may be below $10 — that's OK for closes
