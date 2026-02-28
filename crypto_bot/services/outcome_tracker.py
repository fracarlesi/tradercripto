"""
HLQuantBot Outcome Tracker Service
====================================

Tracks LLM decision outcomes with price checkpoints and MFE/MAE.

NOTE: Database has been removed. This module retains pure-logic helper
functions (compute_tp_price, compute_mfe_mae, check_tp_sl_hit,
determine_was_correct, select_checkpoint_column) that are used by tests
and potentially other code. The OutcomeTrackerService class is now a
no-op stub.

Usage:
    tracker = OutcomeTrackerService(stop_loss_pct=0.8, take_profit_pct=1.6)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Coroutine, Dict, Optional

from ..core.models import LLMDecision, MarketState, Setup

logger = logging.getLogger(__name__)

# Checkpoint thresholds: (min_minutes, max_minutes, column_name)
CHECKPOINT_THRESHOLDS = [
    (5, 15, "price_5m"),
    (15, 30, "price_15m"),
    (30, 60, "price_30m"),
    (60, 120, "price_1h"),
    (120, 240, "price_2h"),
    (240, None, "price_4h"),
]

# Type alias for price getter callback
PriceGetter = Callable[[str], Coroutine[Any, Any, Optional[Decimal]]]


def compute_tp_price(
    entry_price: Decimal,
    direction: str,
    take_profit_pct: Decimal,
) -> Decimal:
    """Calculate take-profit price from entry, direction, and TP %."""
    if direction == "long":
        return entry_price * (1 + take_profit_pct / 100)
    else:
        return entry_price * (1 - take_profit_pct / 100)


def compute_mfe_mae(
    entry_price: Decimal,
    current_price: Decimal,
    direction: str,
) -> tuple[Decimal, Decimal]:
    """Compute favorable/adverse excursion % for current price.

    Returns:
        (favorable_pct, adverse_pct) — both as positive values.
    """
    if entry_price == 0:
        return Decimal("0"), Decimal("0")

    pct_change = (current_price - entry_price) / entry_price * 100

    if direction == "long":
        favorable = max(pct_change, Decimal("0"))
        adverse = max(-pct_change, Decimal("0"))
    else:
        favorable = max(-pct_change, Decimal("0"))
        adverse = max(pct_change, Decimal("0"))

    return favorable, adverse


def check_tp_sl_hit(
    entry_price: Decimal,
    current_price: Decimal,
    stop_price: Decimal,
    tp_price: Decimal,
    direction: str,
) -> Optional[str]:
    """Check if TP or SL was hit.

    Returns:
        "tp", "sl", or None.
    """
    if direction == "long":
        if current_price >= tp_price:
            return "tp"
        if current_price <= stop_price:
            return "sl"
    else:
        if current_price <= tp_price:
            return "tp"
        if current_price >= stop_price:
            return "sl"
    return None


def determine_was_correct(decision: str, first_hit: str) -> bool:
    """Determine if the LLM decision was correct.

    Truth table:
        ALLOW + TP hit    -> correct (allowed a winner)
        ALLOW + SL hit    -> incorrect (allowed a loser)
        ALLOW + neither   -> correct (no harm)
        DENY  + TP hit    -> incorrect (blocked a winner)
        DENY  + SL hit    -> correct (blocked a loser)
        DENY  + neither   -> correct (no harm)
    """
    if first_hit == "neither":
        return True
    if decision == "ALLOW":
        return first_hit == "tp"
    else:  # DENY
        return first_hit == "sl"


def select_checkpoint_column(
    elapsed_minutes: float,
    existing_checkpoints: Dict[str, Any],
) -> Optional[str]:
    """Select which checkpoint column to fill based on elapsed time.

    Returns the column name if it should be filled (within window and still NULL),
    or None if no checkpoint should be written.
    """
    for min_m, max_m, col in CHECKPOINT_THRESHOLDS:
        if max_m is not None and elapsed_minutes >= max_m:
            continue
        if elapsed_minutes >= min_m and existing_checkpoints.get(col) is None:
            return col
        break
    return None


class OutcomeTrackerService:
    """Tracks LLM decision outcomes (no-op stub, DB removed)."""

    def __init__(
        self,
        stop_loss_pct: float = 0.8,
        take_profit_pct: float = 1.6,
        max_age_hours: int = 4,
    ) -> None:
        self._stop_loss_pct = Decimal(str(stop_loss_pct))
        self._take_profit_pct = Decimal(str(take_profit_pct))
        self._max_age_hours = max_age_hours

    async def log_decision(
        self,
        setup: Setup,
        decision: LLMDecision,
        market_state: Optional[MarketState],
        latency_ms: int,
    ) -> Optional[int]:
        """Log an LLM decision (no-op, DB removed)."""
        logger.debug(
            "LLM decision (not persisted): %s %s %s",
            decision.decision, setup.direction.value.upper(), setup.symbol,
        )
        return None

    async def check_pending(self, price_getter: PriceGetter) -> int:
        """Check pending decisions (no-op, DB removed)."""
        return 0

    async def get_performance_summary(self, days: int = 7) -> Dict[str, Any]:
        """Get aggregate LLM performance stats (no-op, DB removed)."""
        return {"total": 0}
