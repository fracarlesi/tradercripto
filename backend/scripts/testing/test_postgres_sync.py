"""PostgreSQL Sync Operations Testing Script (T072).

Tests all sync operations on PostgreSQL to verify:
- Concurrent write handling
- Connection pooling behavior
- Transaction isolation levels
- Data integrity under load

Usage:
    # Ensure PostgreSQL is running via docker-compose
    docker-compose up -d postgres

    # Run tests
    DATABASE_URL=postgresql+asyncpg://trader:trader_password@localhost:5432/trader_db \
        python backend/scripts/testing/test_postgres_sync.py
"""

import asyncio
import os
from datetime import datetime
from decimal import Decimal

from config.logging import get_logger
from database.connection import async_session_factory, engine
from database.models import Account, Order, User
from repositories.account_repo import AccountRepository
from repositories.order_repo import OrderRepository
from repositories.user_repo import UserRepository
from sqlalchemy import select

logger = get_logger(__name__)


class PostgreSQLSyncTester:
    """Test PostgreSQL sync operations."""

    def __init__(self):
        """Initialize tester."""
        self.results = []

    async def test_concurrent_writes(self, num_tasks: int = 10) -> bool:
        """Test concurrent write operations.

        Args:
            num_tasks: Number of concurrent tasks

        Returns:
            True if test passed
        """
        print(f"\n🧪 Testing {num_tasks} concurrent writes...")

        async def create_user(index: int):
            async with async_session_factory() as session:
                user = await UserRepository.create_user(
                    session, username=f"test_concurrent_{index}_{datetime.now().timestamp()}"
                )
                return user.id

        try:
            start = datetime.now()
            user_ids = await asyncio.gather(*[create_user(i) for i in range(num_tasks)])
            duration = (datetime.now() - start).total_seconds()

            print(f"  ✅ Created {len(user_ids)} users in {duration:.2f}s")
            print(f"     Throughput: {len(user_ids) / duration:.1f} writes/sec")
            return True

        except Exception as e:
            print(f"  ❌ Concurrent write test failed: {e}")
            return False

    async def test_connection_pooling(self) -> bool:
        """Test connection pool behavior under load.

        Returns:
            True if test passed
        """
        print("\n🧪 Testing connection pool behavior...")

        try:
            # Get pool metrics before test
            pool = engine.pool
            initial_checkedout = pool.checkedout()

            # Create 15 concurrent connections (pool_size=10, max_overflow=5)
            async def query_user(index: int):
                async with async_session_factory() as session:
                    await asyncio.sleep(0.1)  # Hold connection briefly
                    result = await session.execute(select(User).limit(1))
                    return result.scalar_one_or_none()

            tasks = [query_user(i) for i in range(15)]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Check for exceptions
            exceptions = [r for r in results if isinstance(r, Exception)]
            if exceptions:
                print(f"  ❌ {len(exceptions)} tasks failed with exceptions")
                return False

            print("  ✅ Handled 15 concurrent connections")
            print(f"     Pool size: {pool.size()}")
            print(f"     Max overflow: {pool.overflow()}")
            print(f"     Connections used: {pool.checkedout()}")
            return True

        except Exception as e:
            print(f"  ❌ Connection pooling test failed: {e}")
            return False

    async def test_transaction_isolation(self) -> bool:
        """Test transaction isolation levels.

        Returns:
            True if test passed
        """
        print("\n🧪 Testing transaction isolation...")

        try:
            # Create test user
            async with async_session_factory() as session:
                user = await UserRepository.get_or_create_user(session, "isolation_test")
                account = await AccountRepository.get_or_create_default_account(
                    session, user.id, "Test Account", 1000.0
                )
                account_id = account.id

            # Test: Two concurrent transactions updating same account
            async def update_cash(amount: Decimal, delay: float = 0):
                async with async_session_factory() as session:
                    result = await session.execute(select(Account).where(Account.id == account_id))
                    acc = result.scalar_one()

                    await asyncio.sleep(delay)

                    acc.current_cash = float(acc.current_cash) + float(amount)
                    await session.flush()
                    await session.commit()

            # Run concurrent updates
            await asyncio.gather(
                update_cash(Decimal("100.0"), 0.05),
                update_cash(Decimal("200.0"), 0.05),
            )

            # Verify final balance
            async with async_session_factory() as session:
                result = await session.execute(select(Account).where(Account.id == account_id))
                acc = result.scalar_one()
                final_cash = float(acc.current_cash)

            expected = 1000.0 + 100.0 + 200.0
            if abs(final_cash - expected) < 0.01:
                print("  ✅ Transaction isolation maintained")
                print(f"     Final balance: ${final_cash:.2f} (expected: ${expected:.2f})")
                return True
            else:
                print("  ❌ Transaction isolation failed")
                print(f"     Final balance: ${final_cash:.2f} (expected: ${expected:.2f})")
                return False

        except Exception as e:
            print(f"  ❌ Transaction isolation test failed: {e}")
            return False

    async def test_foreign_key_constraints(self) -> bool:
        """Test foreign key constraint enforcement.

        Returns:
            True if test passed
        """
        print("\n🧪 Testing foreign key constraints...")

        try:
            # Create test data
            async with async_session_factory() as session:
                user = await UserRepository.create_user(
                    session, f"fk_test_{datetime.now().timestamp()}"
                )
                account = await AccountRepository.create_account(
                    session,
                    user_id=user.id,
                    name="FK Test Account",
                    account_type="AI",
                    initial_capital=1000.0,
                )

            # Try to create order with invalid account_id (should fail)
            try:
                async with async_session_factory() as session:
                    invalid_order = Order(
                        order_no=f"INVALID_{datetime.now().timestamp()}",
                        user_id=user.id,
                        account_id=99999,  # Invalid account ID
                        symbol="BTC",
                        name="Bitcoin",
                        market="hyperliquid",
                        side="BUY",
                        order_type="MARKET",
                        quantity=Decimal("1.0"),
                        filled_quantity=Decimal("0"),
                        status="PENDING",
                    )
                    session.add(invalid_order)
                    await session.commit()

                print("  ❌ Foreign key constraint not enforced (invalid order created)")
                return False

            except Exception:
                print("  ✅ Foreign key constraint enforced correctly")
                return True

        except Exception as e:
            print(f"  ❌ Foreign key constraint test failed: {e}")
            return False

    async def test_concurrent_order_creation(self, num_orders: int = 5) -> bool:
        """Test concurrent order creation for same account.

        Args:
            num_orders: Number of concurrent orders

        Returns:
            True if test passed
        """
        print(f"\n🧪 Testing {num_orders} concurrent order creations...")

        try:
            # Create test account
            async with async_session_factory() as session:
                user = await UserRepository.get_or_create_user(session, "concurrent_orders_test")
                account = await AccountRepository.get_or_create_default_account(
                    session, user.id, "Concurrent Test", 10000.0
                )
                account_id = account.id

            # Create orders concurrently
            async def create_order(index: int):
                async with async_session_factory() as session:
                    # Get account
                    result = await session.execute(select(Account).where(Account.id == account_id))
                    acc = result.scalar_one()

                    order = await OrderRepository.create_order(
                        session,
                        account=acc,
                        symbol=f"TEST{index}",
                        name=f"Test Coin {index}",
                        market="hyperliquid",
                        side="BUY",
                        order_type="MARKET",
                        quantity=Decimal("1.0"),
                        price=None,
                    )
                    return order.id

            start = datetime.now()
            order_ids = await asyncio.gather(*[create_order(i) for i in range(num_orders)])
            duration = (datetime.now() - start).total_seconds()

            print(f"  ✅ Created {len(order_ids)} orders in {duration:.2f}s")
            print(f"     All orders have unique IDs: {len(set(order_ids)) == len(order_ids)}")
            return True

        except Exception as e:
            print(f"  ❌ Concurrent order creation test failed: {e}")
            return False

    async def run_all_tests(self) -> dict:
        """Run all PostgreSQL sync tests.

        Returns:
            Dict with test results
        """
        print("=" * 80)
        print("PostgreSQL Sync Operations Test Suite (T072)")
        print("=" * 80)
        print(f"\nDatabase URL: {os.getenv('DATABASE_URL', 'Not set')}")
        print(f"Pool size: {engine.pool.size()}")
        print(f"Max overflow: {engine.pool.overflow()}")

        results = {}

        # Run tests
        results["concurrent_writes"] = await self.test_concurrent_writes(10)
        results["connection_pooling"] = await self.test_connection_pooling()
        results["transaction_isolation"] = await self.test_transaction_isolation()
        results["foreign_key_constraints"] = await self.test_foreign_key_constraints()
        results["concurrent_order_creation"] = await self.test_concurrent_order_creation(5)

        # Print summary
        print("\n" + "=" * 80)
        print("TEST SUMMARY")
        print("=" * 80)

        passed = sum(1 for v in results.values() if v)
        total = len(results)

        for test_name, passed_test in results.items():
            status = "✅ PASS" if passed_test else "❌ FAIL"
            print(f"  {status} {test_name}")

        print("\n" + "=" * 80)
        print(f"Results: {passed}/{total} tests passed")

        if passed == total:
            print("✅ ALL TESTS PASSED - PostgreSQL ready for production")
        else:
            print(f"❌ {total - passed} TESTS FAILED - Review failures above")

        print("=" * 80 + "\n")

        return results


async def main():
    """Run PostgreSQL sync tests."""
    # Check if PostgreSQL URL is set
    db_url = os.getenv("DATABASE_URL", "")
    if "postgresql" not in db_url:
        print("❌ ERROR: DATABASE_URL must be set to PostgreSQL connection string")
        print("\nUsage:")
        print(
            "  DATABASE_URL=postgresql+asyncpg://trader:trader_password@localhost:5432/trader_db \\"
        )
        print("    python backend/scripts/testing/test_postgres_sync.py")
        return 1

    tester = PostgreSQLSyncTester()
    results = await tester.run_all_tests()

    # Return exit code based on results
    all_passed = all(results.values())
    return 0 if all_passed else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
