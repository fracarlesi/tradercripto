"""Application startup initialization service"""

import logging
import threading

from services.auto_trader import AI_TRADE_JOB_ID, place_ai_driven_crypto_order
from services.scheduler import setup_market_tasks, start_scheduler, task_scheduler

logger = logging.getLogger(__name__)


def initialize_services() -> None:
    """Initialize all services"""
    try:
        # Start the scheduler
        start_scheduler()
        logger.info("Scheduler service started")

        # Set up market-related scheduled tasks
        setup_market_tasks()
        logger.info("Market scheduled tasks have been set up")

        # Start automatic cryptocurrency trading simulation task (3-minute interval)
        schedule_auto_trading(interval_seconds=180)
        logger.info("Automatic cryptocurrency trading task started (3-minute interval)")

        # Add price cache cleanup task (every 2 minutes)
        from services.market_data.price_cache import clear_expired_prices

        task_scheduler.add_interval_task(
            task_func=clear_expired_prices,
            interval_seconds=120,  # Clean every 2 minutes
            task_id="price_cache_cleanup",
        )
        logger.info("Price cache cleanup task started (2-minute interval)")

        # Add Hyperliquid account sync task (every 60 seconds)
        # This ensures local database stays in sync with on-chain state
        from services.trading.hyperliquid_sync_service import sync_all_active_accounts

        task_scheduler.add_interval_task(
            task_func=sync_all_active_accounts,
            interval_seconds=60,  # Sync every minute
            task_id="hyperliquid_account_sync",
        )
        logger.info("Hyperliquid account sync task started (1-minute interval)")

        logger.info("All services initialized successfully")

    except Exception as e:
        logger.error(f"Service initialization failed: {e}")
        raise


def shutdown_services() -> None:
    """Shut down all services"""
    try:
        from services.scheduler import stop_scheduler

        stop_scheduler()
        logger.info("All services have been shut down")

    except Exception as e:
        logger.error(f"Failed to shut down services: {e}")


async def startup_event() -> None:
    """FastAPI application startup event"""
    initialize_services()


async def shutdown_event() -> None:
    """FastAPI application shutdown event"""
    shutdown_services()


def schedule_auto_trading(interval_seconds: int = 300, max_ratio: float = 0.2) -> None:
    """Schedule AI-driven automatic trading tasks (real trading only)

    Args:
        interval_seconds: Interval between trading attempts
        max_ratio: Maximum portion of portfolio to use per trade
    """

    def execute_trade():
        try:
            place_ai_driven_crypto_order(max_ratio)
            logger.info("Initial AI-driven trading execution completed")
        except Exception as e:
            logger.error(f"Error during initial AI-driven trading execution: {e}")

    logger.info("Scheduling AI-driven crypto trading (real trading on Hyperliquid)")

    # Schedule the recurring AI-driven task
    task_scheduler.add_interval_task(
        task_func=place_ai_driven_crypto_order,
        interval_seconds=interval_seconds,
        task_id=AI_TRADE_JOB_ID,
        max_ratio=max_ratio,
    )

    # FIX: Removed initial trade thread to prevent startup blocking
    # The thread was causing event loop deadlock during application startup
    # First trade will execute on the first scheduled interval instead
    # initial_trade = threading.Thread(target=execute_trade, daemon=True)
    # initial_trade.start()
