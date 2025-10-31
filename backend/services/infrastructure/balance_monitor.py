"""Balance mismatch monitoring service.

Periodically compares local database balance with Hyperliquid balance
and sends alerts if discrepancies persist beyond threshold.
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from config.logging import get_logger
from database.connection import get_async_session
from repositories.account_repo import AccountRepository
from services.infrastructure.alerting import (
    AlertLevel,
    alerting_service,
)
from services.trading.hyperliquid_trading_service import (
    hyperliquid_trading_service,
)
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)


class BalanceMonitor:
    """Monitor for balance mismatches between local DB and Hyperliquid (T135).

    Tracks balance discrepancies and sends alerts when:
    - Difference exceeds 1% threshold
    - Mismatch persists for >5 minutes
    """

    def __init__(
        self,
        threshold_percent: float = 1.0,
        persistence_seconds: int = 300,
    ) -> None:
        """Initialize balance monitor.

        Args:
            threshold_percent: Alert threshold as percentage (default: 1.0%)
            persistence_seconds: Alert after mismatch persists this long (default: 300s = 5min)
        """
        self.threshold_percent = threshold_percent
        self.persistence_seconds = persistence_seconds

        # Track mismatches: {account_id: {"first_seen": datetime, "last_diff_percent": float}}
        self._mismatches: dict[int, dict[str, any]] = {}

        logger.info(
            f"BalanceMonitor initialized (threshold: {threshold_percent}%, "
            f"persistence: {persistence_seconds}s)"
        )

    def _calculate_difference_percent(
        self, local_balance: Decimal, remote_balance: Decimal
    ) -> float:
        """Calculate percentage difference between balances.

        Args:
            local_balance: Balance from local database
            remote_balance: Balance from Hyperliquid

        Returns:
            Absolute percentage difference
        """
        if remote_balance == 0:
            # Avoid division by zero
            return 100.0 if local_balance != 0 else 0.0

        diff = abs(local_balance - remote_balance)
        percent = (diff / remote_balance) * 100
        return float(percent)

    async def check_account_balance(self, db: AsyncSession, account_id: int) -> dict[str, any]:
        """Check single account for balance mismatch.

        Args:
            db: Async database session
            account_id: Account ID to check

        Returns:
            Dict with check results:
            {
                "account_id": 1,
                "local_balance": Decimal("10000.00"),
                "remote_balance": Decimal("10050.00"),
                "diff_percent": 0.5,
                "exceeds_threshold": False,
                "mismatch_duration": 0,
                "alert_sent": False
            }
        """
        try:
            # Get local balance
            account = await AccountRepository.get_by_id(db=db, account_id=account_id)
            if not account:
                logger.warning(f"Account {account_id} not found in database")
                return {"error": "Account not found"}

            local_balance = account.current_cash + account.frozen_cash

            # Get remote balance
            user_state = await hyperliquid_trading_service.get_user_state_async()
            if "marginSummary" not in user_state:
                logger.warning("marginSummary not found in Hyperliquid user state")
                return {"error": "Remote data unavailable"}

            margin = user_state["marginSummary"]
            remote_balance = Decimal(str(margin.get("accountValue", "0")))

            # Calculate difference
            diff_percent = self._calculate_difference_percent(local_balance, remote_balance)
            exceeds_threshold = diff_percent > self.threshold_percent

            result = {
                "account_id": account_id,
                "local_balance": local_balance,
                "remote_balance": remote_balance,
                "diff_percent": diff_percent,
                "exceeds_threshold": exceeds_threshold,
                "mismatch_duration": 0,
                "alert_sent": False,
            }

            now = datetime.now(UTC)

            if exceeds_threshold:
                # Track or update mismatch
                if account_id not in self._mismatches:
                    # New mismatch detected
                    self._mismatches[account_id] = {
                        "first_seen": now,
                        "last_diff_percent": diff_percent,
                    }
                    logger.warning(
                        f"Balance mismatch detected for account {account_id}",
                        extra={
                            "context": {
                                "local": float(local_balance),
                                "remote": float(remote_balance),
                                "diff_percent": diff_percent,
                            }
                        },
                    )
                else:
                    # Update existing mismatch
                    mismatch = self._mismatches[account_id]
                    mismatch["last_diff_percent"] = diff_percent

                    # Calculate duration
                    duration = (now - mismatch["first_seen"]).total_seconds()
                    result["mismatch_duration"] = int(duration)

                    # Send alert if persistence threshold exceeded
                    if duration >= self.persistence_seconds:
                        await alerting_service.send_alert(
                            level=AlertLevel.ERROR,
                            title="Balance Mismatch: Persistent Discrepancy Detected",
                            message=f"Account {account_id} balance has been mismatched for {int(duration)}s (threshold: {self.persistence_seconds}s). "
                            f"Local: ${local_balance:.2f}, Hyperliquid: ${remote_balance:.2f} ({diff_percent:.2f}% difference)",
                            metadata={
                                "account_id": account_id,
                                "local_balance": float(local_balance),
                                "remote_balance": float(remote_balance),
                                "diff_percent": diff_percent,
                                "mismatch_duration_seconds": int(duration),
                                "threshold_percent": self.threshold_percent,
                                "persistence_threshold_seconds": self.persistence_seconds,
                            },
                        )
                        result["alert_sent"] = True

                        # Reset tracking after alert sent
                        # This prevents alert spam - will alert again if mismatch persists for another 5 minutes
                        del self._mismatches[account_id]

                        logger.error(
                            f"Balance mismatch alert sent for account {account_id}",
                            extra={"context": result},
                        )
            else:
                # No mismatch or resolved - clear tracking
                if account_id in self._mismatches:
                    logger.info(
                        f"Balance mismatch resolved for account {account_id}",
                        extra={
                            "context": {
                                "local": float(local_balance),
                                "remote": float(remote_balance),
                                "diff_percent": diff_percent,
                            }
                        },
                    )
                    del self._mismatches[account_id]

            return result

        except Exception as e:
            logger.error(
                f"Failed to check balance for account {account_id}: {e}",
                extra={"context": {"error": str(e)}},
            )
            return {"error": str(e)}

    async def check_all_accounts(self) -> dict[str, any]:
        """Check all active accounts for balance mismatches.

        Returns:
            Dict with check summary:
            {
                "checked": 3,
                "mismatches": 1,
                "alerts_sent": 0,
                "errors": 0,
                "results": [...]
            }
        """
        results = []
        mismatches = 0
        alerts_sent = 0
        errors = 0

        async for db in get_async_session():
            try:
                # Get all accounts
                accounts = await AccountRepository.get_all(db=db)

                for account in accounts:
                    result = await self.check_account_balance(db=db, account_id=account.id)

                    if "error" in result:
                        errors += 1
                    else:
                        if result.get("exceeds_threshold"):
                            mismatches += 1
                        if result.get("alert_sent"):
                            alerts_sent += 1

                    results.append(result)

                break  # Exit after processing

            except Exception as e:
                logger.error(
                    f"Failed to check all accounts: {e}",
                    extra={"context": {"error": str(e)}},
                )
                errors += 1

        summary = {
            "checked": len(results),
            "mismatches": mismatches,
            "alerts_sent": alerts_sent,
            "errors": errors,
            "results": results,
        }

        logger.info(
            "Balance check completed",
            extra={"context": summary},
        )

        return summary

    async def monitor_loop(self, interval_seconds: int = 60) -> None:
        """Run continuous monitoring loop.

        Args:
            interval_seconds: Check interval in seconds (default: 60s = 1 minute)
        """
        logger.info(f"Starting balance monitor loop (interval: {interval_seconds}s)")

        while True:
            try:
                await self.check_all_accounts()
            except Exception as e:
                logger.error(
                    f"Balance monitor loop error: {e}",
                    extra={"context": {"error": str(e)}},
                )

            await asyncio.sleep(interval_seconds)


# Global singleton instance
balance_monitor = BalanceMonitor()
