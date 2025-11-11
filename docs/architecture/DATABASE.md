# Database Documentation (T071)

## SQLite vs PostgreSQL Feature Differences

This document outlines the key differences between SQLite and PostgreSQL as used in this project, ensuring compatibility across both environments.

---

## Summary Table

| Feature | SQLite | PostgreSQL | Impact |
|---------|--------|------------|--------|
| **DECIMAL Storage** | Stored as TEXT | Native DECIMAL type | Explicit conversion needed |
| **DATETIME Format** | ISO-8601 strings | Native TIMESTAMP | Compatible with SQLAlchemy |
| **Boolean Type** | INTEGER (0/1) | Native BOOLEAN | String "true"/"false" used |
| **Auto-increment** | `AUTOINCREMENT` | `SERIAL` or `IDENTITY` | Handled by SQLAlchemy |
| **Foreign Keys** | Disabled by default | Always enforced | Explicit PRAGMA needed for SQLite |
| **JSONB Support** | JSON1 extension | Native JSONB type | Not currently used |
| **Concurrency** | File-level locking | Row-level locking | PostgreSQL better for concurrent writes |
| **Connection Pooling** | Single file | Full pooling support | AsyncEngine pool configured |

---

## Detailed Differences

### 1. DECIMAL Storage

**SQLite:**
- No native DECIMAL type
- Stored as TEXT or REAL
- Potential precision loss with REAL

**PostgreSQL:**
- Native DECIMAL/NUMERIC type
- Exact precision maintained
- No rounding errors

**Our Approach:**
```python
from decimal import Decimal
from sqlalchemy import Column, Numeric

# Model definition (works on both)
current_cash = Column(Numeric(precision=20, scale=8), nullable=False)

# Always use Decimal for monetary values
account.current_cash = Decimal("1000.50")
```

---

### 2. DATETIME Format

**SQLite:**
- Stores as ISO-8601 text strings
- Example: `"2025-10-31T15:30:45.123456"`

**PostgreSQL:**
- Native TIMESTAMP type
- Timezone-aware available with TIMESTAMPTZ

**Our Approach:**
```python
from datetime import datetime, UTC
from sqlalchemy import Column, DateTime

# Model definition
created_at = Column(DateTime, nullable=False, default=datetime.now(UTC))

# Both databases handle datetime objects correctly via SQLAlchemy
```

---

### 3. Boolean Type

**SQLite:**
- No native BOOLEAN
- Uses INTEGER (0 = false, 1 = true)

**PostgreSQL:**
- Native BOOLEAN type

**Our Approach:**
```python
# We use string "true"/"false" for consistency
is_active = Column(String(5), nullable=False, default="true")

# Comparison
if account.is_active == "true":
    # Active account
```

**Note:** This is a legacy decision. For new models, consider using proper Boolean with SQLAlchemy's Boolean type.

---

### 4. Auto-increment Syntax

**SQLite:**
```sql
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT
);
```

**PostgreSQL:**
```sql
CREATE TABLE users (
    id SERIAL PRIMARY KEY
);
-- or modern approach:
id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY
```

**Our Approach:**
```python
from sqlalchemy import Column, Integer

# SQLAlchemy handles this automatically
id = Column(Integer, primary_key=True, autoincrement=True)
```

---

### 5. Foreign Key Enforcement

**SQLite:**
- Foreign keys disabled by default
- Must enable with: `PRAGMA foreign_keys = ON`

**PostgreSQL:**
- Foreign keys always enforced
- Cannot be disabled

**Our Approach:**
```python
# In backend/database/connection.py
from sqlalchemy import event
from sqlalchemy.engine import Engine

@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_conn, connection_record):
    if "sqlite" in str(dbapi_conn):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
```

---

### 6. JSONB Support

**SQLite:**
- JSON1 extension provides JSON functions
- No native JSONB indexing

**PostgreSQL:**
- Native JSONB type with indexing
- GIN indexes for fast JSON queries

**Our Usage:**
- Not currently using JSONB in models
- Future consideration for flexible metadata storage

---

### 7. Concurrency and Locking

**SQLite:**
- **File-level locking**
- One writer at a time
- Multiple readers allowed
- Good for: development, low-traffic applications

**PostgreSQL:**
- **Row-level locking**
- MVCC (Multi-Version Concurrency Control)
- Multiple concurrent writers
- Good for: production, high-traffic applications

**Performance Comparison:**

| Scenario | SQLite | PostgreSQL |
|----------|--------|------------|
| Single user | Fast | Fast |
| 10 concurrent reads | Fast | Fast |
| 10 concurrent writes | **Serialized** | **Parallel** |
| 100+ concurrent users | **Not recommended** | Excellent |

---

### 8. Connection Pooling

**SQLite:**
- Single file access
- No connection pooling benefits
- `NullPool` recommended for async

**PostgreSQL:**
- Full connection pooling support
- Configured in `backend/database/connection.py`:
  ```python
  engine = create_async_engine(
      DATABASE_URL,
      pool_size=10,        # Default connection pool
      max_overflow=5,      # Additional connections when pool exhausted
      pool_timeout=30,     # Wait time for available connection
      pool_pre_ping=True   # Verify connections before use
  )
  ```

---

## Migration Considerations

### When to Use SQLite

✅ **Good for:**
- Local development
- Testing
- Single-user applications
- Quick prototyping
- Low-traffic deployments (<10 concurrent users)

❌ **Not recommended for:**
- Production with multiple concurrent users
- Applications requiring high write throughput
- Distributed systems
- Long-running transactions with concurrent access

### When to Use PostgreSQL

✅ **Good for:**
- Production deployments
- Multiple concurrent users (10+)
- High-traffic applications
- Applications requiring advanced SQL features
- Systems with heavy write operations

---

## Testing Strategy

### Cross-Database Testing

Our test suite should run against both SQLite and PostgreSQL:

```bash
# Test with SQLite (default)
DATABASE_URL=sqlite+aiosqlite:///./test.db pytest

# Test with PostgreSQL
DATABASE_URL=postgresql+asyncpg://test:test@localhost:5432/test_db pytest
```

### Key Test Areas

1. **Decimal Precision**
   - Verify monetary calculations maintain precision
   - Test edge cases (very large/small numbers)

2. **Datetime Handling**
   - Verify timezone awareness
   - Test datetime comparisons and ranges

3. **Boolean Logic**
   - Test "true"/"false" string comparisons
   - Verify filtering and updates work consistently

4. **Foreign Key Constraints**
   - Test cascade deletes
   - Verify referential integrity

5. **Concurrent Operations**
   - Test simultaneous reads/writes
   - Verify transaction isolation

---

## Migration Guide

### SQLite → PostgreSQL

Use the provided migration script:

```bash
# Create backup and migrate
python backend/scripts/maintenance/migrate_sqlite_to_postgres.py \
    --sqlite-url "sqlite+aiosqlite:///./data/trader.db" \
    --postgres-url "postgresql+asyncpg://trader:password@localhost:5432/trader_db"
```

The script automatically:
- ✅ Creates timestamped backup
- ✅ Exports all data from SQLite
- ✅ Imports data to PostgreSQL
- ✅ Verifies row counts match
- ✅ Validates foreign key integrity
- ✅ Checks unique constraints

### Rollback Procedure

If migration fails:

1. Stop application
2. Restore from backup:
   ```bash
   cp data/backups/sqlite_backup_YYYYMMDD_HHMMSS.db data/trader.db
   ```
3. Update `.env` to use SQLite URL
4. Restart application

---

## Alembic Migrations

### Creating Migrations

```bash
# Auto-generate migration
alembic revision --autogenerate -m "Description"

# Review generated migration in alembic/versions/

# Apply migration
alembic upgrade head
```

### Testing Migrations

Migrations must work on both SQLite and PostgreSQL:

```bash
# Test on SQLite
DATABASE_URL=sqlite+aiosqlite:///./test.db alembic upgrade head
DATABASE_URL=sqlite+aiosqlite:///./test.db alembic downgrade base

# Test on PostgreSQL
DATABASE_URL=postgresql+asyncpg://test:test@localhost:5432/test_db alembic upgrade head
DATABASE_URL=postgresql+asyncpg://test:test@localhost:5432/test_db alembic downgrade base
```

---

## Performance Tuning

### SQLite Optimizations

```python
# In connection.py for SQLite
PRAGMA journal_mode=WAL;  # Write-Ahead Logging
PRAGMA synchronous=NORMAL;  # Faster writes
PRAGMA cache_size=-64000;  # 64MB cache
PRAGMA temp_store=MEMORY;  # In-memory temp tables
```

### PostgreSQL Optimizations

```sql
-- In postgresql.conf
shared_buffers = 256MB
effective_cache_size = 1GB
maintenance_work_mem = 64MB
work_mem = 16MB
```

---

## References

- [SQLAlchemy Async Documentation](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)
- [PostgreSQL Documentation](https://www.postgresql.org/docs/14/)
- [SQLite Documentation](https://www.sqlite.org/docs.html)
- [Alembic Documentation](https://alembic.sqlalchemy.org/)

---

**Last Updated**: 2025-10-31 (T071 - User Story 3)
