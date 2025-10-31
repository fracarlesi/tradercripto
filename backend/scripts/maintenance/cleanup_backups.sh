#!/bin/bash
# Backup Retention and Cleanup Script (T139)
#
# Implements backup retention policy:
# - Keep last 7 daily backups (most recent)
# - Keep last 4 weekly backups (Sundays)
# - Compress old uncompressed backups
# - Delete backups beyond retention period
#
# Usage:
#   ./backend/scripts/maintenance/cleanup_backups.sh [OPTIONS]
#
# Options:
#   --backup-dir D       Backup directory (default: data/backups)
#   --daily-keep N       Keep N daily backups (default: 7)
#   --weekly-keep N      Keep N weekly backups (default: 4)
#   --dry-run            Show what would be deleted without deleting
#   --quiet              Minimal output
#
# Cron Example (run weekly on Sundays at 3 AM):
#   0 3 * * 0 /path/to/cleanup_backups.sh >> /path/to/cleanup.log 2>&1

set -e

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Default values
BACKUP_DIR="$PROJECT_ROOT/data/backups"
DAILY_KEEP=7
WEEKLY_KEEP=4
DRY_RUN=false
QUIET=false

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --backup-dir)
            BACKUP_DIR="$2"
            shift 2
            ;;
        --daily-keep)
            DAILY_KEEP="$2"
            shift 2
            ;;
        --weekly-keep)
            WEEKLY_KEEP="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --quiet)
            QUIET=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--backup-dir D] [--daily-keep N] [--weekly-keep N] [--dry-run] [--quiet]"
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

# Check backup directory exists
if [ ! -d "$BACKUP_DIR" ]; then
    log_error "Backup directory does not exist: $BACKUP_DIR"
    exit 1
fi

if [ "$QUIET" = false ]; then
    echo "=============================================================================="
    echo "              BACKUP RETENTION & CLEANUP (T139)                              "
    echo "=============================================================================="
    echo ""
    log "Backup directory: $BACKUP_DIR"
    log "Daily backups to keep: $DAILY_KEEP"
    log "Weekly backups to keep: $WEEKLY_KEEP"
    log "Dry run: $DRY_RUN"
    echo ""
fi

# Step 1: Find all backup files
log "Step 1/5: Scanning backup directory..."
echo "----------------------------------------"

BACKUP_FILES=$(find "$BACKUP_DIR" -name "postgres_backup_*.sql*" -type f | sort -r)
TOTAL_FILES=$(echo "$BACKUP_FILES" | grep -c "." || echo "0")

if [ "$TOTAL_FILES" -eq 0 ]; then
    log_warning "No backup files found"
    exit 0
fi

log "Found $TOTAL_FILES backup file(s)"
echo ""

# Step 2: Compress uncompressed backups
log "Step 2/5: Compressing uncompressed backups..."
echo "----------------------------------------"

COMPRESSED_COUNT=0
while IFS= read -r backup_file; do
    if [ -n "$backup_file" ] && [[ "$backup_file" == *.sql ]]; then
        log "Compressing: $(basename "$backup_file")"
        if [ "$DRY_RUN" = false ]; then
            gzip -f "$backup_file" && ((COMPRESSED_COUNT++))
        else
            ((COMPRESSED_COUNT++))
        fi
    fi
done <<< "$BACKUP_FILES"

if [ $COMPRESSED_COUNT -eq 0 ]; then
    log "No uncompressed backups to compress"
else
    log_success "Compressed $COMPRESSED_COUNT backup(s)"
fi
echo ""

# Step 3: Categorize backups (daily vs weekly)
log "Step 3/5: Categorizing backups..."
echo "----------------------------------------"

# Refresh file list after compression
BACKUP_FILES=$(find "$BACKUP_DIR" -name "postgres_backup_*.sql*" -type f | sort -r)

# Arrays to hold backup files
declare -a DAILY_BACKUPS=()
declare -a WEEKLY_BACKUPS=()

while IFS= read -r backup_file; do
    if [ -n "$backup_file" ]; then
        # Extract timestamp from filename: postgres_backup_20250131_020000.sql.gz
        filename=$(basename "$backup_file")
        timestamp=$(echo "$filename" | sed -n 's/postgres_backup_\([0-9]*_[0-9]*\).*/\1/p')

        if [ -n "$timestamp" ]; then
            # Convert to date format: YYYYMMDD_HHMMSS -> YYYY-MM-DD
            date_part=$(echo "$timestamp" | cut -d'_' -f1)
            formatted_date="${date_part:0:4}-${date_part:4:2}-${date_part:6:2}"

            # Get day of week (0=Sunday, 1=Monday, etc.)
            if command -v date > /dev/null 2>&1; then
                day_of_week=$(date -d "$formatted_date" +%w 2>/dev/null || date -j -f "%Y-%m-%d" "$formatted_date" +%w 2>/dev/null || echo "")

                if [ "$day_of_week" = "0" ]; then
                    # Sunday backup = weekly
                    WEEKLY_BACKUPS+=("$backup_file")
                else
                    # Other days = daily
                    DAILY_BACKUPS+=("$backup_file")
                fi
            else
                # Fallback: treat all as daily if date command unavailable
                DAILY_BACKUPS+=("$backup_file")
            fi
        fi
    fi
done <<< "$BACKUP_FILES"

log "Daily backups: ${#DAILY_BACKUPS[@]}"
log "Weekly backups: ${#WEEKLY_BACKUPS[@]}"
echo ""

# Step 4: Apply retention policy
log "Step 4/5: Applying retention policy..."
echo "----------------------------------------"

# Delete old daily backups (keep last DAILY_KEEP)
DELETED_DAILY=0
if [ ${#DAILY_BACKUPS[@]} -gt $DAILY_KEEP ]; then
    log "Deleting old daily backups (keeping $DAILY_KEEP most recent)..."

    # Skip first DAILY_KEEP files (most recent), delete the rest
    for ((i=$DAILY_KEEP; i<${#DAILY_BACKUPS[@]}; i++)); do
        backup_file="${DAILY_BACKUPS[$i]}"
        log "  Deleting: $(basename "$backup_file")"

        if [ "$DRY_RUN" = false ]; then
            rm -f "$backup_file"
        fi
        ((DELETED_DAILY++))
    done
fi

# Delete old weekly backups (keep last WEEKLY_KEEP)
DELETED_WEEKLY=0
if [ ${#WEEKLY_BACKUPS[@]} -gt $WEEKLY_KEEP ]; then
    log "Deleting old weekly backups (keeping $WEEKLY_KEEP most recent)..."

    # Skip first WEEKLY_KEEP files (most recent), delete the rest
    for ((i=$WEEKLY_KEEP; i<${#WEEKLY_BACKUPS[@]}; i++)); do
        backup_file="${WEEKLY_BACKUPS[$i]}"
        log "  Deleting: $(basename "$backup_file")"

        if [ "$DRY_RUN" = false ]; then
            rm -f "$backup_file"
        fi
        ((DELETED_WEEKLY++))
    done
fi

TOTAL_DELETED=$((DELETED_DAILY + DELETED_WEEKLY))

if [ $TOTAL_DELETED -eq 0 ]; then
    log "No backups to delete (within retention policy)"
else
    if [ "$DRY_RUN" = true ]; then
        log_warning "DRY RUN: Would delete $TOTAL_DELETED backup(s) ($DELETED_DAILY daily, $DELETED_WEEKLY weekly)"
    else
        log_success "Deleted $TOTAL_DELETED backup(s) ($DELETED_DAILY daily, $DELETED_WEEKLY weekly)"
    fi
fi
echo ""

# Step 5: Summary
log "Step 5/5: Generating summary..."
echo "----------------------------------------"

# Refresh stats after cleanup
REMAINING_FILES=$(find "$BACKUP_DIR" -name "postgres_backup_*.sql*" -type f | wc -l | xargs)
TOTAL_SIZE=$(du -sh "$BACKUP_DIR" 2>/dev/null | cut -f1 || echo "unknown")

echo ""
echo "=============================================================================="
if [ "$DRY_RUN" = true ]; then
    echo "                      DRY RUN COMPLETE ✅                                     "
else
    echo "                      CLEANUP COMPLETE ✅                                     "
fi
echo "=============================================================================="
echo ""
echo "Summary:"
echo "  - Backups scanned: $TOTAL_FILES"
echo "  - Backups compressed: $COMPRESSED_COUNT"
echo "  - Backups deleted: $TOTAL_DELETED (${DELETED_DAILY} daily, ${DELETED_WEEKLY} weekly)"
echo "  - Backups remaining: $REMAINING_FILES"
echo "  - Total backup size: $TOTAL_SIZE"
echo ""
echo "Retention policy:"
echo "  - Daily backups: keep last $DAILY_KEEP"
echo "  - Weekly backups (Sundays): keep last $WEEKLY_KEEP"
echo ""

if [ "$DRY_RUN" = true ]; then
    echo "NOTE: This was a dry run. No files were actually modified or deleted."
    echo "Run without --dry-run to perform actual cleanup."
    echo ""
fi

# Log to file
if [ "$DRY_RUN" = false ]; then
    LOG_FILE="$BACKUP_DIR/cleanup.log"
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] Cleanup: compressed=$COMPRESSED_COUNT, deleted=$TOTAL_DELETED (daily=$DELETED_DAILY, weekly=$DELETED_WEEKLY), remaining=$REMAINING_FILES" >> "$LOG_FILE"
fi

log_success "Cleanup complete"
