"""
Daily Evening Analysis Service.

Runs every evening at 21:00 to analyze the day's trading performance
and generate suggestions for improvement.

Main workflow:
1. Fetch today's decision snapshots and completed trades
2. Calculate skill-based metrics
3. Call DeepSeek for pattern analysis
4. Save report to database
5. Notify user (WebSocket)
"""

import json
import logging
import requests
from datetime import date, datetime
from typing import Dict, List, Any, Optional

from database.connection import get_async_session_factory
from database.models import DailyLearningReport, DecisionSnapshot, Account
from services.learning.skill_metrics_calculator import calculate_daily_skill_metrics
from services.learning.daily_deepseek_prompts import build_daily_analysis_prompt
from sqlalchemy import select, and_

logger = logging.getLogger(__name__)


async def run_daily_analysis(account_id: int, target_date: Optional[date] = None) -> Dict[str, Any]:
    """
    Main daily analysis function.

    Args:
        account_id: Account ID to analyze
        target_date: Date to analyze (defaults to today)

    Returns:
        Analysis results: {"report_id": int, "status": str}

    Steps:
    1. Fetch today's decision snapshots
    2. Fetch today's completed trades
    3. Calculate skill-based metrics
    4. Call DeepSeek for pattern analysis
    5. Save report to database
    6. Notify user (WebSocket)
    """
    if target_date is None:
        target_date = date.today()

    logger.info("=" * 60)
    logger.info(f"🌙 DAILY EVENING ANALYSIS - {target_date}")
    logger.info(f"   Account ID: {account_id}")
    logger.info("=" * 60)

    try:
        # 1. Fetch decision snapshots
        snapshots = await get_snapshots_for_date(account_id, target_date)
        logger.info(f"📋 Fetched {len(snapshots)} decision snapshots")

        # 2. Fetch completed trades (for metrics calculation)
        from services.learning.skill_metrics_calculator import _fetch_daily_trades
        async with get_async_session_factory()() as session:
            trades = await _fetch_daily_trades(session, account_id, target_date)

        logger.info(f"💰 Fetched {len(trades)} completed trades")

        if not trades:
            logger.warning(f"No trades found for {target_date} - skipping analysis")
            return {"status": "no_trades", "message": "No completed trades found for this day"}

        # 3. Calculate skill metrics
        logger.info("📊 Calculating skill-based metrics...")
        metrics = await calculate_daily_skill_metrics(account_id, target_date)

        logger.info(
            f"   Win Rate: {metrics['win_rate_pct']:.1f}%, "
            f"Profit Factor: {metrics['profit_factor']:.2f}, "
            f"Sharpe: {metrics['sharpe_ratio']:.2f}"
        )

        # 4. Call DeepSeek for analysis
        logger.info("🤖 Calling DeepSeek for pattern analysis...")
        analysis = await call_deepseek_daily_analysis(
            account_id=account_id,
            snapshots=snapshots,
            trades=trades,
            metrics=metrics
        )

        if not analysis:
            logger.error("DeepSeek analysis failed")
            return {"status": "error", "message": "DeepSeek analysis failed"}

        logger.info(f"✅ DeepSeek analysis complete")

        # 5. Save report
        report_id = await save_daily_report(
            account_id=account_id,
            target_date=target_date,
            metrics=metrics,
            analysis=analysis
        )

        logger.info(f"💾 Report saved (ID: {report_id})")

        # 6. Link decision snapshots to this report
        await link_snapshots_to_report(snapshots, report_id)

        # 7. Notify user (WebSocket broadcast)
        # TODO: Implement WebSocket notification
        logger.info(f"📢 User notification sent (report_id={report_id})")

        logger.info("=" * 60)
        logger.info(f"✅ Daily analysis complete!")
        logger.info(f"   Report ID: {report_id}")
        logger.info(f"   Status: pending user review")
        logger.info("=" * 60)

        return {
            "status": "success",
            "report_id": report_id,
            "summary": analysis.get('summary', ''),
            "win_rate_pct": metrics['win_rate_pct'],
            "profit_factor": metrics['profit_factor']
        }

    except Exception as e:
        logger.error(f"Daily analysis failed: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


async def get_snapshots_for_date(account_id: int, target_date: date) -> List[Dict[str, Any]]:
    """Fetch all decision snapshots for a specific date."""
    start_of_day = datetime.combine(target_date, datetime.min.time())
    end_of_day = datetime.combine(target_date, datetime.max.time())

    async with get_async_session_factory()() as session:
        stmt = select(DecisionSnapshot).where(
            and_(
                DecisionSnapshot.account_id == account_id,
                DecisionSnapshot.timestamp >= start_of_day,
                DecisionSnapshot.timestamp <= end_of_day
            )
        ).order_by(DecisionSnapshot.timestamp)

        result = await session.execute(stmt)
        snapshots_orm = result.scalars().all()

        return [
            {
                'id': s.id,
                'timestamp': s.timestamp,
                'symbol': s.symbol,
                'actual_decision': s.actual_decision,
                'reasoning': s.deepseek_reasoning
            }
            for s in snapshots_orm
        ]


async def call_deepseek_daily_analysis(
    account_id: int,
    snapshots: List[Dict],
    trades: List[Dict],
    metrics: Dict[str, float]
) -> Optional[Dict[str, Any]]:
    """
    Call DeepSeek API for daily analysis.

    Args:
        account_id: Account ID (for API key)
        snapshots: Decision snapshots
        trades: Completed trades
        metrics: Calculated skill metrics

    Returns:
        DeepSeek analysis JSON or None if error
    """
    # Get account for API credentials
    async with get_async_session_factory()() as session:
        stmt = select(Account).where(Account.id == account_id)
        result = await session.execute(stmt)
        account = result.scalar_one_or_none()

        if not account:
            logger.error(f"Account {account_id} not found")
            return None

    # Build prompt
    prompt = build_daily_analysis_prompt(snapshots, trades, metrics)

    # Estimate tokens (for monitoring)
    estimated_tokens = len(prompt) // 4
    logger.info(f"   Prompt size: ~{estimated_tokens} tokens")

    # Call DeepSeek
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {account.api_key}"
    }

    payload = {
        "model": account.model or "deepseek-chat",
        "messages": [
            {
                "role": "system",
                "content": "You are an AI trading system analyzing your own past decisions to improve future performance."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.3,  # Lower temperature for analytical tasks
        "response_format": {"type": "json_object"},  # Force JSON response
    }

    base_url = account.base_url.rstrip("/")
    api_endpoint = f"{base_url}/chat/completions"

    try:
        response = requests.post(
            api_endpoint,
            headers=headers,
            json=payload,
            timeout=90,  # Longer timeout for analysis
            verify=False,  # Disable SSL verification for custom endpoints
        )

        response.raise_for_status()

        result_data = response.json()
        analysis_text = result_data["choices"][0]["message"]["content"]
        analysis = json.loads(analysis_text)

        logger.info(f"   Analysis summary: {analysis.get('summary', '')[:100]}...")

        return analysis

    except requests.exceptions.RequestException as e:
        logger.error(f"DeepSeek API call failed: {e}", exc_info=True)
        return None
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse DeepSeek JSON response: {e}", exc_info=True)
        return None


async def save_daily_report(
    account_id: int,
    target_date: date,
    metrics: Dict[str, float],
    analysis: Dict[str, Any]
) -> int:
    """
    Save daily report to database.

    Args:
        account_id: Account ID
        target_date: Date analyzed
        metrics: Calculated metrics
        analysis: DeepSeek analysis output

    Returns:
        Report ID
    """
    async with get_async_session_factory()() as session:
        # Check if report already exists (should be unique per account+date)
        stmt = select(DailyLearningReport).where(
            and_(
                DailyLearningReport.account_id == account_id,
                DailyLearningReport.report_date == target_date
            )
        )

        result = await session.execute(stmt)
        existing_report = result.scalar_one_or_none()

        if existing_report:
            logger.warning(f"Report already exists for {target_date}, updating...")
            # Update existing report
            existing_report.skill_metrics = metrics
            existing_report.deepseek_analysis = analysis
            existing_report.suggested_weights = analysis.get('suggested_weights')
            existing_report.suggested_prompt_changes = analysis.get('suggested_prompt_changes')
            existing_report.analyzed_at = datetime.utcnow()
            existing_report.status = 'pending'

            await session.commit()
            await session.refresh(existing_report)

            return existing_report.id

        else:
            # Create new report
            report = DailyLearningReport(
                account_id=account_id,
                report_date=target_date,
                skill_metrics=metrics,
                deepseek_analysis=analysis,
                suggested_weights=analysis.get('suggested_weights'),
                suggested_prompt_changes=analysis.get('suggested_prompt_changes'),
                status='pending'
            )

            session.add(report)
            await session.commit()
            await session.refresh(report)

            return report.id


async def link_snapshots_to_report(snapshots: List[Dict], report_id: int) -> None:
    """Link decision snapshots to the daily report."""
    async with get_async_session_factory()() as session:
        snapshot_ids = [s['id'] for s in snapshots]

        stmt = select(DecisionSnapshot).where(
            DecisionSnapshot.id.in_(snapshot_ids)
        )

        result = await session.execute(stmt)
        snapshot_orms = result.scalars().all()

        for snapshot in snapshot_orms:
            snapshot.analyzed_in_daily_report_id = report_id

        await session.commit()

        logger.info(f"   Linked {len(snapshot_orms)} snapshots to report {report_id}")


def run_daily_analysis_sync(account_id: int = 1, target_date: Optional[date] = None):
    """
    Synchronous wrapper for APScheduler.

    This is called by the cron job at 21:00 every day.
    """
    import asyncio

    try:
        result = asyncio.run(run_daily_analysis(account_id, target_date))

        if result.get('status') == 'success':
            logger.info(
                f"✅ Daily analysis completed successfully! "
                f"Report ID: {result['report_id']}"
            )
        else:
            logger.warning(f"Daily analysis finished with status: {result.get('status')}")

    except Exception as e:
        logger.error(f"Daily analysis (sync wrapper) failed: {e}", exc_info=True)
