"""
Learning API Routes

Endpoints for counterfactual learning and self-analysis.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

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
