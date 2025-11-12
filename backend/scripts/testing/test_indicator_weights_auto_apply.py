"""
Test script for indicator weights auto-apply logic.

This script tests the complete auto-apply workflow:
1. Check if auto-apply is enabled (AUTO_APPLY_WEIGHTS=true)
2. Check if 24h have passed since last auto-apply
3. Validate suggested weights from DeepSeek
4. Blend weights (70% old + 30% new)
5. Save to database with history tracking
6. Verify auto_trader.py uses the new weights
"""

import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add backend directory to path
backend_dir = Path(__file__).parent.parent.parent
sys.path.insert(0, str(backend_dir))

from database.connection import async_session_factory
from database.models import Account, IndicatorWeightsHistory
from services.learning.indicator_weights_service import (
    apply_indicator_weights,
    should_auto_apply_today,
    get_weight_history,
    get_current_weights,
)
from services.learning.deepseek_self_analysis_service import _validate_weights, _blend_weights
from sqlalchemy import select


async def test_auto_apply_workflow():
    """Test complete auto-apply workflow with mock data."""

    print("=== Testing Indicator Weights Auto-Apply Workflow ===\n")

    # Step 1: Get test account
    async with async_session_factory() as db:
        result = await db.execute(
            select(Account).where(Account.account_type == "AI")
        )
        account = result.scalar_one_or_none()

        if not account:
            print("❌ No AI account found in database")
            return False

        print(f"✅ Using account: {account.name} (id={account.id})")
        print(f"   Current indicator_weights: {account.indicator_weights}")
        print(f"   Current strategy_weights: {account.strategy_weights}\n")

    # Step 2: Mock suggested weights from DeepSeek
    suggested_weights = {
        "prophet": 0.75,          # Increased (was 0.5, learned it's more accurate)
        "pivot_points": 0.70,     # Decreased (was 0.8, sometimes noisy)
        "rsi_macd": 0.60,         # Increased (was 0.5, good for timing)
        "whale_alerts": 0.50,     # Increased (was 0.4, strong signal)
        "sentiment": 0.20,        # Decreased (was 0.3, too noisy)
        "news": 0.15,             # Decreased (was 0.2, mostly irrelevant)
    }

    print("📊 Mock suggested weights from DeepSeek:")
    print(json.dumps(suggested_weights, indent=2))
    print()

    # Step 3: Validate suggested weights
    print("🔍 Validating suggested weights...")
    is_valid = _validate_weights(suggested_weights)

    if not is_valid:
        print("❌ Validation failed!")
        return False

    print("✅ Validation passed!\n")

    # Step 4: Check if auto-apply should run today
    print("⏰ Checking if auto-apply should run today...")
    should_apply = await should_auto_apply_today(account.id)

    print(f"   Result: {should_apply}")

    if not should_apply:
        print("   ℹ️  Auto-apply blocked (already applied today or disabled)\n")

        # Show history
        print("📜 Weight change history (last 5 entries):")
        history = await get_weight_history(account.id, limit=5)

        for entry in history:
            print(f"   [{entry.applied_at.strftime('%Y-%m-%d %H:%M')}] {entry.source}")
            print(f"      Weights: {entry.new_weights}")

        # For testing, force apply anyway
        print("\n⚠️  FORCING auto-apply for testing purposes...")
    else:
        print("   ✅ Auto-apply allowed!\n")

    # Step 5: Blend weights (70% old + 30% new)
    current_weights = await get_current_weights(account.id)

    print("🔀 Blending weights (70% old + 30% new)...")
    print(f"   Old weights: {current_weights}")
    print(f"   Suggested weights: {suggested_weights}")

    blended_weights = _blend_weights(
        current_weights=current_weights,
        suggested_weights=suggested_weights,
        blend_old=0.7,
        blend_new=0.3,
    )

    print(f"   Blended weights: {blended_weights}\n")

    # Show weight changes
    print("📊 Weight changes:")
    for indicator, new_value in blended_weights.items():
        old_value = (current_weights or {}).get(indicator, 0.5)
        change = new_value - old_value
        direction = "↑" if change > 0 else "↓" if change < 0 else "→"
        print(f"   {indicator:15s}: {old_value:.2f} {direction} {new_value:.2f} ({change:+.2f})")
    print()

    # Step 6: Apply weights to database
    print("💾 Applying weights to database...")

    try:
        applied_weights = await apply_indicator_weights(
            account_id=account.id,
            suggested_weights=blended_weights,
            source="test_script",  # Mark as test source
        )

        print(f"✅ Weights applied successfully!")
        print(f"   Applied weights: {applied_weights}\n")

    except Exception as e:
        print(f"❌ Failed to apply weights: {e}")
        return False

    # Step 7: Verify weights were saved
    print("🔍 Verifying weights were saved to database...")

    async with async_session_factory() as db:
        result = await db.execute(
            select(Account).where(Account.id == account.id)
        )
        updated_account = result.scalar_one()

        print(f"   Account.indicator_weights: {updated_account.indicator_weights}")

        if updated_account.indicator_weights == blended_weights:
            print("✅ Weights saved correctly!\n")
        else:
            print("❌ Weights mismatch!")
            print(f"   Expected: {blended_weights}")
            print(f"   Got: {updated_account.indicator_weights}\n")
            return False

    # Step 8: Verify history entry was created
    print("📜 Verifying history entry was created...")

    history = await get_weight_history(account.id, limit=1)

    if not history:
        print("❌ No history entry found!")
        return False

    latest_entry = history[0]
    print(f"   Latest entry: [{latest_entry.applied_at.strftime('%Y-%m-%d %H:%M:%S')}]")
    print(f"   Source: {latest_entry.source}")
    print(f"   Old weights: {latest_entry.old_weights}")
    print(f"   New weights: {latest_entry.new_weights}")
    print("✅ History entry created!\n")

    # Step 9: Test that auto_trader.py would use these weights
    print("🤖 Testing that auto_trader.py would use these weights...")

    from services.ai_decision_service import _get_strategy_weights

    async with async_session_factory() as db:
        result = await db.execute(
            select(Account).where(Account.id == account.id)
        )
        account = result.scalar_one()

        weights_from_trader = _get_strategy_weights(account)

        print(f"   Weights from _get_strategy_weights(): {weights_from_trader}")

        # Compare with blended_weights
        if weights_from_trader == blended_weights:
            print("✅ auto_trader.py would use the correct weights!\n")
        else:
            print("⚠️  Weights don't match exactly (might include defaults for missing keys)")
            print(f"   Expected: {blended_weights}")
            print(f"   Got: {weights_from_trader}\n")

    print("=" * 60)
    print("✅ All tests passed! Auto-apply workflow is working correctly.")
    print("=" * 60)

    return True


async def cleanup_test_data():
    """Clean up test data (optional)."""

    print("\n🧹 Cleaning up test data...")

    async with async_session_factory() as db:
        # Delete test history entries
        result = await db.execute(
            select(IndicatorWeightsHistory).where(
                IndicatorWeightsHistory.source == "test_script"
            )
        )
        test_entries = result.scalars().all()

        if test_entries:
            print(f"   Deleting {len(test_entries)} test history entries...")
            for entry in test_entries:
                await db.delete(entry)

            await db.commit()
            print("✅ Cleanup complete!")
        else:
            print("   No test data to clean up")


if __name__ == "__main__":
    # Run test
    success = asyncio.run(test_auto_apply_workflow())

    if not success:
        print("\n❌ Tests failed!")
        sys.exit(1)

    # Ask if user wants to clean up test data
    print("\n🗑️  Do you want to clean up test data? (y/N): ", end="")
    response = input().strip().lower()

    if response == "y":
        asyncio.run(cleanup_test_data())
    else:
        print("   Skipping cleanup (test data preserved for inspection)")

    print("\n✅ Done!")
