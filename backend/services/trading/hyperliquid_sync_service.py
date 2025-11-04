"""Hyperliquid Synchronization Service.

Handles atomic synchronization of account data from Hyperliquid to local database.
Implements circuit breaker and retry patterns for resilience.
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from config.logging import get_logger
from database.models import Account, Order, Position, Trade
from repositories.account_repo import AccountRepository
from repositories.order_repo import OrderRepository
from repositories.position_repo import PositionRepository
from repositories.trade_repo import TradeRepository
from services.exceptions import (
    CircuitBreakerOpenException,
    SyncException,
)
from services.infrastructure.alerting import (
    AlertLevel,
    alerting_service,
)
from services.trading.hyperliquid_trading_service import (
    hyperliquid_trading_service,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Blocking requests after failures
    HALF_OPEN = "half_open"  # Testing if service recovered


class HyperliquidSyncService:
    """Synchronization service with circuit breaker and retry logic.

    Circuit Breaker:
    - CLOSED: Normal operation
    - OPEN: After 5 consecutive failures, block all requests
    - HALF_OPEN: After 60s, allow 1 probe request
    - Close on probe success, reopen on probe failure

    Retry Logic:
    - Exponential backoff: 1s, 2s, 4s, 8s, 16s
    - Maximum 5 attempts
    - Only retries transient errors (network, timeouts)
    """

    def __init__(self) -> None:
        """Initialize sync service with circuit breaker state."""
        self._circuit_state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: datetime | None = None
        self._last_error: str = ""
        self._last_account_id: int | None = None
        self._circuit_open_duration = 60  # seconds
        self._max_failures = 5
        self._max_retries = 5
        self._base_delay = 1.0  # seconds

    def _is_transient_error(self, error: Exception) -> bool:
        """Check if error is transient and worth retrying.

        Args:
            error: Exception to check

        Returns:
            True if error is transient (network, timeout, rate limit)
        """
        error_str = str(error).lower()
        transient_indicators = [
            "timeout",
            "connection",
            "network",
            "rate limit",
            "too many requests",
            "503",
            "502",
            "504",
        ]
        return any(indicator in error_str for indicator in transient_indicators)

    def _check_circuit_breaker(self) -> None:
        """Check circuit breaker state and potentially transition.

        Raises:
            CircuitBreakerOpenException: If circuit is open and not ready for probe
        """
        if self._circuit_state == CircuitState.OPEN:
            if self._last_failure_time is None:
                # Should not happen, but reset if it does
                self._circuit_state = CircuitState.CLOSED
                self._failure_count = 0
                return

            # Check if enough time passed to enter half-open
            seconds_since_failure = (datetime.now(UTC) - self._last_failure_time).total_seconds()

            if seconds_since_failure >= self._circuit_open_duration:
                logger.info("Circuit breaker entering HALF_OPEN state (probe request allowed)")
                self._circuit_state = CircuitState.HALF_OPEN
            else:
                remaining = self._circuit_open_duration - seconds_since_failure
                raise CircuitBreakerOpenException(
                    f"Circuit breaker OPEN - retry in {remaining:.0f}s"
                )

    def _record_success(self) -> None:
        """Record successful operation - reset circuit breaker."""
        if self._circuit_state == CircuitState.HALF_OPEN:
            logger.info("Circuit breaker HALF_OPEN → CLOSED (probe succeeded)")

        self._circuit_state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = None

    def _record_failure(self, error: str = "", account_id: int | None = None) -> None:
        """Record failed operation - potentially open circuit breaker and send alerts (T134).

        Args:
            error: Error message for alerting
            account_id: Account ID for alerting context
        """
        self._failure_count += 1
        self._last_failure_time = datetime.now(UTC)
        self._last_error = error
        if account_id:
            self._last_account_id = account_id

        # Send alert after 3 consecutive failures (T134)
        if self._failure_count == 3:
            asyncio.create_task(
                alerting_service.send_alert(
                    level=AlertLevel.WARNING,
                    title="Sync Failures: 3 Consecutive Failures Detected",
                    message="Account sync has failed 3 times in a row. System is approaching circuit breaker threshold.",
                    metadata={
                        "account_id": self._last_account_id,
                        "failure_count": self._failure_count,
                        "last_error": self._last_error,
                        "timestamp": self._last_failure_time.isoformat()
                        if self._last_failure_time
                        else None,
                        "circuit_state": self._circuit_state.value,
                    },
                )
            )

        if self._circuit_state == CircuitState.HALF_OPEN:
            # Probe failed - reopen circuit
            logger.warning("Circuit breaker HALF_OPEN → OPEN (probe failed)")
            self._circuit_state = CircuitState.OPEN
        elif self._failure_count >= self._max_failures:
            logger.error(
                f"Circuit breaker CLOSED → OPEN ({self._failure_count} consecutive failures)"
            )
            self._circuit_state = CircuitState.OPEN

            # Send critical alert when circuit breaker opens (T134)
            asyncio.create_task(
                alerting_service.send_alert(
                    level=AlertLevel.CRITICAL,
                    title="Circuit Breaker OPEN: Sync Service Down",
                    message=f"Circuit breaker has opened after {self._failure_count} consecutive failures. All sync operations are blocked for {self._circuit_open_duration}s.",
                    metadata={
                        "account_id": self._last_account_id,
                        "failure_count": self._failure_count,
                        "last_error": self._last_error,
                        "timestamp": self._last_failure_time.isoformat()
                        if self._last_failure_time
                        else None,
                        "circuit_state": self._circuit_state.value,
                        "circuit_open_duration": self._circuit_open_duration,
                    },
                )
            )

    # REMOVED: sync_account_balance() method
    # Balance fields (current_cash, frozen_cash) were removed from Account model during refactoring.
    # Balance data should ALWAYS be fetched directly from Hyperliquid API in real-time,
    # not stored in database. See Account model docstring for details.

    async def sync_positions(self, db: AsyncSession, account: Account) -> int:
        """Sync positions using clear-recreate strategy (T035).

        Args:
            db: Async database session
            account: Account to sync

        Returns:
            Number of positions synced

        Raises:
            SyncException: If sync fails
        """
        try:
            user_state = await hyperliquid_trading_service.get_user_state_async()
            hyperliquid_positions = user_state.get("assetPositions", [])

            # Clear all existing positions (clear-recreate strategy)
            await PositionRepository.clear_positions(db=db, account_id=account.id)

            # Create fresh positions from Hyperliquid
            positions_to_create = []
            for hl_pos in hyperliquid_positions:
                pos_data = hl_pos.get("position", {})
                symbol = pos_data.get("coin")
                size = Decimal(str(pos_data.get("szi", "0")))

                if size != 0 and symbol:
                    entry_price = Decimal(str(pos_data.get("entryPx", "0")))

                    position = Position(
                        account_id=account.id,
                        symbol=symbol,
                        quantity=abs(size),
                        available_quantity=abs(size),
                        average_cost=entry_price,
                    )
                    positions_to_create.append(position)

            # Bulk insert
            if positions_to_create:
                await PositionRepository.bulk_create_positions(
                    db=db, positions_list=positions_to_create
                )

            count = len(positions_to_create)
            logger.info(
                "Positions synced",
                extra={"context": {"account_id": account.id, "count": count}},
            )

            return count

        except Exception as e:
            logger.error(
                "Failed to sync positions",
                extra={"context": {"account_id": account.id, "error": str(e)}},
            )
            raise SyncException(f"Position sync failed: {e}") from e

    async def sync_orders_from_fills(
        self, db: AsyncSession, account: Account, fills: list[dict[str, Any]]
    ) -> int:
        """Sync orders from Hyperliquid fills with deduplication (T036).

        Args:
            db: Async database session
            account: Account to sync
            fills: List of fill dicts from Hyperliquid

        Returns:
            Number of new orders created

        Raises:
            SyncException: If sync fails
        """
        try:
            created_count = 0

            for fill in fills:
                coin = fill.get("coin")
                side_char = fill.get("side")  # 'B' or 'S'
                size = Decimal(str(fill.get("sz", "0")))
                price = Decimal(str(fill.get("px", "0")))
                time_ms = fill.get("time")

                if not all([coin, side_char, time_ms]):
                    continue

                # Generate unique order_no from fill data
                order_no = f"HL_{time_ms}_{coin}_{side_char}"

                # Check if order already exists (deduplication)
                existing = await OrderRepository.get_by_order_no(db=db, order_no=order_no)
                if existing:
                    continue

                # Convert side to standard format
                side = "BUY" if side_char == "B" else "SELL"

                # Create order
                order = Order(
                    account_id=account.id,
                    order_no=order_no,
                    symbol=coin,
                    side=side,
                    order_type="MARKET",
                    price=price,
                    quantity=size,
                    filled_quantity=size,
                    status="FILLED",
                )

                await OrderRepository.create_order(db=db, order_data=order)
                created_count += 1

            logger.info(
                "Orders synced from fills",
                extra={"context": {"account_id": account.id, "count": created_count}},
            )

            return created_count

        except Exception as e:
            logger.error(
                "Failed to sync orders",
                extra={"context": {"account_id": account.id, "error": str(e)}},
            )
            raise SyncException(f"Order sync failed: {e}") from e

    async def sync_trades_from_fills(
        self, db: AsyncSession, account: Account, fills: list[dict[str, Any]]
    ) -> int:
        """Sync trades from Hyperliquid fills with composite key deduplication (T037).

        Args:
            db: Async database session
            account: Account to sync
            fills: List of fill dicts from Hyperliquid

        Returns:
            Number of new trades created

        Raises:
            SyncException: If sync fails
        """
        try:
            created_count = 0

            for fill in fills:
                coin = fill.get("coin")
                side_char = fill.get("side")  # 'B' or 'S'
                size = Decimal(str(fill.get("sz", "0")))
                price = Decimal(str(fill.get("px", "0")))
                time_ms = fill.get("time")

                if not all([coin, side_char, time_ms]):
                    continue

                # Convert timestamp
                trade_time = datetime.fromtimestamp(time_ms / 1000.0, tz=UTC)

                # Check for duplicate using composite key
                existing = await TradeRepository.find_duplicate(
                    db=db, trade_time=trade_time, symbol=coin, quantity=size, price=price
                )
                if existing:
                    continue

                # Convert side to standard format
                side = "BUY" if side_char == "B" else "SELL"

                # Create trade
                trade = Trade(
                    account_id=account.id,
                    symbol=coin,
                    side=side,
                    price=price,
                    quantity=size,
                    commission=Decimal("0"),
                    trade_time=trade_time,
                )

                await TradeRepository.create_trade(db=db, trade_data=trade)
                created_count += 1

            logger.info(
                "Trades synced from fills",
                extra={"context": {"account_id": account.id, "count": created_count}},
            )

            return created_count

        except Exception as e:
            logger.error(
                "Failed to sync trades",
                extra={"context": {"account_id": account.id, "error": str(e)}},
            )
            raise SyncException(f"Trade sync failed: {e}") from e

    async def sync_account(self, db: AsyncSession, account_id: int) -> dict[str, Any]:
        """Orchestrator method: atomic sync of balance, positions, orders, trades (T038-T040).

        Implements:
        - T038: Atomic transaction with rollback on failure
        - T039: Exponential backoff retry (1s, 2s, 4s, 8s, 16s)
        - T040: Circuit breaker pattern

        Args:
            db: Async database session
            account_id: Account ID to sync

        Returns:
            Dict with sync results:
            {
                "success": True,
                "account_id": 1,
                "positions_synced": 3,
                "orders_synced": 5,
                "trades_synced": 10,
                "attempts": 1
            }

        Raises:
            CircuitBreakerOpenException: If circuit breaker is open
            SyncException: If sync fails after all retries
        """
        # Check circuit breaker
        self._check_circuit_breaker()

        attempt = 0
        last_error: Exception | None = None

        while attempt < self._max_retries:
            attempt += 1

            try:
                # Get account
                account = await AccountRepository.get_by_id(db=db, account_id=account_id)
                if not account:
                    raise SyncException(f"Account {account_id} not found")

                # Atomic sync operations
                # Note: Balance is NOT synced - always fetched from Hyperliquid API in real-time
                async with db.begin_nested():
                    # 1. Sync positions (clear-recreate)
                    positions_synced = await self.sync_positions(db=db, account=account)

                    # 2. Get fills from Hyperliquid
                    fills = await hyperliquid_trading_service.get_user_fills_async(limit=100)

                    # 3. Sync orders from fills
                    orders_synced = await self.sync_orders_from_fills(
                        db=db, account=account, fills=fills
                    )

                    # 4. Sync trades from fills
                    trades_synced = await self.sync_trades_from_fills(
                        db=db, account=account, fills=fills
                    )

                # Commit transaction
                await db.commit()

                # Record success
                self._record_success()

                result = {
                    "success": True,
                    "account_id": account_id,
                    "positions_synced": positions_synced,
                    "orders_synced": orders_synced,
                    "trades_synced": trades_synced,
                    "attempts": attempt,
                }

                logger.info(
                    "Account sync completed",
                    extra={"context": result},
                )

                return result

            except (SQLAlchemyError, SyncException) as e:
                # Rollback transaction
                await db.rollback()

                last_error = e
                self._record_failure(error=str(e), account_id=account_id)

                # Check if error is transient and worth retrying
                if not self._is_transient_error(e) or attempt >= self._max_retries:
                    logger.error(
                        f"Account sync failed after {attempt} attempts",
                        extra={
                            "context": {
                                "account_id": account_id,
                                "error": str(e),
                                "attempts": attempt,
                            }
                        },
                    )
                    raise SyncException(f"Sync failed after {attempt} attempts: {e}") from e

                # Exponential backoff
                delay = min(self._base_delay * (2 ** (attempt - 1)), 16.0)
                logger.warning(
                    f"Sync attempt {attempt} failed, retrying in {delay}s",
                    extra={"context": {"account_id": account_id, "error": str(e)}},
                )
                await asyncio.sleep(delay)

        # Should not reach here, but handle gracefully
        raise SyncException(f"Sync failed after {self._max_retries} attempts: {last_error}")


# Global singleton instance
hyperliquid_sync_service = HyperliquidSyncService()


def sync_all_active_accounts() -> None:
    """Sync all active accounts (helper for scheduler).

    This function syncs all active accounts from Hyperliquid.
    Used by the scheduled task in startup.py.
    Wraps async logic in asyncio.run() for scheduler compatibility.
    """
    import asyncio

    async def _sync():
        from database.connection import get_db

        try:
            async for db in get_db():
                # Get all active accounts
                from repositories.account_repo import AccountRepository

                accounts = await AccountRepository.get_all_active(db)

                for account in accounts:
                    try:
                        await hyperliquid_sync_service.sync_account(db, account.id)
                        logger.debug(f"Synced account {account.id}")
                    except Exception as e:
                        logger.error(f"Failed to sync account {account.id}: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Failed to sync accounts: {e}", exc_info=True)

    try:
        asyncio.run(_sync())
    except Exception as e:
        logger.error(f"Sync task failed: {e}", exc_info=True)
