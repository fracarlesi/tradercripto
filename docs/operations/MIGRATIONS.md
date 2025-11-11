# Manual Rollback Scripts

This directory contains manual rollback scripts for complex migration scenarios that require data transformations or special handling.

## When to Use Manual Rollback

Use these scripts when:

1. **Data transformations**: Migration involves restructuring data that can't be automatically reversed
2. **Multiple step dependencies**: Migration depends on external services or multiple coordinated changes
3. **Production data preservation**: Need to preserve production data during rollback
4. **Complex business logic**: Rollback requires business logic that can't be expressed in SQL DDL

## Automatic vs Manual Rollback

### Automatic Rollback (Alembic)

**Use for**:
- Schema changes only (add/drop tables, columns, indexes)
- Simple data migrations with clear reverse operations
- Development and staging environments

**Command**:
```bash
alembic downgrade -1  # Rollback one version
alembic downgrade <revision>  # Rollback to specific version
```

### Manual Rollback (These Scripts)

**Use for**:
- Production deployments with data transformations
- Migrations that split or merge tables
- Migrations with external dependencies (e.g., API changes)
- Scenarios requiring manual verification before proceeding

## Script Naming Convention

```
YYYYMMDD_HHMM_<revision_id>_<description>_rollback.py
```

Example: `20251031_1751_f41a369f1467_initial_schema_rollback.py`

## Script Template

Each rollback script should follow this template:

```python
"""Manual rollback for: <migration description>

Revision ID: <alembic_revision_id>
Migration Applied: <date>
Rollback Reason: <why manual rollback is needed>
"""

import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from config.settings import settings


async def backup_data(session: AsyncSession) -> dict:
    """Backup critical data before rollback."""
    print("Step 1: Backing up data...")
    backup = {}

    # Example: Backup accounts table
    result = await session.execute(text("SELECT * FROM accounts"))
    backup['accounts'] = [dict(row._mapping) for row in result]

    print(f"  ✓ Backed up {len(backup['accounts'])} accounts")
    return backup


async def verify_prerequisites(session: AsyncSession) -> bool:
    """Verify system is ready for rollback."""
    print("Step 2: Verifying prerequisites...")

    # Check no active connections
    result = await session.execute(text("SELECT COUNT(*) FROM accounts WHERE is_active = true"))
    active_accounts = result.scalar()

    if active_accounts > 0:
        print(f"  ⚠️  Warning: {active_accounts} active accounts found")
        response = input("    Continue anyway? (yes/no): ")
        if response.lower() != 'yes':
            return False

    print("  ✓ Prerequisites verified")
    return True


async def perform_rollback(session: AsyncSession, backup: dict) -> bool:
    """Perform the actual rollback operations."""
    print("Step 3: Performing rollback...")

    try:
        # Run Alembic downgrade
        import subprocess
        result = subprocess.run(
            ["alembic", "downgrade", "-1"],
            capture_output=True,
            text=True,
            check=True
        )
        print("  ✓ Alembic downgrade completed")

        # Additional manual steps if needed
        # await session.execute(text("...custom SQL..."))
        # await session.commit()

        return True

    except Exception as e:
        print(f"  ✗ Rollback failed: {e}")
        return False


async def verify_rollback(session: AsyncSession, backup: dict) -> bool:
    """Verify rollback completed successfully."""
    print("Step 4: Verifying rollback...")

    # Check tables no longer exist
    try:
        await session.execute(text("SELECT 1 FROM accounts LIMIT 1"))
        print("  ✗ Verification failed: accounts table still exists")
        return False
    except Exception:
        print("  ✓ Schema rollback verified")

    # Verify alembic version
    result = await session.execute(text("SELECT version_num FROM alembic_version"))
    version = result.scalar()
    print(f"  ✓ Current version: {version}")

    return True


async def restore_if_needed(session: AsyncSession, backup: dict):
    """Restore data if rollback failed."""
    print("Step 5: Checking if restore needed...")

    response = input("Restore backed up data? (yes/no): ")
    if response.lower() == 'yes':
        # Restore logic here
        print("  ✓ Data restored from backup")
    else:
        print("  → Backup saved for manual review")


async def main():
    """Execute manual rollback with safety checks."""
    print("=" * 60)
    print("MANUAL ROLLBACK SCRIPT")
    print("Migration: <description>")
    print("Revision: <revision_id>")
    print("=" * 60)
    print()

    # Confirm execution
    response = input("This will rollback the database schema. Continue? (yes/no): ")
    if response.lower() != 'yes':
        print("Rollback cancelled.")
        return

    # Create database connection
    engine = create_async_engine(settings.database_url)
    AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with AsyncSessionLocal() as session:
        try:
            # Step 1: Backup
            backup = await backup_data(session)

            # Step 2: Prerequisites
            if not await verify_prerequisites(session):
                print("\n✗ Prerequisites check failed. Rollback cancelled.")
                return

            # Step 3: Rollback
            success = await perform_rollback(session, backup)

            # Step 4: Verify
            if success:
                verified = await verify_rollback(session, backup)
                if verified:
                    print("\n✓ Rollback completed successfully!")
                else:
                    print("\n⚠️  Rollback completed but verification failed.")
                    await restore_if_needed(session, backup)
            else:
                print("\n✗ Rollback failed!")
                await restore_if_needed(session, backup)

        except Exception as e:
            print(f"\n✗ Fatal error during rollback: {e}")
            print("Attempting to restore backup...")
            await restore_if_needed(session, backup)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
```

## Using Manual Rollback Scripts

### 1. Before Migration

```bash
# Create rollback script for new migration
cp rollback_scripts/README.md rollback_scripts/$(date +%Y%m%d_%H%M)_<revision>_<desc>_rollback.py
# Edit script with migration-specific logic
```

### 2. After Migration (If Needed)

```bash
# Run manual rollback
cd backend
python alembic/rollback_scripts/20251031_1751_f41a369f1467_initial_schema_rollback.py
```

### 3. Verify Result

```bash
# Check current migration version
alembic current

# Verify data integrity
alembic history
```

## Production Rollback Checklist

- [ ] **Backup database** using `backup_db.sh` script
- [ ] **Stop application** to prevent new writes during rollback
- [ ] **Notify users** of maintenance window
- [ ] **Run manual rollback script** with dry-run mode first (if available)
- [ ] **Verify rollback** using verification queries
- [ ] **Restart application** and check health endpoints
- [ ] **Monitor logs** for errors or warnings
- [ ] **Verify business functionality** with manual testing
- [ ] **Document incident** including rollback reason and steps taken

## Initial Schema Rollback

For the initial schema migration (f41a369f1467), automatic rollback is sufficient:

```bash
alembic downgrade -1
```

No manual rollback script needed because:
- No data transformations involved
- Simple schema creation/deletion
- No external dependencies
- Safe for automated rollback

## Future Migrations

Create manual rollback scripts for:

1. **Column renames with data**: Preserve data during rename operations
2. **Table splits/merges**: Redistribute data correctly
3. **Foreign key changes**: Handle cascades and orphaned records
4. **Data type conversions**: Handle precision loss or invalid conversions
5. **Constraint additions**: Handle existing data that violates new constraints

## Emergency Rollback Procedure

If migration fails in production:

1. **Immediate**: Stop application
2. **Assess**: Check error logs and database state
3. **Decide**: Automatic rollback vs manual intervention
4. **Execute**: Run appropriate rollback (Alembic or manual script)
5. **Verify**: Run verification queries
6. **Restore**: Start application and monitor
7. **Review**: Post-incident analysis

## Testing Rollback Scripts

Always test rollback scripts before production use:

```bash
# 1. Clone production data to staging
pg_dump production | psql staging

# 2. Run migration on staging
alembic upgrade head

# 3. Test automatic rollback
alembic downgrade -1

# 4. Re-apply migration
alembic upgrade head

# 5. Test manual rollback script
python alembic/rollback_scripts/<script_name>

# 6. Verify data integrity
# Run validation queries
```

## Rollback Script Maintenance

- **Review monthly**: Ensure scripts compatible with current codebase
- **Test quarterly**: Run rollback tests on staging environment
- **Update documentation**: Keep procedure steps current
- **Version control**: Track all rollback scripts in git
- **Archive old scripts**: Keep for historical reference but mark as deprecated

## Support

For questions or issues with rollback procedures:

1. Check this README and script comments
2. Review Alembic documentation: https://alembic.sqlalchemy.org/
3. Consult database administrator
4. Review incident logs for similar past rollbacks
