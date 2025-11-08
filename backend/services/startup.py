"""Application startup initialization service"""

import logging
import threading

from services.auto_trader import AI_TRADE_JOB_ID, place_ai_driven_crypto_order
from services.scheduler import setup_market_tasks, start_scheduler, task_scheduler

logger = logging.getLogger(__name__)


# Learning system async job wrappers (must be defined at module level for APScheduler)
async def calculate_counterfactuals_wrapper():
    """
    Async wrapper for counterfactual calculation batch job.
    Calculates counterfactual P&L for decision snapshots older than 24h.
    """
    try:
        from services.learning import calculate_counterfactuals_batch

        processed = await calculate_counterfactuals_batch(limit=100)
        if processed > 0:
            logger.info(f"✅ Calculated counterfactuals for {processed} snapshots")
    except Exception as e:
        logger.error(f"Counterfactual calculation failed: {e}", exc_info=True)


async def auto_self_analysis_wrapper():
    """
    Async wrapper for automatic self-analysis.
    Runs DeepSeek self-analysis every 3h if enough data available (50+ snapshots).
    """
    try:
        from database.connection import async_session_factory
        from database.models import DecisionSnapshot, Account
        from services.learning import run_self_analysis
        from sqlalchemy import select, and_

        async with async_session_factory() as db:
            # Get all active AI accounts
            stmt = select(Account).where(
                and_(Account.is_active == True, Account.account_type == "AI")
            )
            result = await db.execute(stmt)
            accounts = result.scalars().all()

            for account in accounts:
                # Count snapshots with counterfactuals
                count_stmt = select(DecisionSnapshot).where(
                    and_(
                        DecisionSnapshot.account_id == account.id,
                        DecisionSnapshot.regret.isnot(None),
                    )
                )
                count_result = await db.execute(count_stmt)
                total_snapshots = len(count_result.scalars().all())

                # Run analysis if we have enough data (50+ snapshots)
                if total_snapshots >= 50:
                    logger.info(
                        f"Running auto self-analysis for account {account.id} "
                        f"({total_snapshots} snapshots)"
                    )

                    analysis = await run_self_analysis(
                        account_id=account.id, limit=100, min_regret=None
                    )

                    # Log summary
                    if "error" not in analysis:
                        logger.info(
                            f"✅ Self-analysis complete for account {account.id}: "
                            f"Regret=${analysis.get('total_regret_usd', 0):.2f}, "
                            f"Accuracy={analysis.get('accuracy_rate', 0):.1%}"
                        )

                        # Log suggested weights (not auto-applied for safety)
                        suggested_weights = analysis.get("suggested_weights", {})
                        logger.info(
                            f"💡 Suggested weights for account {account.id}: {suggested_weights}"
                        )

    except Exception as e:
        logger.error(f"Auto self-analysis failed: {e}", exc_info=True)


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

        # Add portfolio snapshot capture task (every 5 minutes)
        # Captures periodic snapshots of portfolio value from Hyperliquid for historical charts
        from services.portfolio_snapshot_service import capture_all_accounts_snapshots_async
        from database.connection import SessionLocal
        import asyncio

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

        # Schedule counterfactuals calculation every hour using APScheduler
        from services.infrastructure.scheduler import scheduler_service

        scheduler_service.add_sync_job(
            job_func=calculate_counterfactuals_wrapper,
            interval_seconds=3600,  # Every 1 hour
            job_id="counterfactual_calculation",
        )
        logger.info("Counterfactual calculation task started (1-hour interval)")

        # Schedule self-analysis every 3 hours
        scheduler_service.add_sync_job(
            job_func=auto_self_analysis_wrapper,
            interval_seconds=10800,  # Every 3 hours (3 * 3600)
            job_id="auto_self_analysis",
        )
        logger.info("Auto self-analysis task started (3-hour interval)")

        logger.info("All services initialized successfully")

    except Exception as e:
        logger.error(f"Service initialization failed: {e}", exc_info=True)
        raise


def shutdown_services() -> None:
    """Shut down all services"""
    try:
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
