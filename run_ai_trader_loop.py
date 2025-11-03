#!/usr/bin/env python3
"""
AI Trading Loop - Standalone Script
Runs AI trading cycles every 3 minutes independently of the main server.
"""
import time
import sys
from pathlib import Path

# Add backend to path
backend_path = Path(__file__).parent / "backend"
sys.path.insert(0, str(backend_path))

from services.auto_trader import place_ai_driven_crypto_order
import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/tmp/ai_trader_loop.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

CYCLE_INTERVAL_SECONDS = 180  # 3 minutes

def main():
    """Main loop for AI trading"""
    logger.info("🤖 AI Trading Loop Started")
    logger.info(f"Cycle interval: {CYCLE_INTERVAL_SECONDS} seconds (3 minutes)")

    cycle_count = 0

    try:
        while True:
            cycle_count += 1
            logger.info(f"\n{'='*60}")
            logger.info(f"🔄 Starting AI Trading Cycle #{cycle_count}")
            logger.info(f"{'='*60}")

            try:
                place_ai_driven_crypto_order(max_ratio=1.0)  # Allow AI to use up to 100% per trade
                logger.info(f"✅ Cycle #{cycle_count} completed successfully")
            except Exception as e:
                logger.error(f"❌ Cycle #{cycle_count} failed: {e}", exc_info=True)

            logger.info(f"⏳ Waiting {CYCLE_INTERVAL_SECONDS} seconds until next cycle...")
            time.sleep(CYCLE_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        logger.info("\n🛑 AI Trading Loop stopped by user")
    except Exception as e:
        logger.error(f"💥 Fatal error in AI Trading Loop: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    main()
