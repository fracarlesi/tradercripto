"""Application startup initialization service"""

import asyncio
import logging

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

        # NOTE: AI trading job is now scheduled in main.py with APScheduler (ai_crypto_trade)
        # Removed duplicate task_scheduler registration to prevent double execution

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
            interval_seconds=300,  # Capture every 5 minutes
            task_id="portfolio_snapshot_capture",
        )
        logger.info("Portfolio snapshot capture task started (5-minute interval)")

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
