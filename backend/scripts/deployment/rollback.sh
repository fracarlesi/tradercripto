#!/bin/bash
# Deployment Rollback Script (T147)
#
# Rolls back application to previous version:
# - Reverts to previous Docker image tag
# - Rolls back database migrations if needed
# - Restarts services with health checks
# - Verifies rollback success
#
# Usage:
#   ./backend/scripts/deployment/rollback.sh [VERSION|COMMIT_HASH]
#
# Examples:
#   ./rollback.sh                    # Rollback to previous version
#   ./rollback.sh v1.2.3             # Rollback to specific version tag
#   ./rollback.sh abc123             # Rollback to specific commit
#
# Environment Variables:
#   SKIP_DB_ROLLBACK    Skip database migration rollback (default: false)
#   FORCE_ROLLBACK      Force rollback even if health checks fail (default: false)

set -e

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SKIP_DB_ROLLBACK="${SKIP_DB_ROLLBACK:-false}"
FORCE_ROLLBACK="${FORCE_ROLLBACK:-false}"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Logging functions
log() {
    echo -e "${BLUE}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1"
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

# Change to project root
cd "$PROJECT_ROOT"

echo "=============================================================================="
echo "                        DEPLOYMENT ROLLBACK (T147)                          "
echo "=============================================================================="
echo ""

# Determine target version
TARGET_VERSION="${1:-}"

if [ -z "$TARGET_VERSION" ]; then
    # No version specified, find previous commit
    CURRENT_COMMIT=$(git rev-parse HEAD)
    log "Current commit: $CURRENT_COMMIT"

    # Get previous commit
    TARGET_VERSION=$(git rev-parse HEAD~1)
    log "Rolling back to previous commit: $TARGET_VERSION"
else
    log "Rolling back to specified version: $TARGET_VERSION"
fi

# Verify target exists
if ! git rev-parse "$TARGET_VERSION" > /dev/null 2>&1; then
    log_error "Invalid version/commit: $TARGET_VERSION"
    exit 1
fi

# Get full commit hash
TARGET_COMMIT=$(git rev-parse "$TARGET_VERSION")
TARGET_SHORT=$(git rev-parse --short "$TARGET_COMMIT")

log "Rollback target: $TARGET_SHORT"
log "Commit message: $(git log -1 --pretty=%B $TARGET_COMMIT | head -1)"
echo ""

# Confirmation
echo "⚠️  WARNING: This will rollback the application to a previous version."
echo ""
echo "Current state:"
docker-compose ps
echo ""
read -p "Continue with rollback? (yes/no): " CONFIRM

if [ "$CONFIRM" != "yes" ]; then
    log "Rollback cancelled by user"
    exit 0
fi

echo ""

# Step 1: Checkout target version
log "Step 1/6: Checking out target version..."
echo "----------------------------------------"

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
log "Current branch: $CURRENT_BRANCH"

# Stash any uncommitted changes
if [ -n "$(git status --porcelain)" ]; then
    log "Stashing uncommitted changes..."
    git stash save "Pre-rollback stash $(date +'%Y-%m-%d %H:%M:%S')"
fi

# Checkout target
log "Checking out $TARGET_SHORT..."
git checkout "$TARGET_COMMIT"

log_success "Code reverted to $TARGET_SHORT"
echo ""

# Step 2: Identify database migration changes
log "Step 2/6: Checking database migration status..."
echo "----------------------------------------"

# Get current migration version
CURRENT_MIGRATION=$(docker-compose run --rm app alembic current 2>/dev/null | grep -oP '^[a-f0-9]+' | head -1 || echo "none")
log "Current migration: $CURRENT_MIGRATION"

# Determine target migration from target version
git checkout "$TARGET_COMMIT" -- backend/alembic/versions/ 2>/dev/null || true

# List migrations in target version
MIGRATIONS_DIR="backend/alembic/versions"
if [ -d "$MIGRATIONS_DIR" ]; then
    TARGET_MIGRATION=$(ls -1t "$MIGRATIONS_DIR"/*.py 2>/dev/null | head -1 | grep -oP '[0-9]{8}_[0-9]{4}_[a-f0-9]+' | grep -oP '[a-f0-9]{12}' || echo "none")
    log "Target migration: $TARGET_MIGRATION"

    if [ "$CURRENT_MIGRATION" != "$TARGET_MIGRATION" ] && [ "$CURRENT_MIGRATION" != "none" ] && [ "$TARGET_MIGRATION" != "none" ]; then
        NEEDS_DB_ROLLBACK=true
        log_warning "Database migration rollback required"
    else
        NEEDS_DB_ROLLBACK=false
        log "No database migration rollback needed"
    fi
else
    NEEDS_DB_ROLLBACK=false
    log "Migrations directory not found, skipping DB rollback"
fi

echo ""

# Step 3: Rollback database migrations (if needed)
if [ "$NEEDS_DB_ROLLBACK" = true ] && [ "$SKIP_DB_ROLLBACK" = false ]; then
    log "Step 3/6: Rolling back database migrations..."
    echo "----------------------------------------"

    log "Creating database backup before rollback..."
    if [ -f "$SCRIPT_DIR/../maintenance/backup_db.sh" ]; then
        bash "$SCRIPT_DIR/../maintenance/backup_db.sh"
        log_success "Backup created"
    else
        log_warning "Backup script not found, continuing without backup"
    fi

    log "Rolling back database to $TARGET_MIGRATION..."
    if docker-compose run --rm app alembic downgrade "$TARGET_MIGRATION"; then
        log_success "Database rolled back successfully"
    else
        log_error "Database rollback failed"
        log "You may need to manually rollback the database"
        log "Or restore from backup: ./backend/scripts/maintenance/restore_db.sh <backup_file>"

        if [ "$FORCE_ROLLBACK" = false ]; then
            exit 1
        else
            log_warning "Continuing despite database rollback failure (FORCE_ROLLBACK=true)"
        fi
    fi
    echo ""
elif [ "$SKIP_DB_ROLLBACK" = true ]; then
    log "Step 3/6: Skipping database rollback (SKIP_DB_ROLLBACK=true)"
    echo ""
else
    log "Step 3/6: No database rollback needed"
    echo ""
fi

# Step 4: Build Docker images for target version
log "Step 4/6: Building Docker images for target version..."
echo "----------------------------------------"

log "Building application image..."
if docker-compose build --no-cache app; then
    log_success "Docker build successful"

    log "Tagging image with commit hash..."
    docker tag trader_bitcoin:latest "trader_bitcoin:$TARGET_SHORT"
else
    log_error "Docker build failed"
    if [ "$FORCE_ROLLBACK" = false ]; then
        exit 1
    fi
fi

echo ""

# Step 5: Restart services with health checks
log "Step 5/6: Restarting services..."
echo "----------------------------------------"

log "Stopping current containers..."
docker-compose down

log "Starting services with rolled-back version..."
docker-compose up -d

# Wait for health checks
log "Waiting for services to be healthy..."
MAX_WAIT=120  # 2 minutes
WAIT_COUNT=0

while [ $WAIT_COUNT -lt $MAX_WAIT ]; do
    APP_HEALTH=$(docker inspect trader_app --format='{{.State.Health.Status}}' 2>/dev/null || echo "starting")

    if [ "$APP_HEALTH" = "healthy" ]; then
        log_success "Application is healthy!"
        break
    fi

    echo -n "."
    sleep 2
    ((WAIT_COUNT+=2))
done
echo ""

if [ $WAIT_COUNT -ge $MAX_WAIT ]; then
    log_error "Services failed to become healthy within $MAX_WAIT seconds"
    log "Check logs: docker-compose logs app"

    if [ "$FORCE_ROLLBACK" = false ]; then
        exit 1
    fi
fi

log_success "Services restarted successfully"
echo ""

# Step 6: Verify rollback
log "Step 6/6: Verifying rollback..."
echo "----------------------------------------"

# Check health endpoint
log "Checking health endpoint..."
if curl -f -s http://localhost:5611/api/health > /dev/null 2>&1; then
    HEALTH_RESPONSE=$(curl -s http://localhost:5611/api/health)
    log "Health response: $HEALTH_RESPONSE"
    log_success "Health check passed"
else
    log_error "Health endpoint not responding"
    if [ "$FORCE_ROLLBACK" = false ]; then
        exit 1
    fi
fi

# Check current version
log "Verifying deployed version..."
DEPLOYED_COMMIT=$(docker exec trader_app git -C /app rev-parse --short HEAD 2>/dev/null || echo "unknown")
if [ "$DEPLOYED_COMMIT" = "$TARGET_SHORT" ]; then
    log_success "Deployed version matches target: $TARGET_SHORT"
elif [ "$DEPLOYED_COMMIT" != "unknown" ]; then
    log_warning "Deployed version ($DEPLOYED_COMMIT) doesn't match target ($TARGET_SHORT)"
else
    log_warning "Could not verify deployed version"
fi

# Show service status
log "Service status:"
docker-compose ps

log_success "Rollback verification complete"
echo ""

# Summary
echo "=============================================================================="
echo "                        ROLLBACK SUCCESSFUL ✅                               "
echo "=============================================================================="
echo ""
echo "Rollback Summary:"
echo "  - Target version: $TARGET_SHORT"
echo "  - Commit message: $(git log -1 --pretty=%B $TARGET_COMMIT | head -1)"
echo "  - Database rollback: $([ "$NEEDS_DB_ROLLBACK" = true ] && echo "Performed" || echo "Not needed")"
echo "  - Health status: $(curl -s http://localhost:5611/api/health 2>/dev/null || echo "Not responding")"
echo ""
echo "Application is now running at:"
echo "  - HTTP: http://localhost:5611"
echo "  - HTTPS: https://oaa.finan.club (via Traefik)"
echo ""
echo "To monitor logs:"
echo "  docker-compose logs -f app"
echo ""
echo "If issues persist, you can:"
echo "  1. Restore database backup: ./backend/scripts/maintenance/restore_db.sh <backup_file>"
echo "  2. Rollback to an earlier version: ./backend/scripts/deployment/rollback.sh <version>"
echo "  3. Check logs for errors: docker-compose logs --tail=100 app"
echo ""
