"""
HLQuantBot Outcome Tracker Service
====================================

Tracks every LLM decision (ALLOW/DENY), monitors price evolution,
determines if TP/SL would have been hit, and calculates MFE/MAE.

NOT a BaseService — called synchronously from the main loop.
Zero extra background tasks, zero additional API calls.

Usage:
    tracker = OutcomeTrackerService(db=db, stop_loss_pct=0.8, take_profit_pct=1.6)
    await tracker.log_decision(setup, decision, market_state, latency_ms=120)
    await tracker.check_pending(price_getter)
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
        ALLOW + TP hit    → correct (allowed a winner)
        ALLOW + SL hit    → incorrect (allowed a loser)
        ALLOW + neither   → correct (no harm)
        DENY  + TP hit    → incorrect (blocked a winner)
        DENY  + SL hit    → correct (blocked a loser)
        DENY  + neither   → correct (no harm)
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
    """Tracks LLM decision outcomes with price checkpoints and MFE/MAE."""

    def __init__(
        self,
        db: Any,
        stop_loss_pct: float,
        take_profit_pct: float,
        max_age_hours: int = 4,
    ) -> None:
        self._db = db
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
        """Log an LLM decision with full context snapshot.

        Returns:
            Row ID of the inserted decision, or None on error.
        """
        try:
            tp_price = compute_tp_price(
                setup.entry_price,
                setup.direction.value,
                self._take_profit_pct,
            )

            row_id = await self._db.insert_llm_decision(
                symbol=setup.symbol,
                direction=setup.direction.value,
                regime=setup.regime.value,
                entry_price=setup.entry_price,
                stop_price=setup.stop_price,
                tp_price=tp_price,
                decision=decision.decision,
                confidence=decision.confidence,
                reason=decision.reason[:500],
                latency_ms=latency_ms,
                adx=setup.adx,
                rsi=setup.rsi,
                atr=setup.atr,
                ema9=market_state.ema9 if market_state else None,
                ema21=market_state.ema21 if market_state else None,
                volume_ratio=market_state.volume_ratio if market_state else None,
            )

            logger.info(
                "Logged LLM decision: %s %s %s (id=%d, latency=%dms)",
                decision.decision, setup.direction.value.upper(),
                setup.symbol, row_id, latency_ms,
            )
            return row_id

        except Exception as e:
            logger.error("Failed to log LLM decision: %s", e)
            return None

    async def check_pending(self, price_getter: PriceGetter) -> int:
        """Check all pending decisions: update checkpoints, resolve outcomes.

        Args:
            price_getter: Async callable(symbol) -> Optional[Decimal] for current price.

        Returns:
            Number of decisions resolved in this call.
        """
        resolved_count = 0

        try:
            pending = await self._db.get_pending_llm_decisions(
                max_age_hours=self._max_age_hours,
            )
        except Exception as e:
            logger.error("Failed to fetch pending decisions: %s", e)
            return 0

        now = datetime.now(timezone.utc)

        for row in pending:
            try:
                symbol = row["symbol"]
                current_price = await price_getter(symbol)
                if current_price is None:
                    continue

                decision_id = row["id"]
                decided_at = row["decided_at"]
                entry_price = Decimal(str(row["entry_price"]))
                stop_price = Decimal(str(row["stop_price"]))
                tp_price = Decimal(str(row["tp_price"]))
                direction = row["direction"]
                decision_str = row["decision"]
                elapsed = (now - decided_at).total_seconds() / 60.0

                # Compute MFE/MAE
                favorable, adverse = compute_mfe_mae(
                    entry_price, current_price, direction,
                )

                # Select checkpoint column to fill
                existing = {
                    "price_5m": row.get("price_5m"),
                    "price_15m": row.get("price_15m"),
                    "price_30m": row.get("price_30m"),
                    "price_1h": row.get("price_1h"),
                    "price_2h": row.get("price_2h"),
                    "price_4h": row.get("price_4h"),
                }
                checkpoint_col = select_checkpoint_column(elapsed, existing)

                if checkpoint_col:
                    await self._db.update_decision_checkpoint(
                        decision_id=decision_id,
                        decided_at=decided_at,
                        column=checkpoint_col,
                        price=current_price,
                        favorable_pct=favorable,
                        adverse_pct=adverse,
                    )

                # Check TP/SL hit
                hit = check_tp_sl_hit(
                    entry_price, current_price, stop_price, tp_price, direction,
                )

                if hit:
                    was_correct = determine_was_correct(decision_str, hit)
                    await self._db.resolve_llm_decision(
                        decision_id=decision_id,
                        decided_at=decided_at,
                        first_hit=hit,
                        time_to_hit_min=int(elapsed),
                        was_correct=was_correct,
                    )
                    resolved_count += 1
                    logger.info(
                        "LLM decision resolved: %s %s %s → %s (correct=%s, %.0fmin, MFE=%.2f%% MAE=%.2f%%)",
                        decision_str, direction.upper(), symbol,
                        hit.upper(), was_correct, elapsed,
                        float(favorable), float(adverse),
                    )

                elif elapsed >= self._max_age_hours * 60:
                    # Timeout — neither TP nor SL hit
                    was_correct = determine_was_correct(decision_str, "neither")
                    await self._db.resolve_llm_decision(
                        decision_id=decision_id,
                        decided_at=decided_at,
                        first_hit="neither",
                        time_to_hit_min=None,
                        was_correct=was_correct,
                    )
                    resolved_count += 1
                    logger.info(
                        "LLM decision timeout: %s %s %s → neither (correct=%s, MFE=%.2f%% MAE=%.2f%%)",
                        decision_str, direction.upper(), symbol,
                        was_correct,
                        float(favorable), float(adverse),
                    )

            except Exception as e:
                logger.error(
                    "Error processing pending decision %s: %s",
                    row.get("id"), e,
                )

        return resolved_count

    async def get_performance_summary(self, days: int = 7) -> Dict[str, Any]:
        """Get aggregate LLM performance stats from the database."""
        try:
            return await self._db.get_llm_performance(days=days)
        except Exception as e:
            logger.error("Failed to get LLM performance: %s", e)
            return {"total": 0, "error": str(e)}
