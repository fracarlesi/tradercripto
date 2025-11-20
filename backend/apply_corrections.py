"""Apply corrections from daily learning report."""
import asyncio
from datetime import datetime
from sqlalchemy import select
from database.connection import get_async_session_factory, init_async_engine
from database.models import DailyLearningReport, Account, IndicatorWeightsHistory


async def apply_report_corrections(report_id: int = 1):
    """Apply weight corrections from a report."""
    async with get_async_session_factory()() as session:
        # Get report
        stmt = select(DailyLearningReport).where(DailyLearningReport.id == report_id)
        result = await session.execute(stmt)
        report = result.scalar_one_or_none()

        if not report:
            print(f"❌ Report {report_id} not found")
            return

        if not report.suggested_weights:
            print("❌ No weight suggestions in this report")
            return

        # Get account
        stmt = select(Account).where(Account.id == report.account_id)
        result = await session.execute(stmt)
        account = result.scalar_one_or_none()

        if not account:
            print(f"❌ Account {report.account_id} not found")
            return

        print("=" * 80)
        print("🔧 APPLYING CORRECTIONS")
        print("=" * 80)
        print()

        # Show old weights
        old_weights = account.indicator_weights or {}
        print("📊 OLD WEIGHTS:")
        for indicator, weight in sorted(old_weights.items()):
            print(f"  {indicator:20s} = {weight:.2f}")
        print()

        # Show new weights
        new_weights = report.suggested_weights
        print("📊 NEW WEIGHTS:")
        for indicator, weight in sorted(new_weights.items()):
            old = old_weights.get(indicator, 0)
            change = weight - old
            symbol = "+" if change > 0 else ""
            print(f"  {indicator:20s} = {weight:.2f}  ({symbol}{change:.2f})")
        print()

        # Apply weights
        account.indicator_weights = new_weights

        # Save to history
        history_entry = IndicatorWeightsHistory(
            account_id=account.id,
            old_weights=old_weights if old_weights else None,
            new_weights=new_weights,
            source="daily_learning"
        )
        session.add(history_entry)

        # Update report status
        report.status = "weights_applied"
        report.reviewed_at = datetime.utcnow()
        report.review_notes = "Applied via apply_corrections.py script"

        await session.commit()

        print("✅ Weights applied successfully!")
        print()

        # Show prompt changes
        if report.suggested_prompt_changes:
            changes = report.suggested_prompt_changes
            print("=" * 80)
            print("📝 PROMPT MODIFICATIONS NEEDED (Manual)")
            print("=" * 80)
            print()

            if changes.get('add_rules'):
                print("✅ RULES TO ADD TO PROMPT:")
                for i, rule in enumerate(changes['add_rules'], 1):
                    print(f"  {i}. {rule}")
                print()

            if changes.get('remove_rules'):
                print("❌ RULES TO REMOVE FROM PROMPT:")
                for i, rule in enumerate(changes['remove_rules'], 1):
                    print(f"  {i}. {rule}")
                print()

            print("📄 File to modify: backend/services/ai/deepseek_client.py")
            print("   Look for the prompt building section and update the rules.")
            print()

        print("=" * 80)
        print("✅ DONE!")
        print("=" * 80)


if __name__ == "__main__":
    init_async_engine()
    asyncio.run(apply_report_corrections(report_id=1))
