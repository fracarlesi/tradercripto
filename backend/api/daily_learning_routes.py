"""
Daily Learning API routes.

Endpoints for reviewing and applying daily learning suggestions.
"""

import logging
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select, and_, desc
from pydantic import BaseModel

from database.connection import get_async_session_factory
from database.models import DailyLearningReport, Account, IndicatorWeightsHistory
from services.learning.daily_analysis_service import run_daily_analysis

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/daily-learning", tags=["daily-learning"])


# ============================================================================
# Pydantic Models
# ============================================================================

class DailyReportSummary(BaseModel):
    """Summary of a daily report for list view."""
    id: int
    report_date: str
    analyzed_at: str
    status: str
    win_rate_pct: float
    profit_factor: float
    total_trades: int
    suggested_weights_count: int
    suggested_rules_count: int

    class Config:
        from_attributes = True


class DailyReportDetail(BaseModel):
    """Full daily report detail."""
    id: int
    account_id: int
    report_date: str
    analyzed_at: str
    status: str
    skill_metrics: dict
    deepseek_analysis: dict
    suggested_weights: Optional[dict]
    suggested_prompt_changes: Optional[dict]
    reviewed_at: Optional[str]
    review_notes: Optional[str]

    class Config:
        from_attributes = True


class ApplyWeightsRequest(BaseModel):
    """Request to apply suggested weights."""
    notes: Optional[str] = None


class DismissRequest(BaseModel):
    """Request to dismiss a report."""
    reason: str


# ============================================================================
# API Endpoints
# ============================================================================

@router.get("/reports/{account_id}", response_model=List[DailyReportSummary])
async def get_daily_reports(
    account_id: int,
    limit: int = Query(30, ge=1, le=100),
    status: Optional[str] = Query(None, regex="^(pending|reviewed|weights_applied|prompts_applied|dismissed)$")
):
    """
    Get list of daily learning reports for an account.

    Args:
        account_id: Account ID
        limit: Max number of reports to return (1-100)
        status: Filter by status (optional)

    Returns:
        List of report summaries ordered by date (newest first)
    """
    async with get_async_session_factory()() as session:
        try:
            # Build query
            stmt = select(DailyLearningReport).where(
                DailyLearningReport.account_id == account_id
            )

            if status:
                stmt = stmt.where(DailyLearningReport.status == status)

            stmt = stmt.order_by(desc(DailyLearningReport.report_date)).limit(limit)

            result = await session.execute(stmt)
            reports = result.scalars().all()

            # Convert to summaries
            summaries = []
            for r in reports:
                summaries.append(DailyReportSummary(
                    id=r.id,
                    report_date=r.report_date.isoformat(),
                    analyzed_at=r.analyzed_at.isoformat(),
                    status=r.status,
                    win_rate_pct=r.skill_metrics.get('win_rate_pct', 0),
                    profit_factor=r.skill_metrics.get('profit_factor', 0),
                    total_trades=r.skill_metrics.get('total_trades', 0),
                    suggested_weights_count=len(r.suggested_weights) if r.suggested_weights else 0,
                    suggested_rules_count=(
                        len(r.suggested_prompt_changes.get('add_rules', [])) +
                        len(r.suggested_prompt_changes.get('remove_rules', []))
                    ) if r.suggested_prompt_changes else 0
                ))

            return summaries

        except Exception as e:
            logger.error(f"Failed to get daily reports: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))


@router.get("/reports/{account_id}/{report_date}", response_model=DailyReportDetail)
async def get_daily_report(
    account_id: int,
    report_date: date
):
    """
    Get specific daily report by date.

    Args:
        account_id: Account ID
        report_date: Date of report (YYYY-MM-DD)

    Returns:
        Full report detail
    """
    async with get_async_session_factory()() as session:
        try:
            stmt = select(DailyLearningReport).where(
                and_(
                    DailyLearningReport.account_id == account_id,
                    DailyLearningReport.report_date == report_date
                )
            )

            result = await session.execute(stmt)
            report = result.scalar_one_or_none()

            if not report:
                raise HTTPException(
                    status_code=404,
                    detail=f"No report found for {report_date}"
                )

            return DailyReportDetail(
                id=report.id,
                account_id=report.account_id,
                report_date=report.report_date.isoformat(),
                analyzed_at=report.analyzed_at.isoformat(),
                status=report.status,
                skill_metrics=report.skill_metrics,
                deepseek_analysis=report.deepseek_analysis,
                suggested_weights=report.suggested_weights,
                suggested_prompt_changes=report.suggested_prompt_changes,
                reviewed_at=report.reviewed_at.isoformat() if report.reviewed_at else None,
                review_notes=report.review_notes
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get daily report: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))


@router.post("/reports/{report_id}/apply-weights")
async def apply_suggested_weights(
    report_id: int,
    request: ApplyWeightsRequest
):
    """
    Apply suggested indicator weights from report.

    This updates the account's indicator_weights field and saves
    the change to IndicatorWeightsHistory.

    Args:
        report_id: Report ID
        request: Optional notes about the application

    Returns:
        Success message with old and new weights
    """
    async with get_async_session_factory()() as session:
        try:
            # Get report
            stmt = select(DailyLearningReport).where(DailyLearningReport.id == report_id)
            result = await session.execute(stmt)
            report = result.scalar_one_or_none()

            if not report:
                raise HTTPException(status_code=404, detail="Report not found")

            if not report.suggested_weights:
                raise HTTPException(status_code=400, detail="Report has no weight suggestions")

            # Get account
            stmt = select(Account).where(Account.id == report.account_id)
            result = await session.execute(stmt)
            account = result.scalar_one_or_none()

            if not account:
                raise HTTPException(status_code=404, detail="Account not found")

            # Save old weights
            old_weights = account.indicator_weights or {}

            # Apply new weights
            account.indicator_weights = report.suggested_weights

            # Save to history
            history_entry = IndicatorWeightsHistory(
                account_id=account.id,
                old_weights=old_weights if old_weights else None,
                new_weights=report.suggested_weights,
                source="daily_learning"
            )
            session.add(history_entry)

            # Update report status
            report.status = "weights_applied"
            report.reviewed_at = datetime.utcnow()
            if request.notes:
                report.review_notes = request.notes

            await session.commit()

            logger.info(
                f"✅ Applied weights from report {report_id} to account {account.id}",
                extra={
                    "context": {
                        "report_id": report_id,
                        "account_id": account.id,
                        "old_weights": old_weights,
                        "new_weights": report.suggested_weights
                    }
                }
            )

            return {
                "status": "success",
                "message": "Weights applied successfully",
                "old_weights": old_weights,
                "new_weights": report.suggested_weights,
                "diff": {
                    key: {
                        "old": old_weights.get(key, 0),
                        "new": report.suggested_weights.get(key, 0),
                        "change": report.suggested_weights.get(key, 0) - old_weights.get(key, 0)
                    }
                    for key in report.suggested_weights.keys()
                }
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to apply weights: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))


@router.get("/reports/{report_id}/prompt-instructions")
async def get_prompt_instructions(report_id: int):
    """
    Get manual instructions for applying prompt changes.

    Since prompt changes require code modifications, this endpoint
    returns instructions for the user to manually update the prompt.

    Args:
        report_id: Report ID

    Returns:
        Instructions with suggested changes
    """
    async with get_async_session_factory()() as session:
        try:
            stmt = select(DailyLearningReport).where(DailyLearningReport.id == report_id)
            result = await session.execute(stmt)
            report = result.scalar_one_or_none()

            if not report:
                raise HTTPException(status_code=404, detail="Report not found")

            if not report.suggested_prompt_changes:
                raise HTTPException(status_code=400, detail="Report has no prompt suggestions")

            changes = report.suggested_prompt_changes
            add_rules = changes.get('add_rules', [])
            remove_rules = changes.get('remove_rules', [])

            instructions = f"""
# Manual Prompt Update Instructions

## File to Edit
`backend/services/ai/deepseek_client.py`

## Changes Suggested

### Rules to ADD:
"""

            for rule in add_rules:
                instructions += f"\n- {rule}"

            instructions += "\n\n### Rules to REMOVE:\n"

            for rule in remove_rules:
                instructions += f"\n- {rule}"

            instructions += """

## How to Apply

1. Open `backend/services/ai/deepseek_client.py`
2. Find the `_build_json_prompt()` method
3. Locate the decision rules section
4. Add/remove the rules listed above
5. Test the changes locally
6. Deploy to production

## Mark as Applied

After applying changes manually, call:
POST /api/daily-learning/reports/{report_id}/mark-prompts-applied
"""

            return {
                "status": "instructions_ready",
                "file_path": "backend/services/ai/deepseek_client.py",
                "add_rules": add_rules,
                "remove_rules": remove_rules,
                "instructions_markdown": instructions
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get prompt instructions: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))


@router.post("/reports/{report_id}/mark-prompts-applied")
async def mark_prompts_applied(report_id: int, request: ApplyWeightsRequest):
    """
    Mark prompt changes as applied (after manual update).

    Args:
        report_id: Report ID
        request: Optional notes

    Returns:
        Success message
    """
    async with get_async_session_factory()() as session:
        try:
            stmt = select(DailyLearningReport).where(DailyLearningReport.id == report_id)
            result = await session.execute(stmt)
            report = result.scalar_one_or_none()

            if not report:
                raise HTTPException(status_code=404, detail="Report not found")

            report.status = "prompts_applied"
            report.reviewed_at = datetime.utcnow()
            if request.notes:
                report.review_notes = request.notes

            await session.commit()

            logger.info(f"✅ Marked prompts as applied for report {report_id}")

            return {
                "status": "success",
                "message": "Report marked as prompts_applied"
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to mark prompts applied: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))


@router.post("/reports/{report_id}/dismiss")
async def dismiss_report(report_id: int, request: DismissRequest):
    """
    Dismiss a report without applying changes.

    Args:
        report_id: Report ID
        request: Reason for dismissal

    Returns:
        Success message
    """
    async with get_async_session_factory()() as session:
        try:
            stmt = select(DailyLearningReport).where(DailyLearningReport.id == report_id)
            result = await session.execute(stmt)
            report = result.scalar_one_or_none()

            if not report:
                raise HTTPException(status_code=404, detail="Report not found")

            report.status = "dismissed"
            report.reviewed_at = datetime.utcnow()
            report.review_notes = f"Dismissed: {request.reason}"

            await session.commit()

            logger.info(f"✅ Dismissed report {report_id}: {request.reason}")

            return {
                "status": "success",
                "message": "Report dismissed"
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to dismiss report: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))


@router.post("/trigger-analysis/{account_id}")
async def trigger_manual_analysis(
    account_id: int,
    target_date: Optional[date] = Query(None, description="Date to analyze (defaults to today)")
):
    """
    Manually trigger daily analysis (for testing or backfill).

    Args:
        account_id: Account ID
        target_date: Date to analyze (defaults to today)

    Returns:
        Analysis result
    """
    try:
        result = await run_daily_analysis(account_id, target_date)

        return result

    except Exception as e:
        logger.error(f"Manual analysis trigger failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
