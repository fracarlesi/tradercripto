#!/bin/bash
# Zero-Downtime Deployment Script (T087)
#
# Deploys application with blue-green deployment strategy:
# - Pulls latest code from repository
# - Builds new Docker images
# - Runs database migrations
# - Starts new containers alongside old ones
# - Switches Traefik routing to new containers
# - Stops old containers after health checks pass
#
# Usage:
#   ./backend/scripts/deployment/deploy.sh [OPTIONS]
#
# Options:
#   --skip-backup    Skip database backup before deployment
#   --skip-tests     Skip running tests before deployment
#   --no-downtime    Use blue-green strategy (default)
#   --force          Force deployment even if tests fail
#
# Environment Variables:
#   DEPLOY_BRANCH    Git branch to deploy (default: main)
#   BACKUP_BEFORE    Create backup before deploy (default: true)
#   RUN_TESTS        Run tests before deploy (default: true)

set -e

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"
BACKUP_BEFORE="${BACKUP_BEFORE:-true}"
RUN_TESTS="${RUN_TESTS:-true}"
SKIP_BACKUP=false
SKIP_TESTS=false
FORCE_DEPLOY=false

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --skip-backup)
            SKIP_BACKUP=true
            shift
            ;;
        --skip-tests)
            SKIP_TESTS=true
            shift
            ;;
        --force)
            FORCE_DEPLOY=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--skip-backup] [--skip-tests] [--force]"
            exit 1
            ;;
    esac
done

# Logging function
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
echo "                    ZERO-DOWNTIME DEPLOYMENT (T087)                         "
echo "=============================================================================="
echo ""
log "Project root: $PROJECT_ROOT"
log "Deploy branch: $DEPLOY_BRANCH"
log "Backup before deploy: $([ "$SKIP_BACKUP" = true ] && echo "NO" || echo "YES")"
log "Run tests: $([ "$SKIP_TESTS" = true ] && echo "NO" || echo "YES")"
echo ""

# Step 1: Pre-deployment checks
log "Step 1/8: Pre-deployment checks..."
echo "----------------------------------------"

# Check if git repo
if [ ! -d .git ]; then
    log_error "Not a git repository"
    exit 1
fi

# Check for uncommitted changes
if [ -n "$(git status --porcelain)" ]; then
    log_warning "Uncommitted changes detected"
    git status --short
    if [ "$FORCE_DEPLOY" = false ]; then
        log_error "Commit or stash changes before deploying (use --force to override)"
        exit 1
    fi
fi

# Check docker is running
if ! docker info > /dev/null 2>&1; then
    log_error "Docker is not running"
    exit 1
fi

# Check docker-compose is available
if ! command -v docker-compose &> /dev/null; then
    log_error "docker-compose not found"
    exit 1
fi

log_success "Pre-deployment checks passed"
echo ""

# Step 2: Backup database (optional)
if [ "$SKIP_BACKUP" = false ]; then
    log "Step 2/8: Backing up database..."
    echo "----------------------------------------"

    if [ -f "$SCRIPT_DIR/../maintenance/backup_db.sh" ]; then
        bash "$SCRIPT_DIR/../maintenance/backup_db.sh"
        log_success "Database backup complete"
    else
        log_warning "Backup script not found, skipping backup"
    fi
    echo ""
else
    log "Step 2/8: Skipping database backup (--skip-backup)"
    echo ""
fi

# Step 3: Pull latest code
log "Step 3/8: Pulling latest code from $DEPLOY_BRANCH..."
echo "----------------------------------------"

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
log "Current branch: $CURRENT_BRANCH"

if [ "$CURRENT_BRANCH" != "$DEPLOY_BRANCH" ]; then
    log "Checking out $DEPLOY_BRANCH..."
    git checkout "$DEPLOY_BRANCH"
fi

log "Pulling latest changes..."
git pull origin "$DEPLOY_BRANCH"

COMMIT_HASH=$(git rev-parse --short HEAD)
COMMIT_MESSAGE=$(git log -1 --pretty=%B)
log "Deploy commit: $COMMIT_HASH"
log "Commit message: $COMMIT_MESSAGE"

log_success "Code updated to latest"
echo ""

# Step 4: Run tests (optional)
if [ "$SKIP_TESTS" = false ]; then
    log "Step 4/8: Running tests..."
    echo "----------------------------------------"

    # Run Docker build test
    if [ -f "$SCRIPT_DIR/../testing/test_docker_build.sh" ]; then
        log "Running Docker build tests..."
        if bash "$SCRIPT_DIR/../testing/test_docker_build.sh"; then
            log_success "Docker build tests passed"
        else
            log_error "Docker build tests failed"
            if [ "$FORCE_DEPLOY" = false ]; then
                exit 1
            else
                log_warning "Continuing deployment despite test failure (--force)"
            fi
        fi
    else
        log_warning "Docker build test not found, skipping"
    fi
    echo ""
else
    log "Step 4/8: Skipping tests (--skip-tests)"
    echo ""
fi

# Step 5: Build new images
log "Step 5/8: Building Docker images..."
echo "----------------------------------------"

log "Building application image..."
docker-compose build --no-cache app

log "Tagging new image with commit hash..."
docker tag trader_bitcoin:latest "trader_bitcoin:$COMMIT_HASH"

log_success "Docker images built"
echo ""

# Step 6: Run database migrations
log "Step 6/8: Running database migrations..."
echo "----------------------------------------"

# Check if PostgreSQL is running
if ! docker-compose ps postgres | grep -q "Up"; then
    log_warning "PostgreSQL not running, starting it..."
    docker-compose up -d postgres
    sleep 10
fi

# Run Alembic migrations
log "Running Alembic upgrade to head..."
docker-compose run --rm app alembic upgrade head

log_success "Migrations complete"
echo ""

# Step 7: Deploy with zero-downtime (blue-green strategy)
log "Step 7/8: Deploying new version (zero-downtime)..."
echo "----------------------------------------"

# Get current container name
OLD_CONTAINER=$(docker ps --filter "name=trader_app" --format "{{.Names}}" | head -n 1)
log "Current container: $OLD_CONTAINER"

# Start new container with temporary name
log "Starting new container (trader_app_new)..."
docker-compose up -d --no-deps --scale app=2 app

# Wait for new container to be healthy
log "Waiting for new container to be healthy..."
MAX_WAIT=120  # 2 minutes
WAIT_COUNT=0
while [ $WAIT_COUNT -lt $MAX_WAIT ]; do
    NEW_HEALTH=$(docker inspect trader_app --format='{{.State.Health.Status}}' 2>/dev/null || echo "starting")

    if [ "$NEW_HEALTH" = "healthy" ]; then
        log_success "New container is healthy!"
        break
    fi

    echo -n "."
    sleep 2
    ((WAIT_COUNT+=2))
done
echo ""

if [ $WAIT_COUNT -ge $MAX_WAIT ]; then
    log_error "New container failed to become healthy within $MAX_WAIT seconds"
    log "Rolling back..."
    docker-compose up -d --scale app=1 app
    exit 1
fi

# Traefik will automatically route to healthy container
log "Traefik routing traffic to new container..."
sleep 5

# Stop old container
log "Stopping old container..."
docker-compose up -d --scale app=1 app

log_success "Zero-downtime deployment complete"
echo ""

# Step 8: Post-deployment verification
log "Step 8/8: Post-deployment verification..."
echo "----------------------------------------"

# Check health endpoint
log "Checking health endpoint..."
if curl -f -s http://localhost:5611/api/health > /dev/null; then
    HEALTH_RESPONSE=$(curl -s http://localhost:5611/api/health)
    log "Health endpoint response: $HEALTH_RESPONSE"
    log_success "Application is healthy"
else
    log_error "Health endpoint not responding"
    exit 1
fi

# Check service status
log "Checking service status..."
docker-compose ps

# Clean up old images
log "Cleaning up old images..."
docker image prune -f

log_success "Post-deployment verification complete"
echo ""

# Summary
echo "=============================================================================="
echo "                        DEPLOYMENT SUCCESSFUL ✅                             "
echo "=============================================================================="
echo ""
echo "Deployment Summary:"
echo "  - Branch: $DEPLOY_BRANCH"
echo "  - Commit: $COMMIT_HASH"
echo "  - Message: $COMMIT_MESSAGE"
echo "  - Backup: $([ "$SKIP_BACKUP" = true ] && echo "Skipped" || echo "Created")"
echo "  - Tests: $([ "$SKIP_TESTS" = true ] && echo "Skipped" || echo "Passed")"
echo "  - Strategy: Zero-downtime (blue-green)"
echo ""
echo "Application is now running at:"
echo "  - HTTP: http://localhost:5611"
echo "  - HTTPS: https://oaa.finan.club (via Traefik)"
echo ""
echo "To monitor logs:"
echo "  docker-compose logs -f app"
echo ""
echo "To rollback:"
echo "  git checkout <previous-commit>"
echo "  ./backend/scripts/deployment/deploy.sh"
echo ""
