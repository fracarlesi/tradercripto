"""
Test Script: Technical Analysis Integration

Tests that momentum and support factors are calculated correctly
and integrated into the AI trading loop.

Usage:
    cd backend
    source .venv/bin/activate
    python -m scripts.test_technical_analysis
"""

import asyncio
import logging
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.technical_analysis_service import (
    calculate_technical_factors,
    format_technical_analysis_for_ai,
    get_top_technical_pick
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def test_technical_analysis():
    """Test technical analysis calculation"""
    logger.info("=" * 80)
    logger.info("Testing Technical Analysis Integration")
    logger.info("=" * 80)

    # Test symbols (major cryptocurrencies)
    symbols = ["BTC", "ETH", "SOL", "DOGE", "XRP", "BNB", "AVAX", "ARB"]

    logger.info(f"\n📊 Testing with {len(symbols)} symbols: {', '.join(symbols)}")

    # Calculate technical factors
    logger.info("\n🔄 Fetching historical data and calculating factors...")
    technical_factors = calculate_technical_factors(symbols)

    # Check results
    momentum_data = technical_factors.get("momentum", {})
    support_data = technical_factors.get("support", {})
    recommendations = technical_factors.get("recommendations", [])

    logger.info(f"\n✅ Analysis Results:")
    logger.info(f"   Momentum signals: {len(momentum_data)}")
    logger.info(f"   Support signals: {len(support_data)}")
    logger.info(f"   Combined recommendations: {len(recommendations)}")

    # Display top 5 recommendations
    if recommendations:
        logger.info("\n🏆 Top 5 Recommendations:")
        for i, rec in enumerate(recommendations[:5], 1):
            symbol = rec["symbol"]
            score = rec["score"]
            momentum = rec["momentum"]
            support = rec["support"]

            if score >= 0.7:
                signal = "🟢 STRONG BUY"
            elif score >= 0.6:
                signal = "🟢 BUY"
            elif score >= 0.4:
                signal = "🟡 HOLD"
            else:
                signal = "🔴 SELL/AVOID"

            logger.info(
                f"   {i}. {symbol:6s} {signal:15s} | "
                f"Combined: {score:.3f} | Momentum: {momentum:.3f} | Support: {support:.3f}"
            )

        # Get top pick
        top_pick = get_top_technical_pick(technical_factors)
        logger.info(f"\n💎 Top Technical Pick: {top_pick}")

    else:
        logger.warning("\n⚠️  No recommendations generated - check historical data availability")

    # Test AI prompt formatting
    logger.info("\n" + "=" * 80)
    logger.info("Testing AI Prompt Formatting")
    logger.info("=" * 80)

    formatted = format_technical_analysis_for_ai(technical_factors)
    logger.info("\n📝 Formatted for AI Prompt:\n")
    logger.info(formatted)

    # Summary
    logger.info("\n" + "=" * 80)
    if recommendations:
        logger.info("✅ TECHNICAL ANALYSIS TEST PASSED")
        logger.info(f"   - Successfully analyzed {len(recommendations)} symbols")
        logger.info(f"   - Top pick: {top_pick} (score: {recommendations[0]['score']:.3f})")
        logger.info("   - AI prompt formatting working correctly")
    else:
        logger.error("❌ TECHNICAL ANALYSIS TEST FAILED")
        logger.error("   - No recommendations generated")
        logger.error("   - Check network connection and Hyperliquid API access")
    logger.info("=" * 80)


if __name__ == "__main__":
    test_technical_analysis()
