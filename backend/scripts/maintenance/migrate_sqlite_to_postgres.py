"""SQLite to PostgreSQL Migration Script (T068-T070).

This script migrates data from SQLite to PostgreSQL with:
- Data export from SQLite (T068)
- Data import to PostgreSQL (T068)
- Data integrity checks (T069)
- Pre-migration backup (T070)

Usage:
    # Migrate with default URLs from environment
    python backend/scripts/maintenance/migrate_sqlite_to_postgres.py

    # Migrate with custom URLs
    python backend/scripts/maintenance/migrate_sqlite_to_postgres.py \
        --sqlite-url "sqlite+aiosqlite:///./data/trader.db" \
        --postgres-url "postgresql+asyncpg://trader:password@localhost:5432/trader_db"
"""

import argparse
import asyncio
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from config.logging import get_logger
from database.models import (
    Account,
    AIDecisionLog,
    CryptoKline,
    CryptoPrice,
    Order,
    Position,
    SystemConfig,
    Trade,
    TradingConfig,
    User,
)
from sqlalchemy import func, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

logger = get_logger(__name__)

# List of tables to migrate in correct order (respecting foreign keys)
TABLES_TO_MIGRATE = [
    User,
    TradingConfig,
    SystemConfig,
    Account,
    Position,
    Order,
    Trade,
    CryptoPrice,
    CryptoKline,
    AIDecisionLog,
]


class MigrationBackup:
    """Handles pre-migration backup (T070)."""

    def __init__(self, sqlite_path: str):
        """Initialize backup handler.

        Args:
            sqlite_path: Path to SQLite database file
        """
        self.sqlite_path = Path(sqlite_path)
        self.backup_dir = Path("./data/backups")

    def create_backup(self) -> Path:
        """Create timestamped backup of SQLite database.

        Returns:
            Path to backup file

        Raises:
            FileNotFoundError: If SQLite database doesn't exist
        """
        if not self.sqlite_path.exists():
            raise FileNotFoundError(f"SQLite database not found: {self.sqlite_path}")

        # Create backup directory
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        # Generate timestamped backup name
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = self.backup_dir / f"sqlite_backup_{timestamp}.db"

        # Copy database
        shutil.copy2(self.sqlite_path, backup_path)

        logger.info(
            "Created backup",
            extra={
                "context": {
                    "source": str(self.sqlite_path),
                    "backup": str(backup_path),
                    "size_bytes": backup_path.stat().st_size,
                }
            },
        )

        return backup_path


class DataIntegrityChecker:
    """Handles data integrity validation (T069)."""

    @staticmethod
    async def check_row_counts(
        sqlite_session: AsyncSession, postgres_session: AsyncSession
    ) -> dict[str, dict[str, int]]:
        """Compare row counts between databases.

        Args:
            sqlite_session: SQLite session
            postgres_session: PostgreSQL session

        Returns:
            Dict with table names and counts
        """
        results = {}

        for model in TABLES_TO_MIGRATE:
            table_name = model.__tablename__

            # Count in SQLite
            result = await sqlite_session.execute(select(func.count()).select_from(model))
            sqlite_count = result.scalar() or 0

            # Count in PostgreSQL
            result = await postgres_session.execute(select(func.count()).select_from(model))
            postgres_count = result.scalar() or 0

            results[table_name] = {
                "sqlite": sqlite_count,
                "postgres": postgres_count,
                "match": sqlite_count == postgres_count,
            }

        return results

    @staticmethod
    async def verify_foreign_keys(
        postgres_session: AsyncSession,
    ) -> dict[str, bool]:
        """Verify foreign key constraints in PostgreSQL.

        Args:
            postgres_session: PostgreSQL session

        Returns:
            Dict with table names and validation status
        """
        results = {}

        # Check User -> Account relationship
        result = await postgres_session.execute(
            text("""
                SELECT COUNT(*) FROM accounts a
                LEFT JOIN users u ON a.user_id = u.id
                WHERE u.id IS NULL
            """)
        )
        invalid_accounts = result.scalar() or 0
        results["accounts.user_id"] = invalid_accounts == 0

        # Check Account -> Position relationship
        result = await postgres_session.execute(
            text("""
                SELECT COUNT(*) FROM positions p
                LEFT JOIN accounts a ON p.account_id = a.id
                WHERE a.id IS NULL
            """)
        )
        invalid_positions = result.scalar() or 0
        results["positions.account_id"] = invalid_positions == 0

        # Check Account -> Order relationship
        result = await postgres_session.execute(
            text("""
                SELECT COUNT(*) FROM orders o
                LEFT JOIN accounts a ON o.account_id = a.id
                WHERE a.id IS NULL
            """)
        )
        invalid_orders = result.scalar() or 0
        results["orders.account_id"] = invalid_orders == 0

        # Check Order -> Trade relationship
        result = await postgres_session.execute(
            text("""
                SELECT COUNT(*) FROM trades t
                LEFT JOIN orders o ON t.order_id = o.id
                WHERE o.id IS NULL AND t.order_id IS NOT NULL
            """)
        )
        invalid_trades = result.scalar() or 0
        results["trades.order_id"] = invalid_trades == 0

        return results

    @staticmethod
    async def verify_unique_constraints(
        postgres_session: AsyncSession,
    ) -> dict[str, bool]:
        """Verify unique constraints are maintained.

        Args:
            postgres_session: PostgreSQL session

        Returns:
            Dict with constraint names and validation status
        """
        results = {}

        # Check unique usernames
        result = await postgres_session.execute(
            text("""
                SELECT COUNT(*) - COUNT(DISTINCT username) as duplicates
                FROM users
            """)
        )
        duplicates = result.scalar() or 0
        results["users.username_unique"] = duplicates == 0

        # Check unique order numbers
        result = await postgres_session.execute(
            text("""
                SELECT COUNT(*) - COUNT(DISTINCT order_no) as duplicates
                FROM orders
            """)
        )
        duplicates = result.scalar() or 0
        results["orders.order_no_unique"] = duplicates == 0

        return results


class MigrationManager:
    """Manages SQLite to PostgreSQL migration (T068)."""

    def __init__(self, sqlite_url: str, postgres_url: str):
        """Initialize migration manager.

        Args:
            sqlite_url: SQLite connection URL
            postgres_url: PostgreSQL connection URL
        """
        self.sqlite_url = sqlite_url
        self.postgres_url = postgres_url
        self.sqlite_engine = None
        self.postgres_engine = None
        self.integrity_checker = DataIntegrityChecker()

    async def initialize_engines(self):
        """Initialize database engines."""
        self.sqlite_engine = create_async_engine(self.sqlite_url, echo=False)
        self.postgres_engine = create_async_engine(
            self.postgres_url, echo=False, pool_pre_ping=True
        )

        logger.info("Database engines initialized")

    async def close_engines(self):
        """Close database engines."""
        if self.sqlite_engine:
            await self.sqlite_engine.dispose()
        if self.postgres_engine:
            await self.postgres_engine.dispose()

        logger.info("Database engines closed")

    async def export_sqlite_data(self, sqlite_session: AsyncSession) -> dict[str, list[dict]]:
        """Export data from SQLite database.

        Args:
            sqlite_session: SQLite session

        Returns:
            Dict mapping table names to list of row dicts
        """
        exported_data = {}

        for model in TABLES_TO_MIGRATE:
            table_name = model.__tablename__

            # Query all rows
            result = await sqlite_session.execute(select(model))
            rows = result.scalars().all()

            # Convert to dict
            exported_data[table_name] = []
            inspector = inspect(model)

            for row in rows:
                row_dict = {}
                for column in inspector.columns:
                    value = getattr(row, column.key)
                    # Convert datetime to ISO format string
                    if isinstance(value, datetime):
                        value = value.isoformat()
                    row_dict[column.key] = value
                exported_data[table_name].append(row_dict)

            logger.info(f"Exported {len(exported_data[table_name])} rows from {table_name}")

        return exported_data

    async def import_to_postgres(self, postgres_session: AsyncSession, data: dict[str, list[dict]]):
        """Import data to PostgreSQL database.

        Args:
            postgres_session: PostgreSQL session
            data: Exported data from SQLite
        """
        for model in TABLES_TO_MIGRATE:
            table_name = model.__tablename__
            rows = data.get(table_name, [])

            if not rows:
                logger.info(f"No data to import for {table_name}")
                continue

            # Clear existing data
            await postgres_session.execute(text(f"TRUNCATE TABLE {table_name} CASCADE"))

            # Insert rows
            for row_dict in rows:
                # Convert ISO strings back to datetime
                for key, value in row_dict.items():
                    if isinstance(value, str) and "T" in value:
                        try:
                            row_dict[key] = datetime.fromisoformat(value)
                        except (ValueError, AttributeError):
                            pass

                instance = model(**row_dict)
                postgres_session.add(instance)

            await postgres_session.flush()
            logger.info(f"Imported {len(rows)} rows to {table_name}")

        await postgres_session.commit()

    async def verify_migration(
        self, sqlite_session: AsyncSession, postgres_session: AsyncSession
    ) -> dict[str, Any]:
        """Verify migration integrity.

        Args:
            sqlite_session: SQLite session
            postgres_session: PostgreSQL session

        Returns:
            Verification results
        """
        logger.info("Verifying migration integrity...")

        # Check row counts
        row_counts = await self.integrity_checker.check_row_counts(sqlite_session, postgres_session)

        # Check foreign keys
        foreign_keys = await self.integrity_checker.verify_foreign_keys(postgres_session)

        # Check unique constraints
        unique_constraints = await self.integrity_checker.verify_unique_constraints(
            postgres_session
        )

        return {
            "row_counts": row_counts,
            "foreign_keys": foreign_keys,
            "unique_constraints": unique_constraints,
        }

    async def run_migration(self) -> bool:
        """Execute full migration process.

        Returns:
            True if migration successful, False otherwise
        """
        try:
            # Initialize engines
            await self.initialize_engines()

            # Create sessions
            sqlite_session_factory = sessionmaker(
                self.sqlite_engine, class_=AsyncSession, expire_on_commit=False
            )
            postgres_session_factory = sessionmaker(
                self.postgres_engine, class_=AsyncSession, expire_on_commit=False
            )

            async with sqlite_session_factory() as sqlite_session:
                async with postgres_session_factory() as postgres_session:
                    # Export data
                    logger.info("Exporting data from SQLite...")
                    data = await self.export_sqlite_data(sqlite_session)

                    # Import data
                    logger.info("Importing data to PostgreSQL...")
                    await self.import_to_postgres(postgres_session, data)

                    # Verify migration
                    verification = await self.verify_migration(sqlite_session, postgres_session)

                    # Print verification results
                    print("\n" + "=" * 80)
                    print("MIGRATION VERIFICATION RESULTS")
                    print("=" * 80)

                    print("\nRow Count Comparison:")
                    all_counts_match = True
                    for table, counts in verification["row_counts"].items():
                        status = "✅" if counts["match"] else "❌"
                        print(
                            f"  {status} {table}: SQLite={counts['sqlite']}, "
                            f"PostgreSQL={counts['postgres']}"
                        )
                        if not counts["match"]:
                            all_counts_match = False

                    print("\nForeign Key Validation:")
                    all_fk_valid = True
                    for fk, valid in verification["foreign_keys"].items():
                        status = "✅" if valid else "❌"
                        print(f"  {status} {fk}")
                        if not valid:
                            all_fk_valid = False

                    print("\nUnique Constraint Validation:")
                    all_unique_valid = True
                    for constraint, valid in verification["unique_constraints"].items():
                        status = "✅" if valid else "❌"
                        print(f"  {status} {constraint}")
                        if not valid:
                            all_unique_valid = False

                    success = all_counts_match and all_fk_valid and all_unique_valid

                    print("\n" + "=" * 80)
                    if success:
                        print("✅ MIGRATION SUCCESSFUL - All checks passed!")
                    else:
                        print("❌ MIGRATION FAILED - Some checks failed")
                    print("=" * 80 + "\n")

                    return success

        except Exception as e:
            logger.error(
                "Migration failed",
                extra={"context": {"error": str(e)}},
                exc_info=True,
            )
            return False
        finally:
            await self.close_engines()


async def main():
    """Run migration with CLI arguments."""
    parser = argparse.ArgumentParser(description="Migrate data from SQLite to PostgreSQL")
    parser.add_argument(
        "--sqlite-url",
        default="sqlite+aiosqlite:///./data/trader.db",
        help="SQLite connection URL",
    )
    parser.add_argument(
        "--postgres-url",
        default="postgresql+asyncpg://trader:trader_password@localhost:5432/trader_db",
        help="PostgreSQL connection URL",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip pre-migration backup",
    )

    args = parser.parse_args()

    print("=" * 80)
    print("SQLite to PostgreSQL Migration Script (T068-T070)")
    print("=" * 80)
    print(f"\nSQLite URL: {args.sqlite_url}")
    print(f"PostgreSQL URL: {args.postgres_url}")
    print()

    # Create backup (T070)
    if not args.no_backup:
        try:
            # Extract file path from SQLite URL
            sqlite_path = args.sqlite_url.replace("sqlite+aiosqlite:///", "").replace("./", "")
            backup = MigrationBackup(sqlite_path)
            backup_path = backup.create_backup()
            print(f"✅ Backup created: {backup_path}\n")
        except Exception as e:
            print(f"⚠️  Backup failed: {e}")
            print("   Continuing without backup...\n")

    # Run migration
    manager = MigrationManager(args.sqlite_url, args.postgres_url)
    success = await manager.run_migration()

    return 0 if success else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
