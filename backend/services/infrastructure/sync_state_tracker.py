"""Sync state tracker for monitoring synchronization health.

Tracks sync status, timing, and failures for health/readiness checks.
"""

from datetime import UTC, datetime
from typing import Any


class SyncStateTracker:
    """In-memory tracker for sync operations state.

    Tracks sync timing, status, and consecutive failures for health monitoring.
    Thread-safe for concurrent access from scheduler and API endpoints.
    """

    def __init__(self) -> None:
        """Initialize state tracker."""
        self._start_time = datetime.now(UTC)
        self._account_states: dict[int, dict[str, Any]] = {}

    def get_uptime_seconds(self) -> int:
        """Get application uptime in seconds.

        Returns:
            Seconds since application started
        """
        return int((datetime.now(UTC) - self._start_time).total_seconds())

    def record_sync_attempt(self, account_id: int, account_name: str, started_at: datetime) -> None:
        """Record start of sync operation.

        Args:
            account_id: Account identifier
            account_name: Account display name
            started_at: Timestamp when sync started
        """
        if account_id not in self._account_states:
            self._account_states[account_id] = {
                "account_name": account_name,
                "consecutive_failures": 0,
            }

        self._account_states[account_id]["last_attempt_time"] = started_at

    def record_sync_success(
        self, account_id: int, started_at: datetime, finished_at: datetime
    ) -> None:
        """Record successful sync operation.

        Args:
            account_id: Account identifier
            started_at: Timestamp when sync started
            finished_at: Timestamp when sync finished
        """
        if account_id not in self._account_states:
            return

        duration_ms = int((finished_at - started_at).total_seconds() * 1000)

        self._account_states[account_id].update(
            {
                "last_sync_time": finished_at,
                "last_sync_duration_ms": duration_ms,
                "sync_status": "success",
                "consecutive_failures": 0,
                "last_error": None,
            }
        )

    def record_sync_failure(
        self, account_id: int, started_at: datetime, finished_at: datetime, error: str
    ) -> None:
        """Record failed sync operation.

        Args:
            account_id: Account identifier
            started_at: Timestamp when sync started
            finished_at: Timestamp when sync finished
            error: Error message
        """
        if account_id not in self._account_states:
            return

        duration_ms = int((finished_at - started_at).total_seconds() * 1000)

        state = self._account_states[account_id]
        state["last_attempt_time"] = finished_at
        state["last_sync_duration_ms"] = duration_ms
        state["sync_status"] = "failed"
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        state["last_error"] = error

    def get_account_state(self, account_id: int) -> dict[str, Any] | None:
        """Get current sync state for an account.

        Args:
            account_id: Account identifier

        Returns:
            Dict with sync state or None if never synced:
            {
                "account_name": "DeepSeek",
                "last_sync_time": datetime,
                "last_sync_duration_ms": 1234,
                "sync_status": "success",
                "consecutive_failures": 0,
                "last_error": None
            }
        """
        return self._account_states.get(account_id)

    def get_all_account_states(self) -> dict[int, dict[str, Any]]:
        """Get sync states for all tracked accounts.

        Returns:
            Dict mapping account_id to sync state
        """
        return self._account_states.copy()

    def get_last_sync_time(self) -> datetime | None:
        """Get most recent successful sync time across all accounts.

        Returns:
            Datetime of most recent successful sync, or None if no syncs
        """
        successful_syncs = [
            state.get("last_sync_time")
            for state in self._account_states.values()
            if state.get("sync_status") == "success" and state.get("last_sync_time") is not None
        ]

        if not successful_syncs:
            return None

        return max(successful_syncs)

    def get_sync_health_status(self) -> str:
        """Get overall sync health status.

        Returns:
            "ok" (recent sync successful),
            "stale" (no sync > 2 minutes),
            "failing" (3+ consecutive failures)
        """
        if not self._account_states:
            return "failing"  # No accounts tracked yet

        # Check for failing accounts (3+ consecutive failures)
        failing_accounts = [
            state
            for state in self._account_states.values()
            if state.get("consecutive_failures", 0) >= 3
        ]

        if failing_accounts:
            return "failing"

        # Check for stale data (> 2 minutes since last successful sync)
        last_sync = self.get_last_sync_time()
        if last_sync is None:
            return "failing"  # Never synced

        seconds_since_sync = (datetime.now(UTC) - last_sync).total_seconds()

        if seconds_since_sync > 120:  # 2 minutes
            return "stale"

        return "ok"


# Global singleton instance
sync_state_tracker = SyncStateTracker()
