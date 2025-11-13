"""API endpoints for missed opportunities analysis."""

import logging
from typing import Optional

from fastapi import APIRouter, Query

from services.learning.missed_opportunities_analyzer import analyze_missed_opportunities

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/missed-opportunities", tags=["learning"])


@router.post("/analyze")
async def trigger_missed_opportunities_analysis(
    lookback_hours: int = Query(default=1, ge=1, le=24, description="Hours to look back"),
    min_move_pct: float = Query(default=10.0, ge=5.0, le=50.0, description="Minimum % move to consider"),
):
    """
    Manually trigger missed opportunities analysis.
    
    Analyzes top market movers in the last N hours and checks why AI didn't trade them.
    
    Args:
        lookback_hours: How many hours back to analyze (1-24)
        min_move_pct: Minimum price movement % to consider (5-50%)
    
    Returns:
        Analysis report with missed opportunities and recommendations
    """
    logger.info(f"Manual trigger: missed opportunities analysis (lookback={lookback_hours}h, min_move={min_move_pct}%)")
    
    result = await analyze_missed_opportunities(
        lookback_hours=lookback_hours,
        min_move_pct=min_move_pct,
    )
    
    return result


@router.get("/latest")
async def get_latest_analysis():
    """
    Get the most recent missed opportunities analysis from logs.
    
    NOTE: This endpoint returns cached results from the last scheduled run.
    Use POST /analyze to trigger a new analysis.
    """
    # For now, just trigger a new analysis
    # In future, we could cache results in database
    return {
        "message": "Use POST /api/missed-opportunities/analyze to run analysis",
        "scheduled_interval": "Every 1 hour",
    }
