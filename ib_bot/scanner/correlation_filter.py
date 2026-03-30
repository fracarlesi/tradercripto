"""
Correlation Filter — Sector-Based Position Diversification
============================================================

Filters trade candidates to enforce portfolio diversification:
- Max N positions per GICS sector
- Max M total positions
- Priority by confidence (highest first)

Uses STOCK_SECTORS from universe.py for sector mapping.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ib_bot.scanner.universe import STOCK_SECTORS

logger = logging.getLogger(__name__)


@runtime_checkable
class HasSymbolAndConfidence(Protocol):
    """Protocol for objects with symbol and confidence attributes."""

    symbol: str
    confidence: float


def get_sector(symbol: str) -> str:
    """Return the GICS sector for a symbol, or 'Unknown' if not mapped.

    Args:
        symbol: Ticker symbol.

    Returns:
        GICS sector name (e.g., "Information Technology") or "Unknown".
    """
    return STOCK_SECTORS.get(symbol, "Unknown")


def filter_correlated(
    candidates: list[Any],
    open_positions: list[Any] | None = None,
    max_per_sector: int = 2,
    max_total: int = 5,
) -> list[Any]:
    """Filter candidates to enforce sector and total position limits.

    Candidates must have 'symbol' and 'confidence' attributes (or dict keys).
    They are assumed to be pre-sorted by confidence descending (highest first).
    If not sorted, this function sorts them.

    Open positions are counted toward sector limits but not toward the
    candidate output list.

    Args:
        candidates: List of trade decisions/setups with symbol + confidence.
        open_positions: List of currently open positions (dicts or objects
            with 'symbol' attribute/key). Used to count existing sector exposure.
        max_per_sector: Maximum positions allowed per GICS sector.
        max_total: Maximum total positions (including open + new).

    Returns:
        Filtered list of candidates, maintaining input order (by confidence).
    """
    if not candidates:
        return []

    if open_positions is None:
        open_positions = []

    # Count existing sector exposure from open positions
    sector_counts: Counter[str] = Counter()
    for pos in open_positions:
        sym = _get_symbol(pos)
        if sym:
            sector_counts[get_sector(sym)] += 1

    total_open = len(open_positions)

    # Sort candidates by absolute confidence descending (if not already)
    sorted_candidates = sorted(
        candidates,
        key=lambda c: abs(_get_confidence(c)),
        reverse=True,
    )

    filtered: list[Any] = []

    for candidate in sorted_candidates:
        # Check total limit
        if total_open + len(filtered) >= max_total:
            logger.debug(
                "Max total positions reached (%d), stopping filter", max_total
            )
            break

        sym = _get_symbol(candidate)
        if not sym:
            continue

        sector = get_sector(sym)
        current_in_sector = sector_counts[sector] + sum(
            1 for f in filtered if get_sector(_get_symbol(f)) == sector
        )

        if current_in_sector >= max_per_sector:
            logger.debug(
                "Sector limit reached for %s (%s): %d/%d",
                sym, sector, current_in_sector, max_per_sector,
            )
            continue

        filtered.append(candidate)

    logger.info(
        "Correlation filter: %d candidates -> %d after filtering "
        "(max_per_sector=%d, max_total=%d, open=%d)",
        len(candidates), len(filtered), max_per_sector, max_total, total_open,
    )
    return filtered


# ---------------------------------------------------------------------------
# Helpers for duck-typed access
# ---------------------------------------------------------------------------

def _get_symbol(obj: Any) -> str:
    """Extract symbol from an object or dict."""
    if hasattr(obj, "symbol"):
        return obj.symbol
    if isinstance(obj, dict):
        return obj.get("symbol", "")
    return ""


def _get_confidence(obj: Any) -> float:
    """Extract confidence from an object or dict."""
    if hasattr(obj, "confidence"):
        return obj.confidence
    if isinstance(obj, dict):
        return obj.get("confidence", 0.0)
    return 0.0
