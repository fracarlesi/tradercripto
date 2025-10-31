"""Alembic Migrations Validation Script (T073).

Validates that Alembic migrations work identically on both SQLite and PostgreSQL.
Tests upgrade/downgrade cycles to ensure database schema compatibility.

Usage:
    # Test on SQLite
    DATABASE_URL=sqlite+aiosqlite:///./test_alembic.db \
        python backend/scripts/testing/validate_alembic.py

    # Test on PostgreSQL
    DATABASE_URL=postgresql+asyncpg://trader:trader_password@localhost:5432/test_alembic \
        python backend/scripts/testing/validate_alembic.py
"""

import asyncio
import os
import subprocess
from pathlib import Path

from config.logging import get_logger
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

logger = get_logger(__name__)


class AlembicValidator:
    """Validates Alembic migrations across databases."""

    def __init__(self, database_url: str):
        """Initialize validator.

        Args:
            database_url: Database connection URL
        """
        self.database_url = database_url
        self.is_sqlite = "sqlite" in database_url
        self.is_postgres = "postgresql" in database_url
        self.engine = None

    async def initialize_engine(self):
        """Initialize database engine."""
        self.engine = create_async_engine(self.database_url, echo=False)
        logger.info(f"Initialized engine for {self.get_db_type()}")

    async def close_engine(self):
        """Close database engine."""
        if self.engine:
            await self.engine.dispose()

    def get_db_type(self) -> str:
        """Get database type name.

        Returns:
            Database type ("SQLite" or "PostgreSQL")
        """
        if self.is_sqlite:
            return "SQLite"
        elif self.is_postgres:
            return "PostgreSQL"
        return "Unknown"

    async def get_table_names(self) -> list[str]:
        """Get list of tables in database.

        Returns:
            List of table names
        """
        async with self.engine.connect() as conn:
            result = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
            return sorted(result)

    async def get_table_columns(self, table_name: str) -> dict[str, str]:
        """Get columns for a table.

        Args:
            table_name: Name of table

        Returns:
            Dict mapping column names to types
        """
        async with self.engine.connect() as conn:
            result = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_columns(table_name)
            )
            return {col["name"]: str(col["type"]) for col in result}

    def run_alembic_command(self, command: str) -> tuple[int, str]:
        """Run Alembic command.

        Args:
            command: Alembic command (e.g., "upgrade head")

        Returns:
            Tuple of (return_code, output)
        """
        env = os.environ.copy()
        env["DATABASE_URL"] = self.database_url

        # Run alembic from project root
        project_root = Path(__file__).parent.parent.parent.parent
        alembic_cmd = f"cd {project_root} && alembic {command}"

        try:
            result = subprocess.run(
                alembic_cmd,
                shell=True,
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )
            return result.returncode, result.stdout + result.stderr

        except subprocess.TimeoutExpired:
            return 1, "Command timed out after 60 seconds"
        except Exception as e:
            return 1, f"Command failed: {str(e)}"

    async def test_upgrade_to_head(self) -> bool:
        """Test upgrading database to latest migration.

        Returns:
            True if upgrade successful
        """
        print(f"\n🧪 Testing 'alembic upgrade head' on {self.get_db_type()}...")

        returncode, output = self.run_alembic_command("upgrade head")

        if returncode == 0:
            # Verify tables were created
            tables = await self.get_table_names()
            expected_tables = ["users", "accounts", "positions", "orders", "trades"]
            has_tables = all(table in tables for table in expected_tables)

            if has_tables:
                print(f"  ✅ Upgrade successful - {len(tables)} tables created")
                print(f"     Tables: {', '.join(tables[:5])}" + ("..." if len(tables) > 5 else ""))
                return True
            else:
                print("  ❌ Upgrade completed but missing expected tables")
                print(f"     Found: {tables}")
                return False
        else:
            print("  ❌ Upgrade failed")
            print(f"     Output: {output[:200]}")
            return False

    async def test_downgrade_to_base(self) -> bool:
        """Test downgrading database to base (no migrations).

        Returns:
            True if downgrade successful
        """
        print(f"\n🧪 Testing 'alembic downgrade base' on {self.get_db_type()}...")

        returncode, output = self.run_alembic_command("downgrade base")

        if returncode == 0:
            # Verify tables were removed
            tables = await self.get_table_names()
            # Should only have alembic_version table left
            if len(tables) <= 1:
                print("  ✅ Downgrade successful - all tables removed")
                return True
            else:
                print("  ❌ Downgrade completed but tables remain")
                print(f"     Remaining tables: {tables}")
                return False
        else:
            print("  ❌ Downgrade failed")
            print(f"     Output: {output[:200]}")
            return False

    async def test_upgrade_downgrade_cycle(self) -> bool:
        """Test full upgrade -> downgrade -> upgrade cycle.

        Returns:
            True if cycle successful
        """
        print(f"\n🧪 Testing full upgrade/downgrade cycle on {self.get_db_type()}...")

        # Upgrade
        success_up1 = await self.test_upgrade_to_head()
        if not success_up1:
            return False

        # Get table count after first upgrade
        tables_after_upgrade = await self.get_table_names()

        # Downgrade
        success_down = await self.test_downgrade_to_base()
        if not success_down:
            return False

        # Upgrade again
        success_up2 = await self.test_upgrade_to_head()
        if not success_up2:
            return False

        # Verify same tables exist
        tables_after_reupgrade = await self.get_table_names()

        if tables_after_upgrade == tables_after_reupgrade:
            print("  ✅ Full cycle successful - schema consistent")
            return True
        else:
            print("  ❌ Schema differs after cycle")
            print(f"     Before: {tables_after_upgrade}")
            print(f"     After:  {tables_after_reupgrade}")
            return False

    async def test_schema_consistency(self) -> bool:
        """Test schema consistency for key tables.

        Returns:
            True if schema consistent
        """
        print(f"\n🧪 Testing schema consistency on {self.get_db_type()}...")

        try:
            # Check critical tables exist
            tables = await self.get_table_names()
            critical_tables = ["users", "accounts", "positions", "orders"]

            for table in critical_tables:
                if table not in tables:
                    print(f"  ❌ Missing critical table: {table}")
                    return False

            # Check accounts table structure
            accounts_columns = await self.get_table_columns("accounts")
            required_columns = ["id", "user_id", "name", "current_cash", "is_active"]

            for col in required_columns:
                if col not in accounts_columns:
                    print(f"  ❌ Missing column in accounts table: {col}")
                    return False

            print("  ✅ Schema consistent - all critical tables and columns present")
            return True

        except Exception as e:
            print(f"  ❌ Schema consistency check failed: {e}")
            return False

    async def run_validation(self) -> dict[str, bool]:
        """Run full validation suite.

        Returns:
            Dict with test results
        """
        print("=" * 80)
        print(f"Alembic Migration Validation (T073) - {self.get_db_type()}")
        print("=" * 80)
        print(f"\nDatabase URL: {self.database_url}")

        await self.initialize_engine()

        results = {}

        try:
            # Run tests
            results["upgrade_to_head"] = await self.test_upgrade_to_head()
            results["schema_consistency"] = await self.test_schema_consistency()
            results["downgrade_to_base"] = await self.test_downgrade_to_base()
            results["full_cycle"] = await self.test_upgrade_downgrade_cycle()

        finally:
            await self.close_engine()

        # Print summary
        print("\n" + "=" * 80)
        print("VALIDATION SUMMARY")
        print("=" * 80)

        passed = sum(1 for v in results.values() if v)
        total = len(results)

        for test_name, passed_test in results.items():
            status = "✅ PASS" if passed_test else "❌ FAIL"
            print(f"  {status} {test_name}")

        print("\n" + "=" * 80)
        print(f"Results: {passed}/{total} tests passed on {self.get_db_type()}")

        if passed == total:
            print(
                f"✅ ALL TESTS PASSED - Alembic migrations work correctly on {self.get_db_type()}"
            )
        else:
            print(f"❌ {total - passed} TESTS FAILED - Review failures above")

        print("=" * 80 + "\n")

        return results


async def main():
    """Run Alembic validation."""
    db_url = os.getenv("DATABASE_URL")

    if not db_url:
        print("❌ ERROR: DATABASE_URL environment variable not set")
        print("\nUsage:")
        print("  # Test on SQLite")
        print("  DATABASE_URL=sqlite+aiosqlite:///./test_alembic.db \\")
        print("    python backend/scripts/testing/validate_alembic.py")
        print("\n  # Test on PostgreSQL")
        print(
            "  DATABASE_URL=postgresql+asyncpg://trader:trader_password@localhost:5432/test_alembic \\"
        )
        print("    python backend/scripts/testing/validate_alembic.py")
        return 1

    validator = AlembicValidator(db_url)
    results = await validator.run_validation()

    # Return exit code
    all_passed = all(results.values())
    return 0 if all_passed else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
