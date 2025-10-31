"""Synchronization management endpoints."""

import time
from datetime import UTC, datetime, timedelta

from config.logging import get_logger
from database.connection import get_db
from repositories.account_repo import AccountRepository
from services.exceptions import CircuitBreakerOpenException, SyncException
from services.infrastructure.sync_state_tracker import sync_state_tracker
from services.trading.hyperliquid_sync_service import hyperliquid_sync_service
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)

router = APIRouter(prefix="/api/sync", tags=["synchronization"])


class AccountSyncStatus(BaseModel):
    """Sync status for a single account."""

    account_id: int
    account_name: str
    last_sync_time: datetime | None = None
    last_sync_duration_ms: int | None = None
    sync_status: str  # success, failed, pending
    consecutive_failures: int = Field(ge=0)
    data_freshness_seconds: int | None = None
    next_sync_estimated: datetime | None = None


class SyncStatusResponse(BaseModel):
    """Sync status for all accounts."""

    accounts: list[AccountSyncStatus]
    overall_status: str  # healthy, degraded, failing
    message: str


class BalanceData(BaseModel):
    """Balance data in sync result."""

    available: float
    frozen: float
    total_equity: float


class SyncData(BaseModel):
    """Synced data summary."""

    balance: BalanceData
    positions_count: int
    new_orders_count: int
    new_trades_count: int


class SyncResultResponse(BaseModel):
    """Result of sync operation."""

    success: bool
    account_id: int
    account_name: str
    sync_duration_ms: int
    synced_at: datetime | None = None
    data: SyncData | None = None
    error: str | None = None
    error_code: str | None = None
    message: str


class AccountSyncResult(BaseModel):
    """Result of single account sync in batch operation."""

    account_id: int
    account_name: str
    success: bool
    message: str
    error_code: str | None = None


class SyncAllResultResponse(BaseModel):
    """Result of syncing all accounts."""

    success: bool
    total_accounts: int
    synced_accounts: int
    failed_accounts: int
    sync_duration_ms: int
    results: list[AccountSyncResult]
    message: str


@router.get("/status", response_model=SyncStatusResponse)
async def get_sync_status(db: AsyncSession = Depends(get_db)) -> SyncStatusResponse:
    """Get current sync status for all accounts (T043).

    Returns sync health, timing, and freshness for all active accounts.
    """
    try:
        # Get all active accounts
        accounts = await AccountRepository.get_all_active(db)

        account_statuses = []
        now = datetime.now(UTC)

        for account in accounts:
            state = sync_state_tracker.get_account_state(account.id)

            if state is None:
                # Account never synced
                account_statuses.append(
                    AccountSyncStatus(
                        account_id=account.id,
                        account_name=account.name,
                        last_sync_time=None,
                        last_sync_duration_ms=None,
                        sync_status="pending",
                        consecutive_failures=0,
                        data_freshness_seconds=None,
                        next_sync_estimated=None,
                    )
                )
            else:
                last_sync = state.get("last_sync_time")
                freshness = int((now - last_sync).total_seconds()) if last_sync else None
                next_sync = last_sync + timedelta(seconds=30) if last_sync else None

                account_statuses.append(
                    AccountSyncStatus(
                        account_id=account.id,
                        account_name=account.name,
                        last_sync_time=last_sync,
                        last_sync_duration_ms=state.get("last_sync_duration_ms"),
                        sync_status=state.get("sync_status", "pending"),
                        consecutive_failures=state.get("consecutive_failures", 0),
                        data_freshness_seconds=freshness,
                        next_sync_estimated=next_sync,
                    )
                )

        # Determine overall status
        failing_count = sum(1 for s in account_statuses if s.consecutive_failures >= 3)
        stale_count = sum(
            1
            for s in account_statuses
            if s.data_freshness_seconds and s.data_freshness_seconds > 120
        )

        if failing_count > 0:
            overall_status = "failing"
            message = f"{failing_count} account(s) with 3+ consecutive sync failures"
        elif stale_count > 0:
            overall_status = "degraded"
            message = f"{stale_count} account(s) with stale data (>2 minutes)"
        else:
            overall_status = "healthy"
            message = "All accounts syncing normally"

        return SyncStatusResponse(
            accounts=account_statuses,
            overall_status=overall_status,
            message=message,
        )

    except Exception as e:
        logger.error(
            "Failed to get sync status",
            extra={"context": {"error": str(e)}},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get sync status: {str(e)}",
        )


@router.post("/account/{account_id}", response_model=SyncResultResponse)
async def sync_account(account_id: int, db: AsyncSession = Depends(get_db)) -> SyncResultResponse:
    """Trigger manual sync for a specific account (T044).

    Fetches latest data from Hyperliquid and updates local database atomically.

    Returns:
        200: Sync successful
        404: Account not found
        503: Sync failed (API unreachable, circuit breaker open)
    """
    started_at = datetime.now(UTC)
    start_time_ms = time.time()

    try:
        # Get account
        account = await AccountRepository.get_by_id(db, account_id)
        if not account:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Account {account_id} not found",
            )

        # Record sync attempt
        sync_state_tracker.record_sync_attempt(account_id, account.name, started_at)

        # Perform sync
        result = await hyperliquid_sync_service.sync_account(db, account_id)

        # Calculate duration
        finished_at = datetime.now(UTC)
        duration_ms = int((time.time() - start_time_ms) * 1000)

        # Record success
        sync_state_tracker.record_sync_success(account_id, started_at, finished_at)

        # Build response
        return SyncResultResponse(
            success=True,
            account_id=account_id,
            account_name=account.name,
            sync_duration_ms=duration_ms,
            synced_at=finished_at,
            data=SyncData(
                balance=BalanceData(
                    available=float(account.current_cash),
                    frozen=float(account.frozen_cash),
                    total_equity=float(account.current_cash + account.frozen_cash),
                ),
                positions_count=result.get("positions_synced", 0),
                new_orders_count=result.get("orders_synced", 0),
                new_trades_count=result.get("trades_synced", 0),
            ),
            message="Account synced successfully",
        )

    except CircuitBreakerOpenException as e:
        # Circuit breaker open - return 503
        finished_at = datetime.now(UTC)
        duration_ms = int((time.time() - start_time_ms) * 1000)

        sync_state_tracker.record_sync_failure(account_id, started_at, finished_at, str(e))

        return SyncResultResponse(
            success=False,
            account_id=account_id,
            account_name=account.name if account else "Unknown",
            sync_duration_ms=duration_ms,
            error=str(e),
            error_code="SYNC_CIRCUIT_BREAKER_OPEN",
            message=f"Sync blocked: {str(e)}",
        )

    except SyncException as e:
        # Sync failed
        finished_at = datetime.now(UTC)
        duration_ms = int((time.time() - start_time_ms) * 1000)

        account = await AccountRepository.get_by_id(db, account_id)
        account_name = account.name if account else "Unknown"

        sync_state_tracker.record_sync_failure(account_id, started_at, finished_at, str(e))

        return SyncResultResponse(
            success=False,
            account_id=account_id,
            account_name=account_name,
            sync_duration_ms=duration_ms,
            error=str(e),
            error_code="SYNC_API_ERROR",
            message=f"Sync failed: {str(e)}",
        )

    except Exception as e:
        # Unexpected error
        logger.error(
            "Unexpected error in sync_account",
            extra={"context": {"account_id": account_id, "error": str(e)}},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Sync failed: {str(e)}",
        )


@router.post("/all", response_model=SyncAllResultResponse)
async def sync_all_accounts(
    db: AsyncSession = Depends(get_db),
) -> SyncAllResultResponse:
    """Trigger sync for all active accounts (T045).

    Syncs each account independently, continues even if individual syncs fail.

    Returns:
        200: Sync completed (partial failures possible)
    """
    start_time_ms = time.time()

    try:
        # Get all active accounts
        accounts = await AccountRepository.get_all_active(db)

        if not accounts:
            return SyncAllResultResponse(
                success=True,
                total_accounts=0,
                synced_accounts=0,
                failed_accounts=0,
                sync_duration_ms=0,
                results=[],
                message="No active accounts to sync",
            )

        results = []
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

                results.append(
                    AccountSyncResult(
                        account_id=account.id,
                        account_name=account.name,
                        success=True,
                        message="Account synced successfully",
                    )
                )
                synced_count += 1

            except (CircuitBreakerOpenException, SyncException) as e:
                # Sync failed - record and continue
                finished_at = datetime.now(UTC)
                sync_state_tracker.record_sync_failure(account.id, started_at, finished_at, str(e))

                error_code = (
                    "SYNC_CIRCUIT_BREAKER_OPEN"
                    if isinstance(e, CircuitBreakerOpenException)
                    else "SYNC_API_ERROR"
                )

                results.append(
                    AccountSyncResult(
                        account_id=account.id,
                        account_name=account.name,
                        success=False,
                        message=f"Sync failed: {str(e)}",
                        error_code=error_code,
                    )
                )
                failed_count += 1

        # Calculate total duration
        duration_ms = int((time.time() - start_time_ms) * 1000)

        # Build response
        success = failed_count == 0
        if success:
            message = "All accounts synced successfully"
        else:
            message = f"{synced_count} succeeded, {failed_count} failed"

        return SyncAllResultResponse(
            success=success,
            total_accounts=len(accounts),
            synced_accounts=synced_count,
            failed_accounts=failed_count,
            sync_duration_ms=duration_ms,
            results=results,
            message=message,
        )

    except Exception as e:
        logger.error(
            "Failed to sync all accounts",
            extra={"context": {"error": str(e)}},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to sync all accounts: {str(e)}",
        )
