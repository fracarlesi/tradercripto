#!/usr/bin/env python3
"""
Manual Integration Test Script - Complete Orchestrator + DeepSeek Pipeline

This script tests the complete new architecture end-to-end:
1. Orchestrator builds market data snapshot (all 142 symbols)
2. DeepSeek client analyzes JSON and makes decision
3. Results are displayed for verification

Usage:
    cd backend
    python scripts/testing/test_full_orchestrator_pipeline.py

Options:
    --skip-prophet: Disable Prophet forecasting (faster test)
    --skip-deepseek: Only test orchestrator (no AI API call)
    --account-id: Account ID to use (default: 1)

Requirements:
    - Valid Hyperliquid connection
    - Valid DeepSeek API key in account settings
    - Database with account data
"""

import asyncio
import argparse
import json
import logging
import sys
import time
from pathlib import Path

# Add backend to path
backend_path = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(backend_path))

from database.connection import async_session_factory
from database.models import Account
from services.ai.deepseek_client import get_trading_decision_from_snapshot
from services.orchestrator.market_data_orchestrator import build_market_data_snapshot
from services.orchestrator.schemas import validate_snapshot
from sqlalchemy import select

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def test_orchestrator_only(account_id: int, enable_prophet: bool = False):
    """
    Test orchestrator data fetching (no DeepSeek API call).

    Args:
        account_id: Account ID to use
        enable_prophet: Enable Prophet forecasting (slower)

    Returns:
        Market snapshot or None if failed
    """
    logger.info("=" * 80)
    logger.info("PHASE 1: ORCHESTRATOR DATA FETCHING")
    logger.info("=" * 80)

    start = time.time()

    try:
        snapshot = await build_market_data_snapshot(
            account_id=account_id,
            enable_prophet=enable_prophet,
            prophet_mode="lite" if enable_prophet else None,
        )

        duration = time.time() - start

        # Validate
        validate_snapshot(snapshot)

        # Print summary
        logger.info("=" * 80)
        logger.info("✅ ORCHESTRATOR SUCCESS")
        logger.info("=" * 80)
        logger.info(f"Duration: {duration:.1f}s")
        logger.info(f"Symbols analyzed: {snapshot['metadata']['symbols_analyzed']}")
        logger.info(f"Cache hit rate: {snapshot['metadata']['cache_hit_rate']:.1%}")

        # Prophet stats
        if enable_prophet:
            prophet_count = sum(1 for s in snapshot["symbols"] if s["prophet_forecast"] is not None)
            logger.info(f"Prophet forecasts: {prophet_count} symbols")

        # Top 5 signals
        top_symbols = sorted(
            snapshot["symbols"],
            key=lambda s: s["technical_analysis"]["score"],
            reverse=True,
        )[:5]

        logger.info("\n📊 TOP 5 TECHNICAL SIGNALS:")
        for i, symbol_data in enumerate(top_symbols, 1):
            symbol = symbol_data["symbol"]
            ta = symbol_data["technical_analysis"]
            price = symbol_data["price"]
            logger.info(
                f"  {i}. {symbol}: ${price:,.2f} - "
                f"Score {ta['score']:.3f} ({ta['signal']}) - "
                f"Pivot: {symbol_data['pivot_points']['current_zone']}"
            )

        # Portfolio
        portfolio = snapshot["portfolio"]
        logger.info(f"\n💼 PORTFOLIO:")
        logger.info(f"  Total: ${portfolio['total_assets']:.2f}")
        logger.info(f"  Cash: ${portfolio['available_cash']:.2f}")
        logger.info(f"  Positions: {len(portfolio['positions'])}")

        for pos in portfolio["positions"]:
            logger.info(
                f"    • {pos['symbol']} {pos['side']}: "
                f"${pos['market_value']:.2f} "
                f"(PNL: {pos['unrealized_pnl_pct']:+.2f}%)"
            )

        # Global indicators
        sentiment = snapshot["global_indicators"]["sentiment"]
        logger.info(f"\n🌐 GLOBAL INDICATORS:")
        logger.info(f"  Sentiment: {sentiment['label']} ({sentiment['value']})")
        logger.info(f"  Whale alerts: {len(snapshot['global_indicators']['whale_alerts'])}")
        logger.info(f"  News: {len(snapshot['global_indicators']['news'])}")

        return snapshot

    except Exception as e:
        logger.error(f"❌ ORCHESTRATOR FAILED: {e}", exc_info=True)
        return None


async def test_deepseek_decision(account: Account, snapshot: dict):
    """
    Test DeepSeek AI decision using snapshot.

    Args:
        account: Account with API config
        snapshot: Market data snapshot

    Returns:
        Trading decision or None if failed
    """
    logger.info("=" * 80)
    logger.info("PHASE 2: DEEPSEEK AI DECISION")
    logger.info("=" * 80)

    start = time.time()

    try:
        decision = await get_trading_decision_from_snapshot(account, snapshot)

        duration = time.time() - start

        if not decision:
            logger.error("❌ DEEPSEEK FAILED: No decision returned")
            return None

        # Print decision
        logger.info("=" * 80)
        logger.info("✅ DEEPSEEK SUCCESS")
        logger.info("=" * 80)
        logger.info(f"Duration: {duration:.1f}s")
        logger.info(f"\n🤖 TRADING DECISION:")
        logger.info(f"  Operation: {decision['operation'].upper()}")
        logger.info(f"  Symbol: {decision.get('symbol', 'N/A')}")
        logger.info(f"  Portion: {decision.get('target_portion_of_balance', 0):.1%}")
        logger.info(f"  Leverage: {decision.get('leverage', 1)}x")
        logger.info(f"\n💭 REASONING:")
        logger.info(f"  {decision.get('reason', 'No reason provided')}")

        # Detailed analysis (if present)
        if "analysis" in decision:
            analysis = decision["analysis"]
            logger.info(f"\n📊 DETAILED ANALYSIS:")
            logger.info(f"  Confidence: {analysis.get('confidence', 0):.1%}")

            if "indicators_used" in analysis:
                logger.info(f"  Indicators used:")
                for indicator in analysis["indicators_used"]:
                    logger.info(f"    • {indicator}")

            if "alternatives_considered" in analysis:
                logger.info(f"  Alternatives considered:")
                for alt in analysis["alternatives_considered"][:3]:
                    logger.info(
                        f"    • {alt['symbol']}: score {alt.get('weighted_score', 0):.3f} - {alt.get('reason', 'N/A')}"
                    )

        return decision

    except Exception as e:
        logger.error(f"❌ DEEPSEEK FAILED: {e}", exc_info=True)
        return None


async def run_full_test(
    account_id: int = 1,
    enable_prophet: bool = True,
    skip_deepseek: bool = False,
):
    """
    Run complete integration test.

    Args:
        account_id: Account ID to test
        enable_prophet: Enable Prophet forecasting
        skip_deepseek: Skip DeepSeek API call (orchestrator only)

    Returns:
        True if all tests passed, False otherwise
    """
    logger.info("=" * 80)
    logger.info("FULL INTEGRATION TEST: Orchestrator + DeepSeek")
    logger.info("=" * 80)
    logger.info(f"Account ID: {account_id}")
    logger.info(f"Prophet: {'ENABLED' if enable_prophet else 'DISABLED'}")
    logger.info(f"DeepSeek: {'SKIP' if skip_deepseek else 'ENABLED'}")
    logger.info("=" * 80)

    # Get account
    async with async_session_factory() as db:
        stmt = select(Account).where(Account.id == account_id)
        result = await db.execute(stmt)
        account = result.scalar_one_or_none()

        if not account:
            logger.error(f"❌ Account {account_id} not found")
            return False

        logger.info(f"✅ Account: {account.name} (model: {account.model})")

    # PHASE 1: Test orchestrator
    snapshot = await test_orchestrator_only(account_id, enable_prophet)

    if not snapshot:
        logger.error("❌ TEST FAILED: Orchestrator")
        return False

    # PHASE 2: Test DeepSeek (optional)
    if not skip_deepseek:
        decision = await test_deepseek_decision(account, snapshot)

        if not decision:
            logger.error("❌ TEST FAILED: DeepSeek")
            return False
    else:
        logger.info("\n⏭️  Skipping DeepSeek test (--skip-deepseek)")

    # SUCCESS
    logger.info("=" * 80)
    logger.info("✅ ALL TESTS PASSED")
    logger.info("=" * 80)

    return True


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Test orchestrator + DeepSeek pipeline")
    parser.add_argument(
        "--account-id",
        type=int,
        default=1,
        help="Account ID to test (default: 1)",
    )
    parser.add_argument(
        "--skip-prophet",
        action="store_true",
        help="Disable Prophet forecasting (faster)",
    )
    parser.add_argument(
        "--skip-deepseek",
        action="store_true",
        help="Skip DeepSeek API call (orchestrator only)",
    )

    args = parser.parse_args()

    # Run test
    success = await run_full_test(
        account_id=args.account_id,
        enable_prophet=not args.skip_prophet,
        skip_deepseek=args.skip_deepseek,
    )

    # Exit code
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
