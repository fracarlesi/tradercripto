#!/bin/bash
# PostgreSQL Backup Script (T088, Enhanced T138)
#
# Creates compressed timestamped backups of PostgreSQL database:
# - Dumps database using pg_dump
# - Compresses with gzip
# - Stores in data/backups/ with timestamp
# - Keeps last 7 days of backups
# - Logs all operations
# - Pre-backup disk space check (T138)
# - Post-backup integrity verification (T138)
# - Success/failure notifications via alerting service (T138)
#
# Usage:
#   ./backend/scripts/maintenance/backup_db.sh [OPTIONS]
#
# Options:
#   --keep-days N        Keep backups for N days (default: 7)
#   --output-dir D       Output directory (default: data/backups)
#   --db-name NAME       Database name (default: from .env or trader_db)
#   --compress           Compress backup with gzip (default: true)
#   --quiet              Minimal output
#   --min-space-gb N     Minimum free space required in GB (default: 1)
#   --alert              Send alerts on success/failure (default: false)
#
# Environment Variables:
#   POSTGRES_USER     PostgreSQL username (default: trader)
#   POSTGRES_PASSWORD PostgreSQL password (required)
#   POSTGRES_HOST     PostgreSQL host (default: localhost)
#   POSTGRES_PORT     PostgreSQL port (default: 5432)
#   POSTGRES_DB       Database name (default: trader_db)

set -e

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Default values
KEEP_DAYS=7
OUTPUT_DIR="$PROJECT_ROOT/data/backups"
COMPRESS=true
QUIET=false
MIN_SPACE_GB=1
SEND_ALERTS=false

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

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --keep-days)
            KEEP_DAYS="$2"
            shift 2
            ;;
        --output-dir)
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --db-name)
            POSTGRES_DB="$2"
            shift 2
            ;;
        --no-compress)
            COMPRESS=false
            shift
            ;;
        --quiet)
            QUIET=true
            shift
            ;;
        --min-space-gb)
            MIN_SPACE_GB="$2"
            shift 2
            ;;
        --alert)
            SEND_ALERTS=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--keep-days N] [--output-dir D] [--db-name NAME] [--no-compress] [--quiet] [--min-space-gb N] [--alert]"
            exit 1
            ;;
    esac
done

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

# Function to send alert (T138)
send_alert() {
    local level="$1"
    local title="$2"
    local message="$3"

    if [ "$SEND_ALERTS" = true ]; then
        # Use Python to call the alerting service
        python3 -c "
import asyncio
import sys
sys.path.insert(0, '$PROJECT_ROOT')
from backend.services.infrastructure.alerting import AlertLevel, alerting_service

async def send():
    await alerting_service.send_alert(
        level=AlertLevel.${level^^},
        title='$title',
        message='$message',
        metadata={
            'database': '$POSTGRES_DB',
            'host': '$POSTGRES_HOST',
            'backup_dir': '$OUTPUT_DIR',
            'script': 'backup_db.sh'
        }
    )

asyncio.run(send())
" 2>/dev/null || log_warning "Failed to send alert"
    fi
}

# Create backup directory if it doesn't exist
mkdir -p "$OUTPUT_DIR"

# Generate backup filename with timestamp
TIMESTAMP=$(date +'%Y%m%d_%H%M%S')
BACKUP_FILE="$OUTPUT_DIR/postgres_backup_${TIMESTAMP}.sql"
COMPRESSED_FILE="${BACKUP_FILE}.gz"

if [ "$QUIET" = false ]; then
    echo "=============================================================================="
    echo "                 POSTGRESQL BACKUP (T088, Enhanced T138)                     "
    echo "=============================================================================="
    echo ""
    log "Database: $POSTGRES_DB"
    log "Host: $POSTGRES_HOST:$POSTGRES_PORT"
    log "User: $POSTGRES_USER"
    log "Output directory: $OUTPUT_DIR"
    log "Keep backups for: $KEEP_DAYS days"
    log "Compress: $COMPRESS"
    log "Minimum free space: ${MIN_SPACE_GB}GB"
    log "Send alerts: $SEND_ALERTS"
    echo ""
fi

# Step 0: Check available disk space (T138)
log "Step 0/6: Checking available disk space..."
echo "----------------------------------------"

# Get available space in GB
if command -v df > /dev/null 2>&1; then
    AVAIL_SPACE_KB=$(df -k "$OUTPUT_DIR" | tail -1 | awk '{print $4}')
    AVAIL_SPACE_GB=$(echo "scale=2; $AVAIL_SPACE_KB / 1024 / 1024" | bc)

    log "Available space: ${AVAIL_SPACE_GB}GB"

    # Check if enough space
    if (( $(echo "$AVAIL_SPACE_GB < $MIN_SPACE_GB" | bc -l) )); then
        log_error "Insufficient disk space: ${AVAIL_SPACE_GB}GB available, ${MIN_SPACE_GB}GB required"
        send_alert "critical" "Backup Failed: Insufficient Disk Space" "Cannot create backup - only ${AVAIL_SPACE_GB}GB available, ${MIN_SPACE_GB}GB required. Database: $POSTGRES_DB"
        exit 1
    fi

    log_success "Sufficient disk space available"
else
    log_warning "Cannot check disk space (df command not available)"
fi
echo ""

# Step 1: Check PostgreSQL connection
log "Step 1/6: Checking PostgreSQL connection..."
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
        send_alert "error" "Backup Failed: PostgreSQL Connection Error" "Cannot connect to PostgreSQL in Docker container. Database: $POSTGRES_DB"
        exit 1
    fi
else
    if ! PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c '\q' > /dev/null 2>&1; then
        log_error "Cannot connect to PostgreSQL on host"
        send_alert "error" "Backup Failed: PostgreSQL Connection Error" "Cannot connect to PostgreSQL on host $POSTGRES_HOST:$POSTGRES_PORT. Database: $POSTGRES_DB"
        exit 1
    fi
fi

log_success "PostgreSQL connection successful"
echo ""

# Step 2: Get database size
log "Step 2/6: Checking database size..."
echo "----------------------------------------"

if [ -n "$DOCKER_EXEC" ]; then
    DB_SIZE=$($DOCKER_EXEC psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -c "SELECT pg_size_pretty(pg_database_size('$POSTGRES_DB'));" | xargs)
else
    DB_SIZE=$(PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -c "SELECT pg_size_pretty(pg_database_size('$POSTGRES_DB'));" | xargs)
fi

log "Database size: $DB_SIZE"
log_success "Database size checked"
echo ""

# Step 3: Create backup
log "Step 3/6: Creating database backup..."
echo "----------------------------------------"

START_TIME=$(date +%s)

if [ -n "$DOCKER_EXEC" ]; then
    # Backup from Docker container
    log "Running pg_dump in Docker container..."
    if ! $DOCKER_EXEC pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --create > "$BACKUP_FILE" 2>/dev/null; then
        log_error "pg_dump failed"
        send_alert "error" "Backup Failed: pg_dump Error" "Failed to create database dump. Database: $POSTGRES_DB. Check database connection and permissions."
        exit 1
    fi
else
    # Backup from host
    log "Running pg_dump on host..."
    if ! PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --create > "$BACKUP_FILE" 2>/dev/null; then
        log_error "pg_dump failed"
        send_alert "error" "Backup Failed: pg_dump Error" "Failed to create database dump. Database: $POSTGRES_DB. Check database connection and permissions."
        exit 1
    fi
fi

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

# Verify backup file was created and is not empty
if [ ! -f "$BACKUP_FILE" ] || [ ! -s "$BACKUP_FILE" ]; then
    log_error "Backup file is empty or does not exist"
    send_alert "error" "Backup Failed: Empty Backup File" "Backup file was created but is empty. Database: $POSTGRES_DB"
    exit 1
fi

BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
log "Backup created: $BACKUP_FILE"
log "Backup size: $BACKUP_SIZE"
log "Duration: ${DURATION}s"

log_success "Database backup created"
echo ""

# Step 4: Compress backup
if [ "$COMPRESS" = true ]; then
    log "Step 4/6: Compressing backup..."
    echo "----------------------------------------"

    log "Compressing with gzip..."
    if ! gzip -f "$BACKUP_FILE"; then
        log_error "Compression failed"
        send_alert "error" "Backup Failed: Compression Error" "Failed to compress backup file. Database: $POSTGRES_DB"
        exit 1
    fi

    COMPRESSED_SIZE=$(du -h "$COMPRESSED_FILE" | cut -f1)
    ORIGINAL_SIZE=$(echo "$BACKUP_SIZE" | sed 's/[^0-9.]//g')
    COMPRESSED_SIZE_NUM=$(echo "$COMPRESSED_SIZE" | sed 's/[^0-9.]//g')

    if [ -n "$ORIGINAL_SIZE" ] && [ -n "$COMPRESSED_SIZE_NUM" ]; then
        COMPRESSION_RATIO=$(echo "scale=2; 100 - ($COMPRESSED_SIZE_NUM / $ORIGINAL_SIZE * 100)" | bc 2>/dev/null || echo "N/A")
        log "Compressed size: $COMPRESSED_SIZE (${COMPRESSION_RATIO}% reduction)"
    else
        log "Compressed size: $COMPRESSED_SIZE"
    fi

    log_success "Backup compressed"
    echo ""
else
    log "Step 4/6: Skipping compression (--no-compress)"
    COMPRESSED_FILE="$BACKUP_FILE"
    echo ""
fi

# Step 5: Verify backup integrity (T138)
log "Step 5/6: Verifying backup integrity..."
echo "----------------------------------------"

if [ "$COMPRESS" = true ]; then
    # Verify gzip integrity
    log "Testing gzip file integrity..."
    if gzip -t "$COMPRESSED_FILE" 2>/dev/null; then
        log_success "Backup file integrity verified (gzip test passed)"
    else
        log_error "Backup file is corrupted (gzip test failed)"
        send_alert "error" "Backup Failed: Corrupted Backup File" "Backup file created but failed integrity check. Database: $POSTGRES_DB. File: $(basename "$COMPRESSED_FILE")"
        exit 1
    fi
else
    # For uncompressed, check if it's valid SQL
    log "Checking SQL file structure..."
    if head -n 20 "$COMPRESSED_FILE" | grep -q "PostgreSQL database dump"; then
        log_success "Backup file appears valid (PostgreSQL dump header found)"
    else
        log_warning "Cannot verify SQL file integrity (header check inconclusive)"
    fi
fi
echo ""

# Step 6: Clean up old backups
log "Step 6/6: Cleaning up old backups (keep last $KEEP_DAYS days)..."
echo "----------------------------------------"

# Find and delete backups older than KEEP_DAYS
DELETED_COUNT=0
while IFS= read -r old_backup; do
    if [ -n "$old_backup" ]; then
        log "Deleting old backup: $(basename "$old_backup")"
        rm -f "$old_backup"
        ((DELETED_COUNT++))
    fi
done < <(find "$OUTPUT_DIR" -name "postgres_backup_*.sql*" -type f -mtime +$KEEP_DAYS)

if [ $DELETED_COUNT -eq 0 ]; then
    log "No old backups to delete"
else
    log "Deleted $DELETED_COUNT old backup(s)"
fi

log_success "Cleanup complete"
echo ""

# Summary
TOTAL_BACKUPS=$(find "$OUTPUT_DIR" -name "postgres_backup_*.sql*" -type f | wc -l | xargs)
TOTAL_SIZE=$(du -sh "$OUTPUT_DIR" 2>/dev/null | cut -f1)

echo "=============================================================================="
echo "                        BACKUP SUCCESSFUL ✅                                 "
echo "=============================================================================="
echo ""
echo "Backup Summary:"
echo "  - Database: $POSTGRES_DB"
echo "  - Database size: $DB_SIZE"
echo "  - Backup file: $(basename "$COMPRESSED_FILE")"
echo "  - Backup size: $([ "$COMPRESS" = true ] && echo "$COMPRESSED_SIZE" || echo "$BACKUP_SIZE")"
echo "  - Duration: ${DURATION}s"
echo "  - Total backups: $TOTAL_BACKUPS"
echo "  - Total backup size: $TOTAL_SIZE"
echo ""
echo "Backup stored at:"
echo "  $COMPRESSED_FILE"
echo ""
echo "To restore this backup:"
echo "  ./backend/scripts/maintenance/restore_db.sh $COMPRESSED_FILE"
echo ""

# Log to file
LOG_FILE="$OUTPUT_DIR/backup.log"
echo "[$(date +'%Y-%m-%d %H:%M:%S')] Backup created: $(basename "$COMPRESSED_FILE") (size: $([ "$COMPRESS" = true ] && echo "$COMPRESSED_SIZE" || echo "$BACKUP_SIZE"), duration: ${DURATION}s)" >> "$LOG_FILE"

# Send success alert (T138)
send_alert "info" "Database Backup Successful" "Database backup completed successfully. Database: $POSTGRES_DB, Size: $([ "$COMPRESS" = true ] && echo "$COMPRESSED_SIZE" || echo "$BACKUP_SIZE"), Duration: ${DURATION}s, Total backups: $TOTAL_BACKUPS"

# Output backup file path for scripting
echo "$COMPRESSED_FILE"
