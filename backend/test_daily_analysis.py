"""Quick test script for daily learning analysis."""
import asyncio
from datetime import date
from services.learning.daily_analysis_service import run_daily_analysis


async def main():
    """Run analysis for today."""
    print("=" * 60)
    print("🧪 TEST - Daily Learning Analysis")
    print("=" * 60)

    result = await run_daily_analysis(
        account_id=1,
        target_date=date.today()
    )

    print("\n" + "=" * 60)
    print("📊 RESULT:")
    print("=" * 60)

    if result.get('status') == 'success':
        print(f"✅ Analysis completed!")
        print(f"   Report ID: {result['report_id']}")
        print(f"   Win Rate: {result['win_rate_pct']:.1f}%")
        print(f"   Profit Factor: {result['profit_factor']:.2f}")
        print(f"\n   Summary: {result['summary']}")
    elif result.get('status') == 'no_trades':
        print(f"⚠️  {result['message']}")
    else:
        print(f"❌ Error: {result.get('message', 'Unknown error')}")

    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
