"""API endpoints for missed opportunities analysis."""

import logging
from typing import Optional

from fastapi import APIRouter, Query, Depends
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import get_db
from database.models import MissedOpportunitiesReport, IndicatorWeightsHistory, Account
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


@router.get("/reports")
async def get_reports(
    limit: int = Query(default=10, ge=1, le=50, description="Number of reports to fetch"),
    db: AsyncSession = Depends(get_db),
):
    """
    Get recent missed opportunities reports.

    Returns list of reports with summary stats, patterns, and recommendations.
    """
    result = await db.execute(
        select(MissedOpportunitiesReport)
        .order_by(desc(MissedOpportunitiesReport.analyzed_at))
        .limit(limit)
    )
    reports = result.scalars().all()

    return {
        "total": len(reports),
        "reports": [
            {
                "id": r.id,
                "analyzed_at": r.analyzed_at.isoformat(),
                "lookback_hours": r.lookback_hours,
                "summary": {
                    "total_movers": r.total_movers,
                    "analyzed_movers": r.analyzed_movers,
                    "gainers_missed": r.gainers_missed,
                    "losers_missed": r.losers_missed,
                },
                "patterns": r.patterns_identified,
                "recommendations": r.recommendations,
                "status": r.status,
            }
            for r in reports
        ],
    }


@router.get("/reports/{report_id}")
async def get_report_detail(
    report_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Get detailed report including full text and all opportunities.
    """
    result = await db.execute(
        select(MissedOpportunitiesReport)
        .where(MissedOpportunitiesReport.id == report_id)
    )
    report = result.scalar_one_or_none()

    if not report:
        return {"error": "Report not found"}

    return {
        "id": report.id,
        "analyzed_at": report.analyzed_at.isoformat(),
        "lookback_hours": report.lookback_hours,
        "min_move_pct": float(report.min_move_pct),
        "summary": {
            "total_movers": report.total_movers,
            "analyzed_movers": report.analyzed_movers,
            "gainers_missed": report.gainers_missed,
            "losers_missed": report.losers_missed,
        },
        "missed_opportunities": report.missed_opportunities,
        "patterns": report.patterns_identified,
        "recommendations": report.recommendations,
        "report_text": report.report_text,
        "status": report.status,
    }


@router.get("/weights-history")
async def get_weights_history(
    account_id: int = Query(default=1, description="Account ID"),
    limit: int = Query(default=20, ge=1, le=100, description="Number of history entries"),
    db: AsyncSession = Depends(get_db),
):
    """
    Get indicator weights change history for an account.

    Shows how weights have evolved over time.
    """
    result = await db.execute(
        select(IndicatorWeightsHistory)
        .where(IndicatorWeightsHistory.account_id == account_id)
        .order_by(desc(IndicatorWeightsHistory.applied_at))
        .limit(limit)
    )
    history = result.scalars().all()

    # Get current weights
    account_result = await db.execute(
        select(Account.indicator_weights)
        .where(Account.id == account_id)
    )
    current_weights = account_result.scalar_one_or_none()

    return {
        "account_id": account_id,
        "current_weights": current_weights,
        "history": [
            {
                "id": h.id,
                "applied_at": h.applied_at.isoformat(),
                "source": h.source,
                "old_weights": h.old_weights,
                "new_weights": h.new_weights,
            }
            for h in history
        ],
    }
