"""
Learning API Routes

Endpoints for counterfactual learning and self-analysis.
"""

import logging
from typing import Optional

from datetime import datetime
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from database.connection import get_async_session_factory
from database.models import PendingStrategySuggestion
from services.learning import (
    calculate_counterfactuals_batch,
    get_snapshots_for_analysis,
    run_self_analysis,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/learning", tags=["learning"])


class SelfAnalysisResponse(BaseModel):
    """Response from self-analysis endpoint."""

    total_regret_usd: float
    total_actual_pnl: float
    potential_pnl_if_perfect: float
    accuracy_rate: float
    worst_patterns: list
    best_patterns: list
    indicator_performance: dict
    suggested_weights: dict
    new_rules: list
    summary: str


@router.post("/analyze/{account_id}", response_model=SelfAnalysisResponse)
async def trigger_self_analysis(
    account_id: int,
    limit: int = Query(100, ge=10, le=500, description="Number of decisions to analyze"),
    min_regret: Optional[float] = Query(
        None, ge=0.0, description="Only analyze decisions with regret >= this value"
    ),
):
    """
    Trigger DeepSeek self-analysis for an account.

    Analyzes past decisions (both executed and missed opportunities) to:
    - Identify systematic errors
    - Calculate indicator performance
    - Suggest optimal weights
    - Propose new trading rules

    **Prerequisites**:
    - Account must have decision snapshots with calculated counterfactuals
    - Counterfactuals are calculated 24h after each decision by batch job

    **Example usage**:
    ```bash
    curl -X POST "http://localhost:8000/api/learning/analyze/1?limit=50"
    ```

    Returns:
        Analysis results with suggested improvements
    """
    try:
        logger.info(
            f"Self-analysis requested for account_id={account_id}, "
            f"limit={limit}, min_regret={min_regret}"
        )

        analysis = await run_self_analysis(
            account_id=account_id, limit=limit, min_regret=min_regret
        )

        if "error" in analysis:
            raise HTTPException(status_code=400, detail=analysis["error"])

        return analysis

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Self-analysis failed for account_id={account_id}: {e}",
            extra={"context": {"account_id": account_id, "error": str(e)}},
            exc_info=True,
        )
        raise HTTPException(
            status_code=500, detail=f"Self-analysis failed: {str(e)}"
        ) from e


@router.post("/counterfactuals/calculate")
async def trigger_counterfactual_calculation(
    limit: int = Query(100, ge=1, le=1000, description="Max snapshots to process")
):
    """
    Manually trigger counterfactual P&L calculation.

    This is normally run as a scheduled batch job every hour.
    Use this endpoint to trigger it manually for testing or debugging.

    **What it does**:
    - Finds decision snapshots older than 24h without counterfactuals
    - Fetches price 24h after decision
    - Calculates P&L for LONG, SHORT, HOLD
    - Determines optimal decision and regret

    **Example usage**:
    ```bash
    curl -X POST "http://localhost:8000/api/learning/counterfactuals/calculate?limit=50"
    ```

    Returns:
        Number of snapshots processed
    """
    try:
        logger.info(f"Manual counterfactual calculation triggered (limit={limit})")

        processed = await calculate_counterfactuals_batch(limit=limit)

        return {
            "processed": processed,
            "message": f"Calculated counterfactuals for {processed} snapshots",
        }

    except Exception as e:
        logger.error(
            f"Counterfactual calculation failed: {e}",
            extra={"context": {"limit": limit, "error": str(e)}},
            exc_info=True,
        )
        raise HTTPException(
            status_code=500, detail=f"Counterfactual calculation failed: {str(e)}"
        ) from e


@router.get("/snapshots/{account_id}")
async def get_decision_snapshots(
    account_id: int,
    limit: int = Query(50, ge=1, le=500, description="Max snapshots to return"),
    min_regret: Optional[float] = Query(
        None, ge=0.0, description="Filter by minimum regret"
    ),
):
    """
    Get decision snapshots for an account.

    Returns recent decision snapshots with counterfactual analysis.
    Useful for debugging or manual review.

    **Example usage**:
    ```bash
    curl "http://localhost:8000/api/learning/snapshots/1?limit=20&min_regret=10"
    ```

    Returns:
        List of decision snapshots
    """
    try:
        snapshots = await get_snapshots_for_analysis(
            account_id=account_id, limit=limit, min_regret=min_regret
        )

        return {"snapshots": snapshots, "count": len(snapshots)}

    except Exception as e:
        logger.error(
            f"Failed to get snapshots for account_id={account_id}: {e}",
            extra={"context": {"account_id": account_id, "error": str(e)}},
            exc_info=True,
        )
        raise HTTPException(
            status_code=500, detail=f"Failed to get snapshots: {str(e)}"
        ) from e


# ============================================================================
# PENDING STRATEGY SUGGESTIONS ENDPOINTS
# ============================================================================


class SuggestionResponse(BaseModel):
    """Response model for a single suggestion."""
    id: int
    created_at: datetime
    source: str
    suggestion_type: str
    symbol: Optional[str]
    suggestion_data: dict
    reason: str
    evidence: Optional[dict]
    status: str
    reviewed_at: Optional[datetime]
    review_notes: Optional[str]


class SuggestionsListResponse(BaseModel):
    """Response model for list of suggestions."""
    suggestions: list[SuggestionResponse]
    count: int
    pending_count: int


@router.get("/suggestions", response_model=SuggestionsListResponse)
async def get_pending_suggestions(
    status: Optional[str] = Query(None, description="Filter by status: pending, applied, dismissed"),
    limit: int = Query(50, ge=1, le=200, description="Max suggestions to return"),
):
    """
    Get pending strategy suggestions for manual review.

    Returns suggestions generated by the learning system (hourly retrospective, self-analysis)
    that are waiting for manual review.

    **Example usage**:
    ```bash
    # Get all pending suggestions
    curl "http://localhost:8000/api/learning/suggestions?status=pending"

    # Get all suggestions
    curl "http://localhost:8000/api/learning/suggestions?limit=100"
    ```

    Returns:
        List of suggestions with their details
    """
    try:
        async with get_async_session_factory()() as session:
            # Build query
            query = select(PendingStrategySuggestion).order_by(
                PendingStrategySuggestion.created_at.desc()
            ).limit(limit)

            if status:
                query = query.where(PendingStrategySuggestion.status == status)

            result = await session.execute(query)
            suggestions = result.scalars().all()

            # Count pending
            pending_query = select(PendingStrategySuggestion).where(
                PendingStrategySuggestion.status == "pending"
            )
            pending_result = await session.execute(pending_query)
            pending_count = len(pending_result.scalars().all())

            return {
                "suggestions": [
                    {
                        "id": s.id,
                        "created_at": s.created_at,
                        "source": s.source,
                        "suggestion_type": s.suggestion_type,
                        "symbol": s.symbol,
                        "suggestion_data": s.suggestion_data,
                        "reason": s.reason,
                        "evidence": s.evidence,
                        "status": s.status,
                        "reviewed_at": s.reviewed_at,
                        "review_notes": s.review_notes,
                    }
                    for s in suggestions
                ],
                "count": len(suggestions),
                "pending_count": pending_count,
            }

    except Exception as e:
        logger.error(f"Failed to get suggestions: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to get suggestions: {str(e)}"
        ) from e


@router.get("/suggestions/{suggestion_id}", response_model=SuggestionResponse)
async def get_suggestion_detail(suggestion_id: int):
    """
    Get details of a specific suggestion.

    **Example usage**:
    ```bash
    curl "http://localhost:8000/api/learning/suggestions/123"
    ```
    """
    try:
        async with get_async_session_factory()() as session:
            result = await session.execute(
                select(PendingStrategySuggestion).where(
                    PendingStrategySuggestion.id == suggestion_id
                )
            )
            suggestion = result.scalar_one_or_none()

            if not suggestion:
                raise HTTPException(status_code=404, detail=f"Suggestion {suggestion_id} not found")

            return {
                "id": suggestion.id,
                "created_at": suggestion.created_at,
                "source": suggestion.source,
                "suggestion_type": suggestion.suggestion_type,
                "symbol": suggestion.symbol,
                "suggestion_data": suggestion.suggestion_data,
                "reason": suggestion.reason,
                "evidence": suggestion.evidence,
                "status": suggestion.status,
                "reviewed_at": suggestion.reviewed_at,
                "review_notes": suggestion.review_notes,
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get suggestion {suggestion_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to get suggestion: {str(e)}"
        ) from e


@router.post("/suggestions/{suggestion_id}/dismiss")
async def dismiss_suggestion(
    suggestion_id: int,
    notes: Optional[str] = Query(None, description="Optional notes for why it was dismissed"),
):
    """
    Dismiss a pending suggestion (mark as not to be applied).

    **Example usage**:
    ```bash
    curl -X POST "http://localhost:8000/api/learning/suggestions/123/dismiss?notes=Not%20relevant"
    ```
    """
    try:
        async with get_async_session_factory()() as session:
            result = await session.execute(
                select(PendingStrategySuggestion).where(
                    PendingStrategySuggestion.id == suggestion_id
                )
            )
            suggestion = result.scalar_one_or_none()

            if not suggestion:
                raise HTTPException(status_code=404, detail=f"Suggestion {suggestion_id} not found")

            if suggestion.status != "pending":
                raise HTTPException(
                    status_code=400,
                    detail=f"Suggestion {suggestion_id} is already {suggestion.status}"
                )

            suggestion.status = "dismissed"
            suggestion.reviewed_at = datetime.utcnow()
            suggestion.review_notes = notes

            await session.commit()

            logger.info(f"Suggestion {suggestion_id} dismissed (notes: {notes})")

            return {
                "id": suggestion_id,
                "status": "dismissed",
                "message": f"Suggestion {suggestion_id} has been dismissed"
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to dismiss suggestion {suggestion_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to dismiss suggestion: {str(e)}"
        ) from e


@router.post("/suggestions/{suggestion_id}/mark-applied")
async def mark_suggestion_applied(
    suggestion_id: int,
    notes: Optional[str] = Query(None, description="Notes about how it was applied"),
):
    """
    Mark a suggestion as applied (after manual implementation).

    Use this after you've manually applied the suggested changes to the code.

    **Example usage**:
    ```bash
    curl -X POST "http://localhost:8000/api/learning/suggestions/123/mark-applied?notes=Applied%20in%20commit%20abc123"
    ```
    """
    try:
        async with get_async_session_factory()() as session:
            result = await session.execute(
                select(PendingStrategySuggestion).where(
                    PendingStrategySuggestion.id == suggestion_id
                )
            )
            suggestion = result.scalar_one_or_none()

            if not suggestion:
                raise HTTPException(status_code=404, detail=f"Suggestion {suggestion_id} not found")

            if suggestion.status != "pending":
                raise HTTPException(
                    status_code=400,
                    detail=f"Suggestion {suggestion_id} is already {suggestion.status}"
                )

            suggestion.status = "applied"
            suggestion.reviewed_at = datetime.utcnow()
            suggestion.review_notes = notes

            await session.commit()

            logger.info(f"Suggestion {suggestion_id} marked as applied (notes: {notes})")

            return {
                "id": suggestion_id,
                "status": "applied",
                "message": f"Suggestion {suggestion_id} has been marked as applied"
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to mark suggestion {suggestion_id} as applied: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to mark suggestion as applied: {str(e)}"
        ) from e


@router.get("/suggestions/summary/stats")
async def get_suggestions_summary():
    """
    Get summary statistics of all suggestions.

    **Example usage**:
    ```bash
    curl "http://localhost:8000/api/learning/suggestions/summary/stats"
    ```
    """
    try:
        async with get_async_session_factory()() as session:
            # Get all suggestions
            result = await session.execute(select(PendingStrategySuggestion))
            all_suggestions = result.scalars().all()

            # Calculate stats
            total = len(all_suggestions)
            by_status = {"pending": 0, "applied": 0, "dismissed": 0, "expired": 0}
            by_type = {}
            total_missed_profit = 0

            for s in all_suggestions:
                by_status[s.status] = by_status.get(s.status, 0) + 1
                by_type[s.suggestion_type] = by_type.get(s.suggestion_type, 0) + 1
                if s.evidence and "missed_profit" in s.evidence:
                    total_missed_profit += s.evidence["missed_profit"]

            return {
                "total": total,
                "by_status": by_status,
                "by_type": by_type,
                "total_missed_profit": round(total_missed_profit, 2),
            }

    except Exception as e:
        logger.error(f"Failed to get suggestions summary: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to get suggestions summary: {str(e)}"
        ) from e
