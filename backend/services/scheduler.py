"""Account snapshot scheduler for WebSocket connections."""

import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Store active snapshot jobs by account_id
_active_jobs: dict[int, dict[str, Any]] = {}

# Task scheduler state
_scheduler_running = False
_scheduled_tasks: dict[str, dict[str, Any]] = {}


def add_account_snapshot_job(account_id: int, interval_seconds: int = 10) -> None:
    """Add periodic snapshot job for account.

    Args:
        account_id: Account ID to snapshot
        interval_seconds: Snapshot interval in seconds (default: 10)
    """
    if account_id in _active_jobs:
        logger.debug(f"Account {account_id} snapshot job already exists")
        return

    _active_jobs[account_id] = {
        "account_id": account_id,
        "interval_seconds": interval_seconds,
        "enabled": True,
    }

    logger.info(
        f"Added snapshot job for account {account_id} "
        f"with interval {interval_seconds}s"
    )


def remove_account_snapshot_job(account_id: int) -> None:
    """Remove periodic snapshot job for account.

    Args:
        account_id: Account ID to stop snapshotting
    """
    if account_id in _active_jobs:
        del _active_jobs[account_id]
        logger.info(f"Removed snapshot job for account {account_id}")
    else:
        logger.debug(f"No snapshot job found for account {account_id}")


def get_active_jobs() -> dict[int, dict[str, Any]]:
    """Get all active snapshot jobs.

    Returns:
        Dictionary mapping account_id to job info
    """
    return _active_jobs.copy()


# Task scheduler implementation
class TaskScheduler:
    """Simple interval-based task scheduler."""

    def __init__(self):
        self.tasks: dict[str, dict[str, Any]] = {}
        self.running = False
        self.thread = None

    def add_interval_task(
        self,
        task_func: Callable,
        interval_seconds: int,
        task_id: str,
        **kwargs
    ) -> None:
        """Add a task to run at regular intervals.

        Args:
            task_func: Function to execute
            interval_seconds: Interval in seconds
            task_id: Unique task identifier
            **kwargs: Additional arguments to pass to task_func
        """
        self.tasks[task_id] = {
            "func": task_func,
            "interval": interval_seconds,
            "kwargs": kwargs,
            "last_run": 0,
        }
        logger.info(f"Added task {task_id} with {interval_seconds}s interval")

    def remove_task(self, task_id: str) -> None:
        """Remove a scheduled task."""
        if task_id in self.tasks:
            del self.tasks[task_id]
            logger.info(f"Removed task {task_id}")

    def _run(self) -> None:
        """Run the scheduler loop."""
        while self.running:
            current_time = time.time()
            for task_id, task_info in list(self.tasks.items()):
                if current_time - task_info["last_run"] >= task_info["interval"]:
                    try:
                        task_info["func"](**task_info["kwargs"])
                        task_info["last_run"] = current_time
                    except Exception as e:
                        logger.error(f"Error executing task {task_id}: {e}", exc_info=True)
            time.sleep(1)

    def start(self) -> None:
        """Start the scheduler."""
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
            logger.info("Task scheduler started")

    def stop(self) -> None:
        """Stop the scheduler."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        logger.info("Task scheduler stopped")


# Global task scheduler instance
task_scheduler = TaskScheduler()


def start_scheduler() -> None:
    """Start the global task scheduler."""
    global _scheduler_running
    if not _scheduler_running:
        task_scheduler.start()
        _scheduler_running = True
        logger.info("Global scheduler started")


def stop_scheduler() -> None:
    """Stop the global task scheduler."""
    global _scheduler_running
    if _scheduler_running:
        task_scheduler.stop()
        _scheduler_running = False
        logger.info("Global scheduler stopped")


def setup_market_tasks() -> None:
    """Set up market-related scheduled tasks (placeholder)."""
    logger.info("Market tasks setup (placeholder)")
    # TODO: Implement market data refresh tasks
