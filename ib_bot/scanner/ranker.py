"""Ranker for scanner results.

Sorts scan results by composite score and filters candidates.
"""

from __future__ import annotations

from ib_bot.scanner.signals import ScanResult


def rank_candidates(
    results: list[ScanResult],
    max_candidates: int = 20,
) -> list[ScanResult]:
    """Rank scan results by score (descending) and return top N.

    Only results with score > 0 are included.

    Args:
        results: List of ScanResult from the scanner.
        max_candidates: Maximum number of candidates to return.

    Returns:
        Sorted list of top candidates, limited to max_candidates.
    """
    filtered = [r for r in results if r.score > 0]
    ranked = sorted(filtered, key=lambda r: r.score, reverse=True)
    return ranked[:max_candidates]
