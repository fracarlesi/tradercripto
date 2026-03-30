"""Tests for ib_bot.scanner.ranker."""

from ib_bot.scanner.ranker import rank_candidates
from ib_bot.scanner.signals import ScanResult


def _make_result(symbol: str, score: float) -> ScanResult:
    return ScanResult(symbol=symbol, score=score)


class TestRankCandidates:
    """Test ranking logic."""

    def test_sorts_by_score_descending(self) -> None:
        results = [
            _make_result("A", 2.0),
            _make_result("B", 5.0),
            _make_result("C", 3.0),
        ]
        ranked = rank_candidates(results)
        assert [r.symbol for r in ranked] == ["B", "C", "A"]

    def test_filters_zero_scores(self) -> None:
        results = [
            _make_result("A", 0.0),
            _make_result("B", 3.0),
            _make_result("C", 0.0),
        ]
        ranked = rank_candidates(results)
        assert len(ranked) == 1
        assert ranked[0].symbol == "B"

    def test_respects_max_candidates(self) -> None:
        results = [_make_result(f"S{i}", float(i)) for i in range(1, 30)]
        ranked = rank_candidates(results, max_candidates=5)
        assert len(ranked) == 5
        assert ranked[0].score == 29.0

    def test_empty_input(self) -> None:
        assert rank_candidates([]) == []

    def test_all_zero_scores(self) -> None:
        results = [_make_result("A", 0.0), _make_result("B", 0.0)]
        assert rank_candidates(results) == []

    def test_default_max_is_20(self) -> None:
        results = [_make_result(f"S{i}", float(i)) for i in range(1, 50)]
        ranked = rank_candidates(results)
        assert len(ranked) == 20

    def test_ties_preserved(self) -> None:
        results = [
            _make_result("A", 3.0),
            _make_result("B", 3.0),
            _make_result("C", 3.0),
        ]
        ranked = rank_candidates(results)
        assert len(ranked) == 3
        # All have same score
        assert all(r.score == 3.0 for r in ranked)
