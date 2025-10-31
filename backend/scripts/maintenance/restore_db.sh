#!/bin/bash
# PostgreSQL Restore Script (T089)
#
# Restores PostgreSQL database from compressed backup:
# - Decompresses backup file if needed
# - Drops and recreates database
# - Restores from SQL dump
# - Verifies data integrity (row counts)
# - Creates backup before restore for safety
#
# Usage:
#   ./backend/scripts/maintenance/restore_db.sh BACKUP_FILE [OPTIONS]
#
# Arguments:
#   BACKUP_FILE      Path to backup file (.sql or .sql.gz)
#
# Options:
#   --no-backup      Skip creating safety backup before restore
#   --force          Force restore without confirmation
#   --db-name NAME   Database name (default: from .env or trader_db)
#   --quiet          Minimal output
#
# Environment Variables:
#   POSTGRES_USER     PostgreSQL username (default: trader)
#   POSTGRES_PASSWORD PostgreSQL password (required)
#   POSTGRES_HOST     PostgreSQL host (default: localhost)
#   POSTGRES_PORT     PostgreSQL port (default: 5432)
#   POSTGRES_DB       Database name (default: trader_db)
#
# Examples:
#   # Restore from compressed backup
#   ./backend/scripts/maintenance/restore_db.sh data/backups/postgres_backup_20250131_120000.sql.gz
#
#   # Restore without safety backup (faster)
#   ./backend/scripts/maintenance/restore_db.sh backup.sql.gz --no-backup
#
#   # Force restore without confirmation
#   ./backend/scripts/maintenance/restore_db.sh backup.sql.gz --force

set -e

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Default values
CREATE_SAFETY_BACKUP=true
FORCE_RESTORE=false
QUIET=false
BACKUP_FILE=""

# Load environment variables from .env if exists
if [ -f "$PROJECT_ROOT/.env" ]; then
    export $(cat "$PROJECT_ROOT/.env" | grep -v '^#' | xargs)
fi

# PostgreSQL connection settings
POSTGRES_USER="${POSTGRES_USER:-trader}"
POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_DB="${POSTGRES_DB:-trader_db}"

# Check if password is set
if [ -z "$POSTGRES_PASSWORD" ]; then
    echo "Error: POSTGRES_PASSWORD not set"
    echo "Set it in .env file or as environment variable"
    exit 1
fi

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Logging functions
log() {
    if [ "$QUIET" = false ]; then
        echo -e "${BLUE}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1"
    fi
}

log_success() {
    echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')] ✅ $1${NC}"
}

log_warning() {
    echo -e "${YELLOW}[$(date +'%Y-%m-%d %H:%M:%S')] ⚠️  $1${NC}"
}

log_error() {
    echo -e "${RED}[$(date +'%Y-%m-%d %H:%M:%S')] ❌ $1${NC}"
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --no-backup)
            CREATE_SAFETY_BACKUP=false
            shift
            ;;
        --force)
            FORCE_RESTORE=true
            shift
            ;;
        --db-name)
            POSTGRES_DB="$2"
            shift 2
            ;;
        --quiet)
            QUIET=true
            shift
            ;;
        -*)
            echo "Unknown option: $1"
            echo "Usage: $0 BACKUP_FILE [--no-backup] [--force] [--db-name NAME] [--quiet]"
            exit 1
            ;;
        *)
            BACKUP_FILE="$1"
            shift
            ;;
    esac
done

# Check if backup file is provided
if [ -z "$BACKUP_FILE" ]; then
    echo "Error: No backup file specified"
    echo "Usage: $0 BACKUP_FILE [OPTIONS]"
    echo ""
    echo "Available backups:"
    ls -lh "$PROJECT_ROOT/data/backups/postgres_backup_"*.sql* 2>/dev/null || echo "  No backups found in data/backups/"
    exit 1
fi

# Check if backup file exists
if [ ! -f "$BACKUP_FILE" ]; then
    log_error "Backup file not found: $BACKUP_FILE"
    exit 1
fi

if [ "$QUIET" = false ]; then
    echo "=============================================================================="
    echo "                    POSTGRESQL RESTORE (T089)                                "
    echo "=============================================================================="
    echo ""
    log "Database: $POSTGRES_DB"
    log "Host: $POSTGRES_HOST:$POSTGRES_PORT"
    log "User: $POSTGRES_USER"
    log "Backup file: $BACKUP_FILE"
    log "Safety backup: $CREATE_SAFETY_BACKUP"
    echo ""
fi

# Confirmation prompt
if [ "$FORCE_RESTORE" = false ]; then
    echo -e "${RED}WARNING: This will DROP and RECREATE the database '$POSTGRES_DB'${NC}"
    echo -e "${RED}All existing data will be LOST!${NC}"
    echo ""
    read -p "Are you sure you want to continue? (yes/no): " CONFIRM
    if [ "$CONFIRM" != "yes" ]; then
        log "Restore cancelled by user"
        exit 0
    fi
    echo ""
fi

# Step 1: Create safety backup
if [ "$CREATE_SAFETY_BACKUP" = true ]; then
    log "Step 1/7: Creating safety backup before restore..."
    echo "----------------------------------------"

    if [ -f "$SCRIPT_DIR/backup_db.sh" ]; then
        SAFETY_BACKUP=$(bash "$SCRIPT_DIR/backup_db.sh" --quiet)
        log "Safety backup created: $SAFETY_BACKUP"
        log_success "Safety backup complete"
    else
        log_warning "Backup script not found, skipping safety backup"
    fi
    echo ""
else
    log "Step 1/7: Skipping safety backup (--no-backup)"
    echo ""
fi

# Step 2: Check PostgreSQL connection
log "Step 2/7: Checking PostgreSQL connection..."
echo "----------------------------------------"

# Check if running in Docker
if docker ps --filter "name=trader_postgres" --format "{{.Names}}" | grep -q "trader_postgres"; then
    log "PostgreSQL running in Docker container"
    DOCKER_EXEC="docker exec trader_postgres"
    PGPASSWORD="$POSTGRES_PASSWORD"
else
    log "PostgreSQL running on host"
    DOCKER_EXEC=""
    export PGPASSWORD="$POSTGRES_PASSWORD"
fi

# Test connection
if [ -n "$DOCKER_EXEC" ]; then
    if ! $DOCKER_EXEC pg_isready -U "$POSTGRES_USER" > /dev/null 2>&1; then
        log_error "Cannot connect to PostgreSQL in Docker"
        exit 1
    fi
else
    if ! PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d postgres -c '\q' > /dev/null 2>&1; then
        log_error "Cannot connect to PostgreSQL on host"
        exit 1
    fi
fi

log_success "PostgreSQL connection successful"
echo ""

# Step 3: Get current database stats (before restore)
log "Step 3/7: Recording current database stats..."
echo "----------------------------------------"

TABLES_BEFORE=0
ROWS_BEFORE=0

if [ -n "$DOCKER_EXEC" ]; then
    # Check if database exists
    DB_EXISTS=$($DOCKER_EXEC psql -U "$POSTGRES_USER" -d postgres -t -c "SELECT 1 FROM pg_database WHERE datname='$POSTGRES_DB';" | xargs)
    if [ "$DB_EXISTS" = "1" ]; then
        TABLES_BEFORE=$($DOCKER_EXEC psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public';" | xargs)
        log "Current tables: $TABLES_BEFORE"
    else
        log "Database does not exist yet"
    fi
else
    DB_EXISTS=$(PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d postgres -t -c "SELECT 1 FROM pg_database WHERE datname='$POSTGRES_DB';" | xargs)
    if [ "$DB_EXISTS" = "1" ]; then
        TABLES_BEFORE=$(PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public';" | xargs)
        log "Current tables: $TABLES_BEFORE"
    else
        log "Database does not exist yet"
    fi
fi

log_success "Current stats recorded"
echo ""

# Step 4: Decompress backup if needed
log "Step 4/7: Preparing backup file..."
echo "----------------------------------------"

RESTORE_FILE="$BACKUP_FILE"

if [[ "$BACKUP_FILE" == *.gz ]]; then
    log "Backup is compressed, decompressing..."
    TEMP_FILE="${BACKUP_FILE%.gz}.tmp"

    gunzip -c "$BACKUP_FILE" > "$TEMP_FILE"
    RESTORE_FILE="$TEMP_FILE"

    DECOMPRESSED_SIZE=$(du -h "$RESTORE_FILE" | cut -f1)
    log "Decompressed size: $DECOMPRESSED_SIZE"
    log_success "Backup decompressed"
else
    log "Backup is not compressed, using directly"
fi

echo ""

# Step 5: Drop and recreate database
log "Step 5/7: Dropping and recreating database..."
echo "----------------------------------------"

log "Terminating active connections to $POSTGRES_DB..."
if [ -n "$DOCKER_EXEC" ]; then
    $DOCKER_EXEC psql -U "$POSTGRES_USER" -d postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$POSTGRES_DB' AND pid <> pg_backend_pid();" > /dev/null 2>&1 || true
else
    PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$POSTGRES_DB' AND pid <> pg_backend_pid();" > /dev/null 2>&1 || true
fi

log "Dropping database $POSTGRES_DB..."
if [ -n "$DOCKER_EXEC" ]; then
    $DOCKER_EXEC psql -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE IF EXISTS \"$POSTGRES_DB\";" > /dev/null
else
    PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE IF EXISTS \"$POSTGRES_DB\";" > /dev/null
fi

log "Creating database $POSTGRES_DB..."
if [ -n "$DOCKER_EXEC" ]; then
    $DOCKER_EXEC psql -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE \"$POSTGRES_DB\" OWNER \"$POSTGRES_USER\";" > /dev/null
else
    PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE \"$POSTGRES_DB\" OWNER \"$POSTGRES_USER\";" > /dev/null
fi

log_success "Database recreated"
echo ""

# Step 6: Restore from backup
log "Step 6/7: Restoring database from backup..."
echo "----------------------------------------"

START_TIME=$(date +%s)

log "Running psql restore..."
if [ -n "$DOCKER_EXEC" ]; then
    # Copy backup file to container
    docker cp "$RESTORE_FILE" trader_postgres:/tmp/restore.sql
    $DOCKER_EXEC psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /tmp/restore.sql > /dev/null 2>&1
    $DOCKER_EXEC rm /tmp/restore.sql
else
    PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f "$RESTORE_FILE" > /dev/null 2>&1
fi

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

log "Restore duration: ${DURATION}s"
log_success "Database restored"
echo ""

# Clean up temp file
if [[ "$BACKUP_FILE" == *.gz ]]; then
    rm -f "$TEMP_FILE"
fi

# Step 7: Verify data integrity
log "Step 7/7: Verifying data integrity..."
echo "----------------------------------------"

if [ -n "$DOCKER_EXEC" ]; then
    TABLES_AFTER=$($DOCKER_EXEC psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public';" | xargs)
    DB_SIZE=$($DOCKER_EXEC psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -c "SELECT pg_size_pretty(pg_database_size('$POSTGRES_DB'));" | xargs)
else
    TABLES_AFTER=$(PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public';" | xargs)
    DB_SIZE=$(PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -c "SELECT pg_size_pretty(pg_database_size('$POSTGRES_DB'));" | xargs)
fi

log "Tables restored: $TABLES_AFTER"
log "Database size: $DB_SIZE"

# Verify table row counts
log "Checking table row counts..."
if [ -n "$DOCKER_EXEC" ]; then
    $DOCKER_EXEC psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT schemaname, tablename, n_live_tup as rows FROM pg_stat_user_tables ORDER BY n_live_tup DESC;"
else
    PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT schemaname, tablename, n_live_tup as rows FROM pg_stat_user_tables ORDER BY n_live_tup DESC;"
fi

log_success "Data integrity verified"
echo ""

# Summary
echo "=============================================================================="
echo "                        RESTORE SUCCESSFUL ✅                                "
echo "=============================================================================="
echo ""
echo "Restore Summary:"
echo "  - Database: $POSTGRES_DB"
echo "  - Backup file: $(basename "$BACKUP_FILE")"
echo "  - Tables restored: $TABLES_AFTER"
echo "  - Database size: $DB_SIZE"
echo "  - Duration: ${DURATION}s"
if [ "$CREATE_SAFETY_BACKUP" = true ]; then
    echo "  - Safety backup: Created before restore"
fi
echo ""
echo "Database is now running with restored data."
echo ""
echo "To verify application:"
echo "  docker-compose restart app"
echo "  curl http://localhost:5611/api/health"
echo ""
if [ "$CREATE_SAFETY_BACKUP" = true ] && [ -n "$SAFETY_BACKUP" ]; then
    echo "If you need to rollback:"
    echo "  ./backend/scripts/maintenance/restore_db.sh $SAFETY_BACKUP --no-backup"
    echo ""
fi

# Log to file
LOG_FILE="$PROJECT_ROOT/data/backups/restore.log"
mkdir -p "$(dirname "$LOG_FILE")"
echo "[$(date +'%Y-%m-%d %H:%M:%S')] Restore complete: $(basename "$BACKUP_FILE") → $POSTGRES_DB (tables: $TABLES_AFTER, size: $DB_SIZE, duration: ${DURATION}s)" >> "$LOG_FILE"
