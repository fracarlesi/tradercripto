"""Background sync jobs for periodic Hyperliquid synchronization."""

from datetime import UTC, datetime

from config.logging import get_logger
from database.connection import async_session_factory
from repositories.account_repo import AccountRepository
from services.exceptions import CircuitBreakerOpenException, SyncException
from services.infrastructure.sync_state_tracker import sync_state_tracker
from services.trading.hyperliquid_sync_service import hyperliquid_sync_service

logger = get_logger(__name__)


async def periodic_sync_job() -> None:
    """Periodic job to sync all active accounts with Hyperliquid.

    Runs every 30 seconds (configurable via SYNC_INTERVAL_SECONDS).
    Syncs each account independently, continues on individual failures.

    This function is called by APScheduler automatically.
    """
    logger.debug("Starting periodic sync job")

    async with async_session_factory() as db:
        try:
            # Get all active accounts
            accounts = await AccountRepository.get_all_active(db)

            if not accounts:
                logger.debug("No active accounts to sync")
                return

            synced_count = 0
            failed_count = 0

            for account in accounts:
                started_at = datetime.now(UTC)

                try:
                    # Record attempt
                    sync_state_tracker.record_sync_attempt(account.id, account.name, started_at)

                    # Sync account
                    await hyperliquid_sync_service.sync_account(db, account.id)

                    # Record success
                    finished_at = datetime.now(UTC)
                    sync_state_tracker.record_sync_success(account.id, started_at, finished_at)

                    synced_count += 1

                    logger.debug(
                        "Account synced successfully",
                        extra={
                            "context": {
                                "account_id": account.id,
                                "account_name": account.name,
                            }
                        },
                    )

                except CircuitBreakerOpenException as e:
                    # Circuit breaker open - record failure and skip
                    finished_at = datetime.now(UTC)
                    sync_state_tracker.record_sync_failure(
                        account.id, started_at, finished_at, str(e)
                    )

                    failed_count += 1

                    logger.warning(
                        "Sync blocked by circuit breaker",
                        extra={
                            "context": {
                                "account_id": account.id,
                                "account_name": account.name,
                                "error": str(e),
                            }
                        },
                    )

                except SyncException as e:
                    # Sync failed - record failure and continue
                    finished_at = datetime.now(UTC)
                    sync_state_tracker.record_sync_failure(
                        account.id, started_at, finished_at, str(e)
                    )

                    failed_count += 1

                    logger.error(
                        "Account sync failed",
                        extra={
                            "context": {
                                "account_id": account.id,
                                "account_name": account.name,
                                "error": str(e),
                            }
                        },
                    )

            # Log summary
            if synced_count > 0 or failed_count > 0:
                logger.info(
                    "Periodic sync completed",
                    extra={
                        "context": {
                            "synced": synced_count,
                            "failed": failed_count,
                            "total": len(accounts),
                        }
                    },
                )

        except Exception as e:
            logger.error(
                "Periodic sync job failed",
                extra={"context": {"error": str(e)}},
            )
