"""Tests for ib_bot.scanner.correlation_filter."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from ib_bot.scanner.correlation_filter import (
    filter_correlated,
    get_sector,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class FakeDecision:
    """Minimal object with symbol + confidence."""
    symbol: str
    confidence: float


# ---------------------------------------------------------------------------
# get_sector
# ---------------------------------------------------------------------------

class TestGetSector:
    def test_known_stock(self) -> None:
        assert get_sector("AAPL") == "Information Technology"
        assert get_sector("JPM") == "Financials"
        assert get_sector("UNH") == "Health Care"

    def test_unknown_stock(self) -> None:
        assert get_sector("ZZZZ") == "Unknown"

    def test_empty_string(self) -> None:
        assert get_sector("") == "Unknown"


# ---------------------------------------------------------------------------
# filter_correlated — basic behavior
# ---------------------------------------------------------------------------

class TestFilterCorrelatedBasic:
    def test_empty_candidates(self) -> None:
        result = filter_correlated([])
        assert result == []

    def test_single_candidate_passes(self) -> None:
        candidates = [FakeDecision("AAPL", 0.9)]
        result = filter_correlated(candidates)
        assert len(result) == 1
        assert result[0].symbol == "AAPL"

    def test_preserves_highest_confidence_first(self) -> None:
        candidates = [
            FakeDecision("AAPL", 0.5),
            FakeDecision("MSFT", 0.9),
            FakeDecision("NVDA", 0.7),
        ]
        result = filter_correlated(candidates, max_total=10, max_per_sector=10)
        # Should be sorted by confidence desc
        assert result[0].symbol == "MSFT"
        assert result[1].symbol == "NVDA"
        assert result[2].symbol == "AAPL"


# ---------------------------------------------------------------------------
# filter_correlated — sector limits
# ---------------------------------------------------------------------------

class TestFilterCorrelatedSectorLimits:
    def test_max_per_sector_enforced(self) -> None:
        # All three are "Information Technology"
        candidates = [
            FakeDecision("AAPL", 0.9),
            FakeDecision("MSFT", 0.8),
            FakeDecision("NVDA", 0.7),
        ]
        result = filter_correlated(candidates, max_per_sector=2, max_total=10)
        assert len(result) == 2
        symbols = {d.symbol for d in result}
        assert "AAPL" in symbols
        assert "MSFT" in symbols
        assert "NVDA" not in symbols  # 3rd in same sector

    def test_different_sectors_all_pass(self) -> None:
        candidates = [
            FakeDecision("AAPL", 0.9),   # IT
            FakeDecision("JPM", 0.8),    # Financials
            FakeDecision("UNH", 0.7),    # Health Care
            FakeDecision("XOM", 0.6),    # Energy
        ]
        result = filter_correlated(candidates, max_per_sector=2, max_total=10)
        assert len(result) == 4

    def test_open_positions_count_toward_sector_limit(self) -> None:
        # AAPL is already open (IT), so only 1 more IT slot
        candidates = [
            FakeDecision("MSFT", 0.9),  # IT
            FakeDecision("NVDA", 0.8),  # IT
        ]
        open_positions = [{"symbol": "AAPL"}]  # IT
        result = filter_correlated(
            candidates,
            open_positions=open_positions,
            max_per_sector=2,
            max_total=10,
        )
        assert len(result) == 1
        assert result[0].symbol == "MSFT"  # Higher confidence


# ---------------------------------------------------------------------------
# filter_correlated — total limits
# ---------------------------------------------------------------------------

class TestFilterCorrelatedTotalLimits:
    def test_max_total_enforced(self) -> None:
        candidates = [
            FakeDecision("AAPL", 0.9),
            FakeDecision("JPM", 0.8),
            FakeDecision("UNH", 0.7),
            FakeDecision("XOM", 0.6),
        ]
        result = filter_correlated(candidates, max_per_sector=5, max_total=2)
        assert len(result) == 2

    def test_open_positions_count_toward_total(self) -> None:
        candidates = [
            FakeDecision("AAPL", 0.9),
            FakeDecision("JPM", 0.8),
        ]
        # 4 open positions, max_total=5 => room for 1 new
        open_positions = [
            {"symbol": "MSFT"},
            {"symbol": "GOOGL"},
            {"symbol": "AMZN"},
            {"symbol": "META"},
        ]
        result = filter_correlated(
            candidates,
            open_positions=open_positions,
            max_per_sector=5,
            max_total=5,
        )
        assert len(result) == 1
        assert result[0].symbol == "AAPL"

    def test_max_total_reached_by_open_positions(self) -> None:
        candidates = [FakeDecision("AAPL", 0.9)]
        open_positions = [
            {"symbol": f"POS{i}"} for i in range(5)
        ]
        result = filter_correlated(
            candidates,
            open_positions=open_positions,
            max_total=5,
        )
        assert len(result) == 0


# ---------------------------------------------------------------------------
# filter_correlated — dict candidates
# ---------------------------------------------------------------------------

class TestFilterCorrelatedDictInput:
    def test_dict_candidates(self) -> None:
        candidates = [
            {"symbol": "AAPL", "confidence": 0.9},
            {"symbol": "JPM", "confidence": 0.8},
        ]
        result = filter_correlated(candidates, max_total=10)
        assert len(result) == 2

    def test_dict_open_positions(self) -> None:
        candidates = [
            {"symbol": "MSFT", "confidence": 0.9},
        ]
        open_positions = [{"symbol": "AAPL"}]
        result = filter_correlated(
            candidates,
            open_positions=open_positions,
            max_per_sector=1,  # Only 1 IT allowed
        )
        # AAPL is already in IT, so MSFT (IT) is blocked
        assert len(result) == 0


# ---------------------------------------------------------------------------
# filter_correlated — unknown sectors
# ---------------------------------------------------------------------------

class TestFilterCorrelatedUnknownSectors:
    def test_unknown_sector_treated_as_one_group(self) -> None:
        # Symbols not in STOCK_SECTORS -> "Unknown" sector
        candidates = [
            FakeDecision("ZZZA", 0.9),
            FakeDecision("ZZZB", 0.8),
            FakeDecision("ZZZC", 0.7),
        ]
        result = filter_correlated(candidates, max_per_sector=2, max_total=10)
        assert len(result) == 2  # Max 2 per "Unknown" sector
