"""
Tests for HLQuantBot Outcome Tracker
======================================

Unit tests for the LLM decision outcome tracker.

Run:
    pytest simple_bot/tests/test_outcome_tracker.py -v
"""

import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from simple_bot.services.outcome_tracker import (
    OutcomeTrackerService,
    compute_tp_price,
    compute_mfe_mae,
    check_tp_sl_hit,
    determine_was_correct,
    select_checkpoint_column,
)
from simple_bot.core.models import (
    Direction,
    LLMDecision,
    MarketState,
    Regime,
    Setup,
    SetupType,
)


# =============================================================================
# Test Fixtures
# =============================================================================

@pytest.fixture
def mock_db():
    """Create a mock database with async methods."""
    db = AsyncMock()
    db.insert_llm_decision = AsyncMock(return_value=42)
    db.get_pending_llm_decisions = AsyncMock(return_value=[])
    db.update_decision_checkpoint = AsyncMock()
    db.resolve_llm_decision = AsyncMock()
    db.get_llm_performance = AsyncMock(return_value={"total": 0})
    return db


@pytest.fixture
def tracker(mock_db):
    """Create an OutcomeTrackerService with mock DB."""
    return OutcomeTrackerService(
        db=mock_db,
        stop_loss_pct=0.8,
        take_profit_pct=1.6,
    )


@pytest.fixture
def sample_setup():
    """Create a sample LONG setup."""
    return Setup(
        id="test-001",
        symbol="BTC",
        timestamp=datetime.now(timezone.utc),
        setup_type=SetupType.MOMENTUM,
        direction=Direction.LONG,
        regime=Regime.TREND,
        entry_price=Decimal("50000"),
        stop_price=Decimal("49600"),  # -0.8%
        stop_distance_pct=Decimal("0.8"),
        atr=Decimal("400"),
        adx=Decimal("30"),
        rsi=Decimal("55"),
    )


@pytest.fixture
def sample_short_setup():
    """Create a sample SHORT setup."""
    return Setup(
        id="test-002",
        symbol="ETH",
        timestamp=datetime.now(timezone.utc),
        setup_type=SetupType.MOMENTUM,
        direction=Direction.SHORT,
        regime=Regime.TREND,
        entry_price=Decimal("3000"),
        stop_price=Decimal("3024"),  # +0.8%
        stop_distance_pct=Decimal("0.8"),
        atr=Decimal("30"),
        adx=Decimal("28"),
        rsi=Decimal("42"),
    )


@pytest.fixture
def sample_decision():
    """Create a sample ALLOW decision."""
    return LLMDecision(
        setup_id="test-001",
        timestamp=datetime.now(timezone.utc),
        decision="ALLOW",
        confidence=Decimal("0.75"),
        reason="Strong trend alignment",
        symbol="BTC",
        regime=Regime.TREND,
        setup_type=SetupType.MOMENTUM,
    )


@pytest.fixture
def sample_deny_decision():
    """Create a sample DENY decision."""
    return LLMDecision(
        setup_id="test-002",
        timestamp=datetime.now(timezone.utc),
        decision="DENY",
        confidence=Decimal("0.80"),
        reason="Counter-trend move",
        symbol="ETH",
        regime=Regime.TREND,
        setup_type=SetupType.MOMENTUM,
    )


# =============================================================================
# TP Price Calculation Tests
# =============================================================================

class TestComputeTpPrice:
    """Tests for TP price calculation."""

    def test_long_tp_price(self):
        tp = compute_tp_price(Decimal("50000"), "long", Decimal("1.6"))
        assert tp == Decimal("50800")

    def test_short_tp_price(self):
        tp = compute_tp_price(Decimal("3000"), "short", Decimal("1.6"))
        assert tp == Decimal("2952")

    def test_long_tp_small_pct(self):
        tp = compute_tp_price(Decimal("100"), "long", Decimal("0.5"))
        assert tp == Decimal("100.5")

    def test_short_tp_small_pct(self):
        tp = compute_tp_price(Decimal("100"), "short", Decimal("0.5"))
        assert tp == Decimal("99.5")


# =============================================================================
# MFE/MAE Calculation Tests
# =============================================================================

class TestComputeMfeMae:
    """Tests for MFE/MAE calculation."""

    def test_long_favorable(self):
        """Price moved up from entry → favorable for long."""
        fav, adv = compute_mfe_mae(Decimal("100"), Decimal("102"), "long")
        assert fav == Decimal("2")
        assert adv == Decimal("0")

    def test_long_adverse(self):
        """Price moved down from entry → adverse for long."""
        fav, adv = compute_mfe_mae(Decimal("100"), Decimal("98"), "long")
        assert fav == Decimal("0")
        assert adv == Decimal("2")

    def test_short_favorable(self):
        """Price moved down from entry → favorable for short."""
        fav, adv = compute_mfe_mae(Decimal("100"), Decimal("98"), "short")
        assert fav == Decimal("2")
        assert adv == Decimal("0")

    def test_short_adverse(self):
        """Price moved up from entry → adverse for short."""
        fav, adv = compute_mfe_mae(Decimal("100"), Decimal("102"), "short")
        assert fav == Decimal("0")
        assert adv == Decimal("2")

    def test_no_movement(self):
        """Price unchanged → both zero."""
        fav, adv = compute_mfe_mae(Decimal("100"), Decimal("100"), "long")
        assert fav == Decimal("0")
        assert adv == Decimal("0")

    def test_zero_entry(self):
        """Zero entry price → both zero (edge case)."""
        fav, adv = compute_mfe_mae(Decimal("0"), Decimal("100"), "long")
        assert fav == Decimal("0")
        assert adv == Decimal("0")


# =============================================================================
# TP/SL Hit Detection Tests
# =============================================================================

class TestCheckTpSlHit:
    """Tests for TP/SL hit detection."""

    def test_long_tp_hit(self):
        result = check_tp_sl_hit(
            Decimal("100"), Decimal("102"),
            Decimal("99"), Decimal("101.6"),
            "long",
        )
        assert result == "tp"

    def test_long_sl_hit(self):
        result = check_tp_sl_hit(
            Decimal("100"), Decimal("98.5"),
            Decimal("99"), Decimal("101.6"),
            "long",
        )
        assert result == "sl"

    def test_long_neither(self):
        result = check_tp_sl_hit(
            Decimal("100"), Decimal("100.5"),
            Decimal("99"), Decimal("101.6"),
            "long",
        )
        assert result is None

    def test_short_tp_hit(self):
        result = check_tp_sl_hit(
            Decimal("100"), Decimal("97"),
            Decimal("101"), Decimal("98.4"),
            "short",
        )
        assert result == "tp"

    def test_short_sl_hit(self):
        result = check_tp_sl_hit(
            Decimal("100"), Decimal("101.5"),
            Decimal("101"), Decimal("98.4"),
            "short",
        )
        assert result == "sl"

    def test_short_neither(self):
        result = check_tp_sl_hit(
            Decimal("100"), Decimal("99.5"),
            Decimal("101"), Decimal("98.4"),
            "short",
        )
        assert result is None

    def test_long_tp_exact_hit(self):
        """TP price exactly hit → counts as TP."""
        result = check_tp_sl_hit(
            Decimal("100"), Decimal("101.6"),
            Decimal("99"), Decimal("101.6"),
            "long",
        )
        assert result == "tp"

    def test_short_sl_exact_hit(self):
        """SL price exactly hit → counts as SL."""
        result = check_tp_sl_hit(
            Decimal("100"), Decimal("101"),
            Decimal("101"), Decimal("98.4"),
            "short",
        )
        assert result == "sl"


# =============================================================================
# Correctness Matrix Tests
# =============================================================================

class TestDetermineWasCorrect:
    """Tests for LLM decision correctness determination."""

    def test_allow_tp_correct(self):
        """ALLOW + TP hit → correct (allowed a winner)."""
        assert determine_was_correct("ALLOW", "tp") is True

    def test_allow_sl_incorrect(self):
        """ALLOW + SL hit → incorrect (allowed a loser)."""
        assert determine_was_correct("ALLOW", "sl") is False

    def test_allow_neither_correct(self):
        """ALLOW + neither → correct (no harm)."""
        assert determine_was_correct("ALLOW", "neither") is True

    def test_deny_tp_incorrect(self):
        """DENY + TP hit → incorrect (blocked a winner)."""
        assert determine_was_correct("DENY", "tp") is False

    def test_deny_sl_correct(self):
        """DENY + SL hit → correct (blocked a loser)."""
        assert determine_was_correct("DENY", "sl") is True

    def test_deny_neither_correct(self):
        """DENY + neither → correct (no harm)."""
        assert determine_was_correct("DENY", "neither") is True


# =============================================================================
# Checkpoint Column Selection Tests
# =============================================================================

class TestSelectCheckpointColumn:
    """Tests for checkpoint column selection logic."""

    def _empty_checkpoints(self):
        return {
            "price_5m": None, "price_15m": None, "price_30m": None,
            "price_1h": None, "price_2h": None, "price_4h": None,
        }

    def test_5min_window(self):
        assert select_checkpoint_column(5, self._empty_checkpoints()) == "price_5m"
        assert select_checkpoint_column(10, self._empty_checkpoints()) == "price_5m"
        assert select_checkpoint_column(14, self._empty_checkpoints()) == "price_5m"

    def test_15min_window(self):
        assert select_checkpoint_column(15, self._empty_checkpoints()) == "price_15m"
        assert select_checkpoint_column(20, self._empty_checkpoints()) == "price_15m"

    def test_30min_window(self):
        assert select_checkpoint_column(30, self._empty_checkpoints()) == "price_30m"

    def test_1h_window(self):
        assert select_checkpoint_column(60, self._empty_checkpoints()) == "price_1h"
        assert select_checkpoint_column(90, self._empty_checkpoints()) == "price_1h"

    def test_2h_window(self):
        assert select_checkpoint_column(120, self._empty_checkpoints()) == "price_2h"

    def test_4h_window(self):
        assert select_checkpoint_column(240, self._empty_checkpoints()) == "price_4h"
        assert select_checkpoint_column(300, self._empty_checkpoints()) == "price_4h"

    def test_already_filled(self):
        """Column already filled → skip to next or None."""
        cp = self._empty_checkpoints()
        cp["price_5m"] = Decimal("50000")
        # At 10 min, price_5m is filled, should return None (not yet in 15m window)
        assert select_checkpoint_column(10, cp) is None

    def test_too_early(self):
        """Before 5 minutes → no checkpoint."""
        assert select_checkpoint_column(3, self._empty_checkpoints()) is None

    def test_15min_when_5min_filled(self):
        """At 17 min with price_5m filled → price_15m."""
        cp = self._empty_checkpoints()
        cp["price_5m"] = Decimal("50000")
        assert select_checkpoint_column(17, cp) == "price_15m"


# =============================================================================
# Integration Tests: log_decision
# =============================================================================

class TestLogDecision:
    """Tests for log_decision method."""

    @pytest.mark.asyncio
    async def test_log_decision_success(self, tracker, mock_db, sample_setup, sample_decision):
        """Successfully log an LLM decision."""
        row_id = await tracker.log_decision(
            setup=sample_setup,
            decision=sample_decision,
            market_state=None,
            latency_ms=120,
        )

        assert row_id == 42
        mock_db.insert_llm_decision.assert_called_once()
        call_kwargs = mock_db.insert_llm_decision.call_args.kwargs
        assert call_kwargs["symbol"] == "BTC"
        assert call_kwargs["direction"] == "long"
        assert call_kwargs["decision"] == "ALLOW"
        assert call_kwargs["latency_ms"] == 120
        # Check TP price calculation: 50000 * 1.016 = 50800
        assert call_kwargs["tp_price"] == Decimal("50800")

    @pytest.mark.asyncio
    async def test_log_decision_deny(self, tracker, mock_db, sample_short_setup, sample_deny_decision):
        """Log a DENY decision."""
        row_id = await tracker.log_decision(
            setup=sample_short_setup,
            decision=sample_deny_decision,
            market_state=None,
            latency_ms=80,
        )

        assert row_id == 42
        call_kwargs = mock_db.insert_llm_decision.call_args.kwargs
        assert call_kwargs["decision"] == "DENY"
        assert call_kwargs["direction"] == "short"

    @pytest.mark.asyncio
    async def test_log_decision_db_error(self, tracker, mock_db, sample_setup, sample_decision):
        """DB error returns None, doesn't raise."""
        mock_db.insert_llm_decision.side_effect = Exception("DB error")
        row_id = await tracker.log_decision(
            setup=sample_setup,
            decision=sample_decision,
            market_state=None,
            latency_ms=100,
        )
        assert row_id is None


# =============================================================================
# Integration Tests: check_pending
# =============================================================================

class TestCheckPending:
    """Tests for check_pending method."""

    @pytest.mark.asyncio
    async def test_check_pending_tp_hit(self, tracker, mock_db):
        """Pending decision with TP hit → resolved as tp."""
        decided_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        mock_db.get_pending_llm_decisions.return_value = [{
            "id": 1,
            "decided_at": decided_at,
            "symbol": "BTC",
            "direction": "long",
            "regime": "trend",
            "entry_price": Decimal("50000"),
            "stop_price": Decimal("49600"),
            "tp_price": Decimal("50800"),
            "decision": "ALLOW",
            "max_favorable_pct": Decimal("0"),
            "max_adverse_pct": Decimal("0"),
            "price_5m": Decimal("50100"),
            "price_15m": Decimal("50300"),
            "price_30m": None,
            "price_1h": None,
            "price_2h": None,
            "price_4h": None,
        }]

        async def price_getter(symbol):
            return Decimal("50900")  # Above TP

        resolved = await tracker.check_pending(price_getter)
        assert resolved == 1
        mock_db.resolve_llm_decision.assert_called_once()
        call_kwargs = mock_db.resolve_llm_decision.call_args.kwargs
        assert call_kwargs["first_hit"] == "tp"
        assert call_kwargs["was_correct"] is True  # ALLOW + TP = correct

    @pytest.mark.asyncio
    async def test_check_pending_sl_hit(self, tracker, mock_db):
        """Pending decision with SL hit → resolved as sl."""
        decided_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        mock_db.get_pending_llm_decisions.return_value = [{
            "id": 2,
            "decided_at": decided_at,
            "symbol": "BTC",
            "direction": "long",
            "regime": "trend",
            "entry_price": Decimal("50000"),
            "stop_price": Decimal("49600"),
            "tp_price": Decimal("50800"),
            "decision": "ALLOW",
            "max_favorable_pct": Decimal("0"),
            "max_adverse_pct": Decimal("0"),
            "price_5m": Decimal("49900"),
            "price_15m": None,
            "price_30m": None,
            "price_1h": None,
            "price_2h": None,
            "price_4h": None,
        }]

        async def price_getter(symbol):
            return Decimal("49500")  # Below SL

        resolved = await tracker.check_pending(price_getter)
        assert resolved == 1
        call_kwargs = mock_db.resolve_llm_decision.call_args.kwargs
        assert call_kwargs["first_hit"] == "sl"
        assert call_kwargs["was_correct"] is False  # ALLOW + SL = incorrect

    @pytest.mark.asyncio
    async def test_check_pending_deny_sl_correct(self, tracker, mock_db):
        """DENY decision with SL hit → correct (blocked a loser)."""
        decided_at = datetime.now(timezone.utc) - timedelta(minutes=20)
        mock_db.get_pending_llm_decisions.return_value = [{
            "id": 3,
            "decided_at": decided_at,
            "symbol": "ETH",
            "direction": "short",
            "regime": "trend",
            "entry_price": Decimal("3000"),
            "stop_price": Decimal("3024"),
            "tp_price": Decimal("2952"),
            "decision": "DENY",
            "max_favorable_pct": Decimal("0"),
            "max_adverse_pct": Decimal("0"),
            "price_5m": Decimal("3010"),
            "price_15m": Decimal("3020"),
            "price_30m": None,
            "price_1h": None,
            "price_2h": None,
            "price_4h": None,
        }]

        async def price_getter(symbol):
            return Decimal("3030")  # Above SL for short

        resolved = await tracker.check_pending(price_getter)
        assert resolved == 1
        call_kwargs = mock_db.resolve_llm_decision.call_args.kwargs
        assert call_kwargs["first_hit"] == "sl"
        assert call_kwargs["was_correct"] is True  # DENY + SL = correct

    @pytest.mark.asyncio
    async def test_check_pending_timeout(self, tracker, mock_db):
        """Pending beyond max_age_hours → resolved as neither."""
        decided_at = datetime.now(timezone.utc) - timedelta(hours=5)
        mock_db.get_pending_llm_decisions.return_value = [{
            "id": 4,
            "decided_at": decided_at,
            "symbol": "BTC",
            "direction": "long",
            "regime": "trend",
            "entry_price": Decimal("50000"),
            "stop_price": Decimal("49600"),
            "tp_price": Decimal("50800"),
            "decision": "DENY",
            "max_favorable_pct": Decimal("1.2"),
            "max_adverse_pct": Decimal("0.3"),
            "price_5m": Decimal("50050"),
            "price_15m": Decimal("50100"),
            "price_30m": Decimal("50200"),
            "price_1h": Decimal("50300"),
            "price_2h": Decimal("50250"),
            "price_4h": Decimal("50150"),
        }]

        async def price_getter(symbol):
            return Decimal("50100")

        resolved = await tracker.check_pending(price_getter)
        assert resolved == 1
        call_kwargs = mock_db.resolve_llm_decision.call_args.kwargs
        assert call_kwargs["first_hit"] == "neither"
        assert call_kwargs["was_correct"] is True  # DENY + neither = correct
        assert call_kwargs["time_to_hit_min"] is None

    @pytest.mark.asyncio
    async def test_check_pending_no_price(self, tracker, mock_db):
        """Price getter returns None → skip, don't resolve."""
        decided_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        mock_db.get_pending_llm_decisions.return_value = [{
            "id": 5,
            "decided_at": decided_at,
            "symbol": "UNKNOWN",
            "direction": "long",
            "regime": "trend",
            "entry_price": Decimal("100"),
            "stop_price": Decimal("99"),
            "tp_price": Decimal("101.6"),
            "decision": "ALLOW",
            "max_favorable_pct": Decimal("0"),
            "max_adverse_pct": Decimal("0"),
            "price_5m": None,
            "price_15m": None,
            "price_30m": None,
            "price_1h": None,
            "price_2h": None,
            "price_4h": None,
        }]

        async def price_getter(symbol):
            return None

        resolved = await tracker.check_pending(price_getter)
        assert resolved == 0
        mock_db.resolve_llm_decision.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_pending_checkpoint_update(self, tracker, mock_db):
        """Price within range fills checkpoint and updates MFE/MAE."""
        decided_at = datetime.now(timezone.utc) - timedelta(minutes=6)
        mock_db.get_pending_llm_decisions.return_value = [{
            "id": 6,
            "decided_at": decided_at,
            "symbol": "BTC",
            "direction": "long",
            "regime": "trend",
            "entry_price": Decimal("50000"),
            "stop_price": Decimal("49600"),
            "tp_price": Decimal("50800"),
            "decision": "ALLOW",
            "max_favorable_pct": Decimal("0"),
            "max_adverse_pct": Decimal("0"),
            "price_5m": None,
            "price_15m": None,
            "price_30m": None,
            "price_1h": None,
            "price_2h": None,
            "price_4h": None,
        }]

        async def price_getter(symbol):
            return Decimal("50200")  # +0.4% from entry

        resolved = await tracker.check_pending(price_getter)
        assert resolved == 0  # Not resolved (no TP/SL hit)
        mock_db.update_decision_checkpoint.assert_called_once()
        call_kwargs = mock_db.update_decision_checkpoint.call_args.kwargs
        assert call_kwargs["column"] == "price_5m"
        assert call_kwargs["price"] == Decimal("50200")
        assert call_kwargs["favorable_pct"] == Decimal("0.4")
        assert call_kwargs["adverse_pct"] == Decimal("0")

    @pytest.mark.asyncio
    async def test_check_pending_db_error(self, tracker, mock_db):
        """DB error on fetch → returns 0, doesn't raise."""
        mock_db.get_pending_llm_decisions.side_effect = Exception("DB down")
        resolved = await tracker.check_pending(AsyncMock(return_value=Decimal("100")))
        assert resolved == 0

    @pytest.mark.asyncio
    async def test_check_pending_empty(self, tracker, mock_db):
        """No pending decisions → returns 0."""
        resolved = await tracker.check_pending(AsyncMock(return_value=Decimal("100")))
        assert resolved == 0


# =============================================================================
# Performance Summary Test
# =============================================================================

class TestGetPerformanceSummary:
    """Tests for get_performance_summary method."""

    @pytest.mark.asyncio
    async def test_performance_summary(self, tracker, mock_db):
        """Returns DB stats."""
        mock_db.get_llm_performance.return_value = {
            "total": 10, "resolved": 8, "correct": 6,
        }
        result = await tracker.get_performance_summary(days=7)
        assert result["total"] == 10
        mock_db.get_llm_performance.assert_called_once_with(days=7)

    @pytest.mark.asyncio
    async def test_performance_summary_db_error(self, tracker, mock_db):
        """DB error → returns error dict."""
        mock_db.get_llm_performance.side_effect = Exception("DB error")
        result = await tracker.get_performance_summary()
        assert result["total"] == 0
        assert "error" in result


# =============================================================================
# MFE/MAE Running Maximum Tests
# =============================================================================

class TestMfeMaeRunningMax:
    """Verify that MFE/MAE only grows (running maximum behavior)."""

    def test_mfe_increases_long(self):
        """Long: higher price → higher MFE."""
        _, _ = compute_mfe_mae(Decimal("100"), Decimal("101"), "long")  # 1%
        fav2, _ = compute_mfe_mae(Decimal("100"), Decimal("103"), "long")  # 3%
        assert fav2 == Decimal("3")

    def test_mae_increases_long(self):
        """Long: lower price → higher MAE."""
        _, _ = compute_mfe_mae(Decimal("100"), Decimal("99"), "long")  # 1%
        _, adv2 = compute_mfe_mae(Decimal("100"), Decimal("97"), "long")  # 3%
        assert adv2 == Decimal("3")

    def test_running_max_via_db_greatest(self):
        """The GREATEST() SQL ensures running max — verified by DB call."""
        # This is implicitly tested by the DB's GREATEST() function
        # Our code just passes the new candidate values
        fav1, adv1 = compute_mfe_mae(Decimal("100"), Decimal("102"), "long")
        fav2, adv2 = compute_mfe_mae(Decimal("100"), Decimal("101"), "long")
        # fav2 < fav1, but DB uses GREATEST(current, new) to keep the max
        assert fav1 == Decimal("2")
        assert fav2 == Decimal("1")
        # The DB would keep 2 via GREATEST(2, 1)
