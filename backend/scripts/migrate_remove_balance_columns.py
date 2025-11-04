#!/usr/bin/env python3
"""
Database migration: Remove obsolete balance columns from accounts table.

Removes:
- initial_capital
- current_cash
- frozen_cash

These fields are now fetched in real-time from Hyperliquid API instead of being stored.

Usage:
    python scripts/migrate_remove_balance_columns.py
"""

import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


def main():
    # Get database path
    backend_dir = Path(__file__).parent.parent
    db_path = backend_dir / "data.db"

    if not db_path.exists():
        print(f"❌ Database not found at {db_path}")
        print("   This script is for SQLite databases only.")
        return

    # Create backup
    backup_path = backend_dir / f"data.db.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    print(f"📦 Creating backup at {backup_path}...")
    shutil.copy2(db_path, backup_path)
    print(f"✅ Backup created")

    # Connect to database
    print(f"\n🔧 Connecting to database...")
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    try:
        # Check if columns exist
        cursor.execute("PRAGMA table_info(accounts)")
        columns = {row[1] for row in cursor.fetchall()}

        columns_to_remove = ["initial_capital", "current_cash", "frozen_cash"]
        existing_columns = [col for col in columns_to_remove if col in columns]

        if not existing_columns:
            print("\n✅ All columns already removed. Database is up to date.")
            return

        print(f"\n📋 Found columns to remove: {', '.join(existing_columns)}")

        # SQLite doesn't support ALTER TABLE DROP COLUMN directly
        # We need to recreate the table
        print("\n🔄 Recreating accounts table without obsolete columns...")

        # Get current schema
        cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='accounts'")
        create_table_sql = cursor.fetchone()[0]
        print(f"\n   Original schema:")
        print(f"   {create_table_sql[:100]}...")

        # Start transaction
        cursor.execute("BEGIN TRANSACTION")

        # Create new table schema (without obsolete columns)
        cursor.execute("""
            CREATE TABLE accounts_new (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                version VARCHAR(100) DEFAULT 'v1' NOT NULL,
                name VARCHAR(100) NOT NULL,
                account_type VARCHAR(20) DEFAULT 'AI' NOT NULL,
                is_active BOOLEAN DEFAULT TRUE NOT NULL,
                model VARCHAR(100),
                base_url VARCHAR(500),
                api_key VARCHAR(500),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users (id)
            )
        """)

        # Copy data from old table to new (only non-obsolete columns)
        cursor.execute("""
            INSERT INTO accounts_new
            (id, user_id, version, name, account_type, is_active, model, base_url, api_key, created_at, updated_at)
            SELECT id, user_id, version, name, account_type, is_active, model, base_url, api_key, created_at, updated_at
            FROM accounts
        """)

        # Drop old table
        cursor.execute("DROP TABLE accounts")

        # Rename new table
        cursor.execute("ALTER TABLE accounts_new RENAME TO accounts")

        # Recreate indexes
        cursor.execute(
            "CREATE INDEX idx_accounts_user_active ON accounts (user_id, is_active)"
        )
        cursor.execute("CREATE INDEX idx_accounts_type ON accounts (account_type)")

        # Commit transaction
        conn.commit()

        print("\n✅ Migration completed successfully!")
        print(f"\n📊 Summary:")
        print(f"   - Removed columns: {', '.join(existing_columns)}")
        print(f"   - Backup saved to: {backup_path}")
        print(
            f"   - All account balance data will now be fetched from Hyperliquid API"
        )

    except Exception as e:
        conn.rollback()
        print(f"\n❌ Migration failed: {e}")
        print(f"\n🔄 Database rolled back. You can restore from backup if needed:")
        print(f"   cp {backup_path} {db_path}")
        raise

    finally:
        conn.close()


if __name__ == "__main__":
    main()
