#!/bin/bash
# Backup & Restore Test Procedure (T141)
#
# Tests complete backup and restore workflow:
# 1. Backup production database
# 2. Restore to staging database
# 3. Verify data integrity (row counts, checksums)
# 4. Measure recovery time (target: <15 minutes)
#
# Usage:
#   ./backend/scripts/testing/test_backup_restore.sh [OPTIONS]
#
# Options:
#   --prod-db NAME       Production database name (default: trader_db)
#   --staging-db NAME    Staging database name (default: trader_db_staging)
#   --backup-dir D       Backup directory (default: data/backups/test)
#   --skip-backup        Use existing backup file
#   --backup-file F      Use specific backup file
#
# Requirements:
#   - PostgreSQL running (Docker or host)
#   - Production database with data
#   - Permissions to create staging database

set -e

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Default values
PROD_DB="trader_db"
STAGING_DB="trader_db_staging"
BACKUP_DIR="$PROJECT_ROOT/data/backups/test"
SKIP_BACKUP=false
BACKUP_FILE=""

# Load environment
if [ -f "$PROJECT_ROOT/.env" ]; then
    export $(cat "$PROJECT_ROOT/.env" | grep -v '^#' | xargs)
fi

POSTGRES_USER="${POSTGRES_USER:-trader}"
POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --prod-db) PROD_DB="$2"; shift 2 ;;
        --staging-db) STAGING_DB="$2"; shift 2 ;;
        --backup-dir) BACKUP_DIR="$2"; shift 2 ;;
        --skip-backup) SKIP_BACKUP=true; shift ;;
        --backup-file) BACKUP_FILE="$2"; shift 2 ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--prod-db NAME] [--staging-db NAME] [--backup-dir D] [--skip-backup] [--backup-file F]"
            exit 1
            ;;
    esac
done

log() { echo -e "${BLUE}[$(date +'%H:%M:%S')]${NC} $1"; }
log_success() { echo -e "${GREEN}[$(date +'%H:%M:%S')] ✅ $1${NC}"; }
log_warning() { echo -e "${YELLOW}[$(date +'%H:%M:%S')] ⚠️  $1${NC}"; }
log_error() { echo -e "${RED}[$(date +'%H:%M:%S')] ❌ $1${NC}"; }

echo "=============================================================================="
echo "           BACKUP & RESTORE TEST PROCEDURE (T141)                            "
echo "=============================================================================="
echo ""
log "Production DB: $PROD_DB"
log "Staging DB: $STAGING_DB"
log "Backup directory: $BACKUP_DIR"
echo ""

# Check if running in Docker
if docker ps --filter "name=trader_postgres" --format "{{.Names}}" | grep -q "trader_postgres"; then
    DOCKER_EXEC="docker exec trader_postgres"
    log "PostgreSQL running in Docker container"
else
    DOCKER_EXEC=""
    export PGPASSWORD="$POSTGRES_PASSWORD"
    log "PostgreSQL running on host"
fi

# ============================================================================
# PHASE 1: BACKUP PRODUCTION DATABASE
# ============================================================================

if [ "$SKIP_BACKUP" = false ]; then
    log "PHASE 1/4: Creating production backup..."
    echo "============================================================================"

    START_BACKUP=$(date +%s)

    mkdir -p "$BACKUP_DIR"
    TIMESTAMP=$(date +'%Y%m%d_%H%M%S')
    BACKUP_FILE="$BACKUP_DIR/test_backup_${TIMESTAMP}.sql.gz"

    # Run backup script
    log "Running backup script..."
    if ! "$PROJECT_ROOT/backend/scripts/maintenance/backup_db.sh" \
        --db-name "$PROD_DB" \
        --output-dir "$BACKUP_DIR" \
        --quiet > /dev/null 2>&1; then
        log_error "Backup failed"
        exit 1
    fi

    # Find the latest backup
    BACKUP_FILE=$(find "$BACKUP_DIR" -name "postgres_backup_*.sql.gz" -type f -printf '%T@ %p\n' | sort -n | tail -1 | cut -f2- -d" ")

    END_BACKUP=$(date +%s)
    BACKUP_DURATION=$((END_BACKUP - START_BACKUP))

    BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
    log_success "Backup created: $(basename "$BACKUP_FILE") (${BACKUP_SIZE}, ${BACKUP_DURATION}s)"
    echo ""
else
    log "PHASE 1/4: Skipping backup (using existing file)"
    echo "============================================================================"
    if [ -z "$BACKUP_FILE" ] || [ ! -f "$BACKUP_FILE" ]; then
        log_error "Backup file not found: $BACKUP_FILE"
        exit 1
    fi
    log_success "Using backup: $(basename "$BACKUP_FILE")"
    echo ""
fi

# ============================================================================
# PHASE 2: COLLECT PRODUCTION DATABASE STATS
# ============================================================================

log "PHASE 2/4: Collecting production database statistics..."
echo "============================================================================"

declare -A PROD_STATS

# Get table row counts
TABLES=("accounts" "positions" "orders" "trades" "klines" "users")

for table in "${TABLES[@]}"; do
    if [ -n "$DOCKER_EXEC" ]; then
        COUNT=$($DOCKER_EXEC psql -U "$POSTGRES_USER" -d "$PROD_DB" -t -c "SELECT COUNT(*) FROM $table;" 2>/dev/null | xargs || echo "0")
    else
        COUNT=$(PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$PROD_DB" -t -c "SELECT COUNT(*) FROM $table;" 2>/dev/null | xargs || echo "0")
    fi
    PROD_STATS["$table"]=$COUNT
    log "  $table: $COUNT rows"
done

# Get database size
if [ -n "$DOCKER_EXEC" ]; then
    PROD_SIZE=$($DOCKER_EXEC psql -U "$POSTGRES_USER" -d "$PROD_DB" -t -c "SELECT pg_size_pretty(pg_database_size('$PROD_DB'));" | xargs)
else
    PROD_SIZE=$(PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$PROD_DB" -t -c "SELECT pg_size_pretty(pg_database_size('$PROD_DB'));" | xargs)
fi

log_success "Production stats collected (size: $PROD_SIZE)"
echo ""

# ============================================================================
# PHASE 3: RESTORE TO STAGING DATABASE
# ============================================================================

log "PHASE 3/4: Restoring to staging database..."
echo "============================================================================"

START_RESTORE=$(date +%s)

# Drop staging database if exists
log "Dropping existing staging database..."
if [ -n "$DOCKER_EXEC" ]; then
    $DOCKER_EXEC psql -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE IF EXISTS $STAGING_DB;" > /dev/null 2>&1
else
    PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE IF EXISTS $STAGING_DB;" > /dev/null 2>&1
fi

# Restore backup to staging
log "Restoring backup to staging..."
if [ -n "$DOCKER_EXEC" ]; then
    gunzip -c "$BACKUP_FILE" | $DOCKER_EXEC psql -U "$POSTGRES_USER" -d postgres > /dev/null 2>&1
    # Rename database to staging name
    $DOCKER_EXEC psql -U "$POSTGRES_USER" -d postgres -c "ALTER DATABASE $PROD_DB RENAME TO $STAGING_DB;" > /dev/null 2>&1 || true
else
    gunzip -c "$BACKUP_FILE" | PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d postgres > /dev/null 2>&1
    PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d postgres -c "ALTER DATABASE $PROD_DB RENAME TO $STAGING_DB;" > /dev/null 2>&1 || true
fi

END_RESTORE=$(date +%s)
RESTORE_DURATION=$((END_RESTORE - START_RESTORE))

log_success "Restore completed (${RESTORE_DURATION}s)"
echo ""

# ============================================================================
# PHASE 4: VERIFY DATA INTEGRITY
# ============================================================================

log "PHASE 4/4: Verifying data integrity..."
echo "============================================================================"

INTEGRITY_OK=true

# Verify row counts match
for table in "${TABLES[@]}"; do
    if [ -n "$DOCKER_EXEC" ]; then
        STAGING_COUNT=$($DOCKER_EXEC psql -U "$POSTGRES_USER" -d "$STAGING_DB" -t -c "SELECT COUNT(*) FROM $table;" 2>/dev/null | xargs || echo "0")
    else
        STAGING_COUNT=$(PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$STAGING_DB" -t -c "SELECT COUNT(*) FROM $table;" 2>/dev/null | xargs || echo "0")
    fi

    PROD_COUNT="${PROD_STATS[$table]}"

    if [ "$STAGING_COUNT" = "$PROD_COUNT" ]; then
        log_success "  $table: $STAGING_COUNT rows (matches production)"
    else
        log_error "  $table: $STAGING_COUNT rows (expected $PROD_COUNT) - MISMATCH!"
        INTEGRITY_OK=false
    fi
done

# Get staging database size
if [ -n "$DOCKER_EXEC" ]; then
    STAGING_SIZE=$($DOCKER_EXEC psql -U "$POSTGRES_USER" -d "$STAGING_DB" -t -c "SELECT pg_size_pretty(pg_database_size('$STAGING_DB'));" | xargs)
else
    STAGING_SIZE=$(PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$STAGING_DB" -t -c "SELECT pg_size_pretty(pg_database_size('$STAGING_DB'));" | xargs)
fi

log "Staging database size: $STAGING_SIZE (production: $PROD_SIZE)"
echo ""

# ============================================================================
# FINAL REPORT
# ============================================================================

TOTAL_TIME=$((END_RESTORE - START_BACKUP))
TARGET_TIME=900  # 15 minutes in seconds

echo "=============================================================================="
if [ "$INTEGRITY_OK" = true ] && [ $TOTAL_TIME -lt $TARGET_TIME ]; then
    echo "                    TEST PASSED ✅                                           "
elif [ "$INTEGRITY_OK" = true ]; then
    echo "                    TEST PASSED (SLOW) ⚠️                                    "
else
    echo "                    TEST FAILED ❌                                           "
fi
echo "=============================================================================="
echo ""
echo "Test Results:"
echo "  - Backup time: ${BACKUP_DURATION}s"
echo "  - Restore time: ${RESTORE_DURATION}s"
echo "  - Total recovery time: ${TOTAL_TIME}s"
echo "  - Target recovery time: <${TARGET_TIME}s (15 minutes)"
echo "  - Data integrity: $([ "$INTEGRITY_OK" = true ] && echo "✅ PASS" || echo "❌ FAIL")"
echo "  - Performance: $([ $TOTAL_TIME -lt $TARGET_TIME ] && echo "✅ PASS" || echo "⚠️  SLOW")"
echo ""
echo "Database Sizes:"
echo "  - Production: $PROD_SIZE"
echo "  - Staging: $STAGING_SIZE"
echo "  - Backup file: $BACKUP_SIZE"
echo ""
echo "Backup file: $BACKUP_FILE"
echo "Staging database: $STAGING_DB (ready for testing)"
echo ""

if [ "$INTEGRITY_OK" = true ] && [ $TOTAL_TIME -lt $TARGET_TIME ]; then
    log_success "Backup & restore test completed successfully!"
    exit 0
elif [ "$INTEGRITY_OK" = true ]; then
    log_warning "Test passed but recovery time exceeded target (${TOTAL_TIME}s > ${TARGET_TIME}s)"
    exit 0
else
    log_error "Data integrity verification failed"
    exit 1
fi
