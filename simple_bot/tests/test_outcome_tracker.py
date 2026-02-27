"""
Tests for HLQuantBot Outcome Tracker
======================================

Unit tests for the LLM decision outcome tracker utility functions.

Run:
    pytest simple_bot/tests/test_outcome_tracker.py -v
"""

import pytest
from decimal import Decimal

from simple_bot.services.outcome_tracker import (
    compute_tp_price,
    compute_mfe_mae,
    check_tp_sl_hit,
    determine_was_correct,
    select_checkpoint_column,
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
# MFE/MAE Running Maximum Tests
# =============================================================================

class TestMfeMaeRunningMax:
    """Verify that MFE/MAE computation behavior."""

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

    def test_running_max_candidates(self):
        """MFE/MAE functions return candidate values correctly."""
        fav1, adv1 = compute_mfe_mae(Decimal("100"), Decimal("102"), "long")
        fav2, adv2 = compute_mfe_mae(Decimal("100"), Decimal("101"), "long")
        assert fav1 == Decimal("2")
        assert fav2 == Decimal("1")
