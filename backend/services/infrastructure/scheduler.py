"""Scheduler service for background periodic tasks.

Wraps APScheduler AsyncIOScheduler for managing background jobs.
"""

from collections.abc import Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from config.logging import get_logger

logger = get_logger(__name__)


class SchedulerService:
    """Wrapper around AsyncIOScheduler for background tasks.

    Provides simplified interface for scheduling periodic async jobs.
    """

    def __init__(self) -> None:
        """Initialize scheduler service."""
        self._scheduler: AsyncIOScheduler | None = None

    def start(self) -> None:
        """Start the scheduler.

        Creates and starts AsyncIOScheduler instance.
        Should be called during application startup.
        """
        if self._scheduler is not None:
            logger.warning("Scheduler already started")
            return

        self._scheduler = AsyncIOScheduler()
        self._scheduler.start()
        logger.info("Scheduler service started")

    def stop(self) -> None:
        """Stop the scheduler gracefully.

        Waits for running jobs to complete before shutdown.
        Should be called during application shutdown.
        """
        if self._scheduler is None:
            logger.warning("Scheduler not started")
            return

        self._scheduler.shutdown(wait=True)
        self._scheduler = None
        logger.info("Scheduler service stopped")

    def add_sync_job(
        self, job_func: Callable, interval_seconds: int, job_id: str = "sync_job", start_delay_seconds: int = 0
    ) -> None:
        """Add periodic sync job to scheduler.

        Args:
            job_func: Async callable to execute periodically
            interval_seconds: Interval between job executions
            job_id: Unique identifier for the job (default: "sync_job")
            start_delay_seconds: Delay before first execution (default: 0)

        Raises:
            RuntimeError: If scheduler not started
        """
        if self._scheduler is None:
            raise RuntimeError("Scheduler not started - call start() first")

        from datetime import datetime, timedelta

        # Calculate start time with delay to stagger job execution
        start_date = datetime.now() + timedelta(seconds=start_delay_seconds) if start_delay_seconds > 0 else None

        trigger = IntervalTrigger(seconds=interval_seconds, start_date=start_date)

        self._scheduler.add_job(
            job_func,
            trigger=trigger,
            id=job_id,
            replace_existing=True,
            max_instances=1,  # Prevent concurrent execution of same job
        )

        logger.info(
            "Sync job added to scheduler",
            extra={
                "context": {
                    "job_id": job_id,
                    "interval_seconds": interval_seconds,
                    "start_delay_seconds": start_delay_seconds,
                }
            },
        )

    def add_cron_job(
        self, job_func: Callable, hour: int = 0, minute: int = 0, job_id: str = "cron_job"
    ) -> None:
        """Add daily cron job to scheduler (T101).

        Schedules a job to run daily at specified time (default: midnight).
        Useful for daily maintenance tasks like resetting counters, archiving data, etc.

        Args:
            job_func: Async callable to execute daily
            hour: Hour to run job (0-23, default: 0 = midnight)
            minute: Minute to run job (0-59, default: 0)
            job_id: Unique identifier for the job (default: "cron_job")

        Raises:
            RuntimeError: If scheduler not started

        Example:
            >>> scheduler_service.add_cron_job(
            ...     job_func=reset_ai_usage_daily,
            ...     hour=0,
            ...     minute=0,
            ...     job_id="ai_usage_reset"
            ... )
        """
        if self._scheduler is None:
            raise RuntimeError("Scheduler not started - call start() first")

        trigger = CronTrigger(hour=hour, minute=minute)

        self._scheduler.add_job(
            job_func,
            trigger=trigger,
            id=job_id,
            replace_existing=True,
            max_instances=1,  # Prevent concurrent execution
        )

        logger.info(
            "Cron job added to scheduler",
            extra={
                "context": {
                    "job_id": job_id,
                    "hour": hour,
                    "minute": minute,
                }
            },
        )


# Global singleton instance
scheduler_service = SchedulerService()
