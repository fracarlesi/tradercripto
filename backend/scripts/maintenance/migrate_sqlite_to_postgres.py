"""
Migrate data from SQLite to PostgreSQL.

This script:
1. Connects to the SQLite database
2. Creates all tables in PostgreSQL using SQLAlchemy models
3. Copies all data from SQLite to PostgreSQL
4. Handles primary key sequences for PostgreSQL

Usage:
    # From inside Docker container
    python scripts/maintenance/migrate_sqlite_to_postgres.py

    # Or from host (requires psycopg2-binary installed)
    SQLITE_PATH=/app/data/data.db POSTGRES_URL=postgresql://trader:trader_secure_pwd_2024@postgres:5432/trader_db python scripts/maintenance/migrate_sqlite_to_postgres.py
"""

import os
import sys
import sqlite3
from datetime import datetime

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import psycopg2
from psycopg2.extras import execute_values


def get_sqlite_tables(sqlite_conn):
    """Get list of tables from SQLite database."""
    cursor = sqlite_conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    tables = [row[0] for row in cursor.fetchall()]
    cursor.close()
    return tables


def get_table_columns(sqlite_conn, table_name):
    """Get column names for a table."""
    cursor = sqlite_conn.cursor()
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = [row[1] for row in cursor.fetchall()]
    cursor.close()
    return columns


def get_table_data(sqlite_conn, table_name):
    """Get all data from a SQLite table."""
    cursor = sqlite_conn.cursor()
    cursor.execute(f"SELECT * FROM {table_name}")
    data = cursor.fetchall()
    cursor.close()
    return data


def create_postgres_tables(pg_conn):
    """Create tables in PostgreSQL using SQLAlchemy models."""
    from database.models import Base
    from sqlalchemy import create_engine

    # Get PostgreSQL URL from environment
    postgres_url = os.getenv(
        'POSTGRES_URL',
        'postgresql://trader:trader_secure_pwd_2024@postgres:5432/trader_db'
    )

    # Create engine and tables
    engine = create_engine(postgres_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    print("Created all tables in PostgreSQL")


def migrate_table(sqlite_conn, pg_conn, table_name, columns):
    """Migrate a single table from SQLite to PostgreSQL."""
    data = get_table_data(sqlite_conn, table_name)

    if not data:
        print(f"  {table_name}: 0 rows (empty)")
        return 0

    # Build INSERT query
    placeholders = ', '.join(['%s'] * len(columns))
    columns_str = ', '.join([f'"{col}"' for col in columns])

    cursor = pg_conn.cursor()

    # Truncate existing data (if any)
    cursor.execute(f'TRUNCATE TABLE "{table_name}" CASCADE')

    # Insert data in batches
    batch_size = 1000
    total_inserted = 0

    for i in range(0, len(data), batch_size):
        batch = data[i:i + batch_size]

        # Convert any datetime strings and boolean values to proper format
        converted_batch = []
        for row in batch:
            converted_row = []
            for idx, val in enumerate(row):
                col_name = columns[idx] if idx < len(columns) else ''

                # Convert SQLite integers (0/1) to PostgreSQL booleans for boolean columns
                if col_name in ('is_active', 'is_synced', 'is_filled', 'is_canceled',
                               'is_open', 'is_closed', 'reduce_only', 'is_testnet', 'executed'):
                    if val == 1:
                        converted_row.append(True)
                    elif val == 0:
                        converted_row.append(False)
                    else:
                        converted_row.append(val)
                elif val is not None and isinstance(val, str):
                    # Try to parse as datetime
                    try:
                        if 'T' in val or ' ' in val:
                            # Check for ISO format
                            if '.' in val:
                                dt = datetime.fromisoformat(val.replace('Z', '+00:00'))
                            else:
                                dt = datetime.fromisoformat(val)
                            converted_row.append(dt)
                        else:
                            converted_row.append(val)
                    except ValueError:
                        converted_row.append(val)
                else:
                    converted_row.append(val)
            converted_batch.append(tuple(converted_row))

        # Use execute_values for better performance
        insert_query = f'INSERT INTO "{table_name}" ({columns_str}) VALUES %s'
        execute_values(cursor, insert_query, converted_batch)
        total_inserted += len(batch)

    pg_conn.commit()
    cursor.close()

    print(f"  {table_name}: {total_inserted} rows migrated")
    return total_inserted


def reset_sequences(pg_conn, tables):
    """Reset PostgreSQL sequences to max ID + 1 for each table with 'id' column."""
    cursor = pg_conn.cursor()

    for table in tables:
        # Check if table has 'id' column
        cursor.execute(f"""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = '{table}' AND column_name = 'id'
        """)

        if cursor.fetchone():
            # Get max ID
            cursor.execute(f'SELECT COALESCE(MAX(id), 0) FROM "{table}"')
            max_id = cursor.fetchone()[0]

            # Reset sequence
            seq_name = f"{table}_id_seq"
            cursor.execute(f"SELECT setval('{seq_name}', {max_id + 1}, false)")
            print(f"  Reset sequence {seq_name} to {max_id + 1}")

    pg_conn.commit()
    cursor.close()


def main():
    print("=" * 60)
    print("SQLite to PostgreSQL Migration")
    print("=" * 60)

    # Get paths from environment or use defaults
    sqlite_path = os.getenv('SQLITE_PATH', '/app/data/data.db')
    postgres_url = os.getenv(
        'POSTGRES_URL',
        'postgresql://trader:trader_secure_pwd_2024@postgres:5432/trader_db'
    )

    # Parse PostgreSQL URL for psycopg2
    # Format: postgresql://user:password@host:port/database
    import re
    match = re.match(r'postgresql://([^:]+):([^@]+)@([^:]+):(\d+)/(.+)', postgres_url)
    if not match:
        print(f"Invalid PostgreSQL URL: {postgres_url}")
        sys.exit(1)

    pg_user, pg_password, pg_host, pg_port, pg_database = match.groups()

    print(f"\nSource: {sqlite_path}")
    print(f"Target: postgresql://{pg_user}:***@{pg_host}:{pg_port}/{pg_database}")

    # Check if SQLite file exists
    if not os.path.exists(sqlite_path):
        print(f"\nError: SQLite database not found at {sqlite_path}")
        sys.exit(1)

    # Connect to SQLite
    print("\n1. Connecting to SQLite...")
    sqlite_conn = sqlite3.connect(sqlite_path)

    # Get tables
    tables = get_sqlite_tables(sqlite_conn)
    print(f"   Found {len(tables)} tables: {', '.join(tables)}")

    # Connect to PostgreSQL
    print("\n2. Connecting to PostgreSQL...")
    try:
        pg_conn = psycopg2.connect(
            host=pg_host,
            port=pg_port,
            user=pg_user,
            password=pg_password,
            database=pg_database
        )
        pg_conn.autocommit = False
    except psycopg2.OperationalError as e:
        print(f"   Error connecting to PostgreSQL: {e}")
        sys.exit(1)

    # Create tables in PostgreSQL
    print("\n3. Creating tables in PostgreSQL...")
    create_postgres_tables(pg_conn)

    # Migrate each table
    print("\n4. Migrating data...")
    total_rows = 0

    # Order tables to handle foreign key constraints
    # Tables with no foreign keys first
    table_order = [
        'users',  # Base table
        'accounts',  # Depends on users
        'positions',  # Depends on accounts
        'orders',  # Depends on accounts
        'trades',  # Depends on accounts
        'portfolio_snapshots',  # Depends on accounts
        'decision_snapshots',  # Depends on accounts
        'strategy_weights',  # Depends on accounts
        'indicator_weights',  # Depends on accounts
        'missed_opportunities_reports',  # Depends on accounts
        'trade_metadata',  # No foreign keys but related to trades
    ]

    # Add any tables not in the order list
    for table in tables:
        if table not in table_order:
            table_order.append(table)

    # Filter to only existing tables
    table_order = [t for t in table_order if t in tables]

    for table in table_order:
        columns = get_table_columns(sqlite_conn, table)
        rows = migrate_table(sqlite_conn, pg_conn, table, columns)
        total_rows += rows

    # Reset sequences
    print("\n5. Resetting sequences...")
    reset_sequences(pg_conn, table_order)

    # Close connections
    sqlite_conn.close()
    pg_conn.close()

    print("\n" + "=" * 60)
    print(f"Migration complete! Total rows migrated: {total_rows}")
    print("=" * 60)


if __name__ == '__main__':
    main()
