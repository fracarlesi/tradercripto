"""Run daily analysis immediately - bypassing FastAPI."""
import asyncio
import sys
from datetime import date
from pathlib import Path

# Add backend to path
backend_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(backend_dir))

# Initialize database first
from database.connection import init_async_engine
init_async_engine()

from services.learning.daily_analysis_service import run_daily_analysis


async def main():
    """Run analysis for today."""
    print("=" * 70)
    print("📊 DAILY LEARNING ANALYSIS - Running for today")
    print("=" * 70)
    print()

    result = await run_daily_analysis(
        account_id=1,
        target_date=date.today()
    )

    print()
    print("=" * 70)
    print("RESULT:")
    print("=" * 70)
    print()

    if result.get('status') == 'success':
        print(f"✅ Analysis completed successfully!")
        print(f"   Report ID: {result['report_id']}")
        print(f"   Win Rate: {result['win_rate_pct']:.1f}%")
        print(f"   Profit Factor: {result['profit_factor']:.2f}")
        print()
        print(f"Summary:")
        print(f"{result['summary']}")
        print()
        print(f"To view full report, check database or use API:")
        print(f"   GET /api/daily-learning/reports/1/2025-11-20")
    elif result.get('status') == 'no_trades':
        print(f"⚠️  {result['message']}")
        print()
        print("Nota: Il sistema analizza solo i giorni con trade completati.")
    else:
        print(f"❌ Error: {result.get('message', 'Unknown error')}")

    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
