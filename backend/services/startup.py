"""Application startup initialization service"""

import asyncio
import logging
import threading

from services.auto_trader import AI_TRADE_JOB_ID, place_ai_driven_crypto_order
from services.scheduler import setup_market_tasks, start_scheduler, task_scheduler

logger = logging.getLogger(__name__)


def initialize_services() -> None:
    """Initialize all services (NOTE: WebSocket service now initialized in main.py lifespan)"""
    try:
        # WebSocket service is now initialized in main.py lifespan context manager
        # This ensures it runs on the main event loop and is properly managed by FastAPI

        # Start the scheduler
        start_scheduler()
        logger.info("Scheduler service started")

        # AI trading now handled by APScheduler in main.py (non-blocking)
        # This prevents the custom scheduler thread from blocking during long technical analysis
        # schedule_auto_trading(interval_seconds=180)
        # logger.info("Automatic cryptocurrency trading task started (3-minute interval)")

        # Add price cache cleanup task (every 2 minutes)
        from services.market_data.price_cache import clear_expired_prices

        task_scheduler.add_interval_task(
            task_func=clear_expired_prices,
            interval_seconds=120,  # Clean every 2 minutes
            task_id="price_cache_cleanup",
        )
        logger.info("Price cache cleanup task started (2-minute interval)")

        # NOTE: Hyperliquid account sync is now handled by APScheduler in main.py (periodic_sync_job every 30s)
        # Removed duplicate sync_all_active_accounts task to reduce API calls

        # Add portfolio snapshot capture task (every 1 minute)
        # Captures periodic snapshots of portfolio value from Hyperliquid for historical charts
        from services.portfolio_snapshot_service import capture_all_accounts_snapshots_async
        from database.connection import SessionLocal

        def capture_snapshots_wrapper():
            """Wrapper to run async snapshot capture in sync context"""
            db = SessionLocal()
            try:
                asyncio.run(capture_all_accounts_snapshots_async(db))
            finally:
                db.close()

        task_scheduler.add_interval_task(
            task_func=capture_snapshots_wrapper,
            interval_seconds=60,  # Capture every 1 minute
            task_id="portfolio_snapshot_capture",
        )
        logger.info("Portfolio snapshot capture task started (1-minute interval)")

        # DISABLED: Counterfactual learning (replaced by hourly market retrospective)
        # Old 24h feedback system - too slow for real-time trading
        # Hourly retrospective provides 1h feedback with dynamic corrections
        # from services.learning import calculate_counterfactuals_batch, run_self_analysis
        # logger.info("Counterfactual learning disabled (replaced by hourly retrospective)")

        # Add hourly market retrospective (REPLACES missed_opportunities_analyzer)
        # This provides REAL-TIME learning with dynamic weight adjustments
        from services.learning.hourly_retrospective import analyze_hourly_market_sync

        task_scheduler.add_interval_task(
            task_func=analyze_hourly_market_sync,
            interval_seconds=3600,  # Every 1 hour at XX:05
            task_id="hourly_market_retrospective",
        )
        logger.info("Hourly market retrospective task started (1-hour interval with auto-correction)")

        logger.info("All services initialized successfully")

    except Exception as e:
        logger.error(f"Service initialization failed: {e}", exc_info=True)
        raise


def shutdown_services() -> None:
    """Shut down all services (NOTE: WebSocket shutdown now in main.py lifespan)"""
    try:
        # WebSocket service shutdown is now handled in main.py lifespan context manager

        # Stop scheduler
        from services.scheduler import stop_scheduler

        stop_scheduler()
        logger.info("All services have been shut down")

    except Exception as e:
        logger.error(f"Failed to shut down services: {e}", exc_info=True)


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
            logger.error(f"Error during initial AI-driven trading execution: {e}", exc_info=True)

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
