"""View the latest daily learning report."""
import json
import asyncio
from datetime import date
from sqlalchemy import select, desc
from database.connection import get_async_session_factory, init_async_engine
from database.models import DailyLearningReport


async def view_latest_report(account_id: int = 1):
    """Display the latest daily learning report."""
    async with get_async_session_factory()() as session:
        stmt = select(DailyLearningReport).where(
            DailyLearningReport.account_id == account_id
        ).order_by(desc(DailyLearningReport.report_date)).limit(1)

        result = await session.execute(stmt)
        report = result.scalar_one_or_none()

        if not report:
            print("❌ No reports found")
            return

        print("=" * 80)
        print(f"📊 DAILY LEARNING REPORT - {report.report_date}")
        print("=" * 80)
        print()

        # Skill Metrics
        print("📈 SKILL-BASED METRICS")
        print("-" * 80)
        metrics = report.skill_metrics
        print(f"  Total Trades:          {metrics['total_trades']}")
        print(f"  Winning Trades:        {metrics['winning_trades']} ({metrics['win_rate_pct']:.1f}%)")
        print(f"  Losing Trades:         {metrics['losing_trades']}")
        print(f"  Profit Factor:         {metrics['profit_factor']:.2f}")
        print(f"  Risk/Reward Ratio:     {metrics['risk_reward_ratio']:.2f}")
        print(f"  Max Drawdown:          {metrics['max_drawdown_pct']:.1f}%")
        print(f"  Sharpe Ratio:          {metrics['sharpe_ratio']:.2f}")
        print(f"  Sortino Ratio:         {metrics['sortino_ratio']:.2f}")
        print(f"  Entry Timing Quality:  {metrics['entry_timing_quality_pct']:.1f}%")
        print(f"  Exit Timing Quality:   {metrics['exit_timing_quality_pct']:.1f}%")
        print(f"  False Signal Rate:     {metrics['false_signal_rate_pct']:.1f}%")
        print(f"  Avg Hold Time:         {metrics['avg_hold_time_hours']:.1f}h")
        print()

        # DeepSeek Analysis
        analysis = report.deepseek_analysis
        print("🤖 DEEPSEEK ANALYSIS")
        print("-" * 80)
        print(f"\n📝 Summary:\n{analysis.get('summary', 'N/A')}\n")

        # Indicator Performance
        if 'indicator_performance' in analysis:
            print("📊 Indicator Performance:")
            for indicator, perf in analysis['indicator_performance'].items():
                print(f"\n  {indicator.upper()}:")
                print(f"    Accuracy:   {perf.get('accuracy_pct', 0):.1f}%")
                print(f"    Times Used: {perf.get('times_used', 0)}")
                print(f"    Win Rate:   {perf.get('win_rate', 0):.1f}%")
                print(f"    Notes:      {perf.get('notes', 'N/A')}")

        # Worst Mistakes
        if 'worst_mistakes' in analysis:
            print("\n⚠️  Worst Mistakes:")
            for i, mistake in enumerate(analysis['worst_mistakes'], 1):
                print(f"\n  {i}. {mistake['trade_symbol']}")
                print(f"     Mistake: {mistake['mistake']}")
                print(f"     Cost:    ${mistake['cost_usd']:.2f}")
                print(f"     Lesson:  {mistake['lesson']}")

        # Systematic Errors
        if 'systematic_errors' in analysis:
            print("\n🔴 Systematic Errors:")
            for error in analysis['systematic_errors']:
                print(f"  • {error}")

        # Suggested Weights
        if report.suggested_weights:
            print("\n⚙️  SUGGESTED INDICATOR WEIGHTS")
            print("-" * 80)
            for indicator, weight in report.suggested_weights.items():
                print(f"  {indicator:20s} → {weight:.2f}")

        # Suggested Prompt Changes
        if report.suggested_prompt_changes:
            changes = report.suggested_prompt_changes
            if changes.get('add_rules'):
                print("\n✅ RULES TO ADD:")
                for rule in changes['add_rules']:
                    print(f"  • {rule}")

            if changes.get('remove_rules'):
                print("\n❌ RULES TO REMOVE:")
                for rule in changes['remove_rules']:
                    print(f"  • {rule}")

        # Status
        print()
        print("=" * 80)
        print(f"Status:     {report.status}")
        print(f"Report ID:  {report.id}")
        print("=" * 80)


if __name__ == "__main__":
    init_async_engine()
    asyncio.run(view_latest_report())
