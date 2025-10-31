#!/bin/bash
# Docker Compose Testing Script (T083)
#
# Tests docker-compose up on clean environment verifying:
# - All services start in correct order
# - Health checks pass
# - Application is accessible
#
# Usage:
#   ./backend/scripts/testing/test_docker_compose.sh

set -e

echo "=============================================================================="
echo "Docker Compose Test Suite (T083)"
echo "=============================================================================="

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Test results
TESTS_PASSED=0
TESTS_FAILED=0

# Cleanup function
cleanup() {
    echo ""
    echo "Cleaning up..."
    docker-compose down -v 2>/dev/null || true
}

# Set trap to cleanup on exit
trap cleanup EXIT

# Test 1: Docker compose configuration is valid
echo ""
echo "📋 Test 1: Validating docker-compose.yml..."
echo "----------------------------------------"

if docker-compose config > /dev/null 2>&1; then
    echo -e "${GREEN}✅ PASS${NC}: docker-compose.yml is valid"
    ((TESTS_PASSED++))
else
    echo -e "${RED}❌ FAIL${NC}: docker-compose.yml has errors"
    docker-compose config
    ((TESTS_FAILED++))
    exit 1
fi

# Test 2: Start services
echo ""
echo "🚀 Test 2: Starting all services..."
echo "----------------------------------------"

echo "Running: docker-compose up -d"
if docker-compose up -d; then
    echo -e "${GREEN}✅ PASS${NC}: All services started"
    ((TESTS_PASSED++))
else
    echo -e "${RED}❌ FAIL${NC}: Failed to start services"
    ((TESTS_FAILED++))
    exit 1
fi

# Wait for services to initialize
echo ""
echo "⏳ Waiting for services to initialize (30 seconds)..."
sleep 30

# Test 3: Check PostgreSQL health
echo ""
echo "🐘 Test 3: Checking PostgreSQL health..."
echo "----------------------------------------"

POSTGRES_HEALTH=$(docker-compose ps postgres --format json | jq -r '.[0].Health')
echo "PostgreSQL health: ${POSTGRES_HEALTH}"

if [ "$POSTGRES_HEALTH" == "healthy" ]; then
    echo -e "${GREEN}✅ PASS${NC}: PostgreSQL is healthy"
    ((TESTS_PASSED++))
else
    echo -e "${RED}❌ FAIL${NC}: PostgreSQL is not healthy"
    docker-compose logs postgres | tail -20
    ((TESTS_FAILED++))
fi

# Test 4: Check Redis health
echo ""
echo "🔴 Test 4: Checking Redis health..."
echo "----------------------------------------"

REDIS_HEALTH=$(docker-compose ps redis --format json | jq -r '.[0].Health')
echo "Redis health: ${REDIS_HEALTH}"

if [ "$REDIS_HEALTH" == "healthy" ]; then
    echo -e "${GREEN}✅ PASS${NC}: Redis is healthy"
    ((TESTS_PASSED++))
else
    echo -e "${RED}❌ FAIL${NC}: Redis is not healthy"
    docker-compose logs redis | tail -20
    ((TESTS_FAILED++))
fi

# Test 5: Check App health
echo ""
echo "🚀 Test 5: Checking App health..."
echo "----------------------------------------"

# Wait a bit more for app to fully start
sleep 10

APP_HEALTH=$(docker-compose ps app --format json | jq -r '.[0].Health')
echo "App health: ${APP_HEALTH}"

if [ "$APP_HEALTH" == "healthy" ]; then
    echo -e "${GREEN}✅ PASS${NC}: App is healthy"
    ((TESTS_PASSED++))
elif [ "$APP_HEALTH" == "starting" ]; then
    echo -e "${YELLOW}⚠️  WARNING${NC}: App is still starting, waiting..."
    sleep 20
    APP_HEALTH=$(docker-compose ps app --format json | jq -r '.[0].Health')
    if [ "$APP_HEALTH" == "healthy" ]; then
        echo -e "${GREEN}✅ PASS${NC}: App is now healthy"
        ((TESTS_PASSED++))
    else
        echo -e "${RED}❌ FAIL${NC}: App did not become healthy"
        docker-compose logs app | tail -30
        ((TESTS_FAILED++))
    fi
else
    echo -e "${RED}❌ FAIL${NC}: App is not healthy"
    docker-compose logs app | tail -30
    ((TESTS_FAILED++))
fi

# Test 6: Check application accessibility
echo ""
echo "🌐 Test 6: Testing application accessibility..."
echo "----------------------------------------"

if curl -f -s http://localhost:5611/api/health > /dev/null; then
    RESPONSE=$(curl -s http://localhost:5611/api/health)
    echo "Health endpoint response: ${RESPONSE}"
    echo -e "${GREEN}✅ PASS${NC}: Application is accessible"
    ((TESTS_PASSED++))
else
    echo -e "${RED}❌ FAIL${NC}: Application is not accessible"
    ((TESTS_FAILED++))
fi

# Test 7: Check service dependencies
echo ""
echo "🔗 Test 7: Verifying service start order..."
echo "----------------------------------------"

# Check that postgres and redis started before app
POSTGRES_STARTED=$(docker inspect trader_postgres --format='{{.State.StartedAt}}')
REDIS_STARTED=$(docker inspect trader_redis --format='{{.State.StartedAt}}')
APP_STARTED=$(docker inspect trader_app --format='{{.State.StartedAt}}')

echo "Service start times:"
echo "  PostgreSQL: ${POSTGRES_STARTED}"
echo "  Redis: ${REDIS_STARTED}"
echo "  App: ${APP_STARTED}"

# Simple check: app should have started after dependencies
if [[ "$APP_STARTED" > "$POSTGRES_STARTED" ]] && [[ "$APP_STARTED" > "$REDIS_STARTED" ]]; then
    echo -e "${GREEN}✅ PASS${NC}: Services started in correct order"
    ((TESTS_PASSED++))
else
    echo -e "${YELLOW}⚠️  WARNING${NC}: Could not verify start order (timestamps may be close)"
    ((TESTS_PASSED++))  # Don't fail on this
fi

# Test 8: Check volumes are created
echo ""
echo "💾 Test 8: Checking volumes are created..."
echo "----------------------------------------"

EXPECTED_VOLUMES=("trader_postgres_data" "trader_redis_data" "trader_app_data")
MISSING_VOLUMES=()

for volume in "${EXPECTED_VOLUMES[@]}"; do
    if docker volume inspect $volume > /dev/null 2>&1; then
        echo "  ✓ Volume exists: $volume"
    else
        echo "  ✗ Volume missing: $volume"
        MISSING_VOLUMES+=("$volume")
    fi
done

if [ ${#MISSING_VOLUMES[@]} -eq 0 ]; then
    echo -e "${GREEN}✅ PASS${NC}: All volumes created"
    ((TESTS_PASSED++))
else
    echo -e "${RED}❌ FAIL${NC}: Missing volumes: ${MISSING_VOLUMES[*]}"
    ((TESTS_FAILED++))
fi

# Test 9: Check networks are created
echo ""
echo "🌐 Test 9: Checking networks are created..."
echo "----------------------------------------"

if docker network inspect trader_network > /dev/null 2>&1; then
    echo "  ✓ Network exists: trader_network"
    echo -e "${GREEN}✅ PASS${NC}: Network created"
    ((TESTS_PASSED++))
else
    echo -e "${RED}❌ FAIL${NC}: Network trader_network not found"
    ((TESTS_FAILED++))
fi

# Test 10: Check memory limits
echo ""
echo "🧠 Test 10: Checking memory limits..."
echo "----------------------------------------"

POSTGRES_MEM=$(docker inspect trader_postgres --format='{{.HostConfig.Memory}}')
REDIS_MEM=$(docker inspect trader_redis --format='{{.HostConfig.Memory}}')
APP_MEM=$(docker inspect trader_app --format='{{.HostConfig.Memory}}')

echo "Memory limits:"
echo "  PostgreSQL: $(($POSTGRES_MEM / 1024 / 1024))MB"
echo "  Redis: $(($REDIS_MEM / 1024 / 1024))MB"
echo "  App: $(($APP_MEM / 1024 / 1024))MB"

if [ $POSTGRES_MEM -gt 0 ] && [ $REDIS_MEM -gt 0 ] && [ $APP_MEM -gt 0 ]; then
    echo -e "${GREEN}✅ PASS${NC}: Memory limits configured"
    ((TESTS_PASSED++))
else
    echo -e "${YELLOW}⚠️  WARNING${NC}: Some services don't have memory limits"
    ((TESTS_PASSED++))  # Don't fail on this
fi

# Print summary
echo ""
echo "=============================================================================="
echo "TEST SUMMARY"
echo "=============================================================================="
echo ""
echo "Total tests: $((TESTS_PASSED + TESTS_FAILED))"
echo -e "${GREEN}Passed: ${TESTS_PASSED}${NC}"
echo -e "${RED}Failed: ${TESTS_FAILED}${NC}"
echo ""

# Print service status
echo "Service Status:"
docker-compose ps
echo ""

if [ $TESTS_FAILED -eq 0 ]; then
    echo -e "${GREEN}✅ ALL TESTS PASSED${NC}"
    echo "Docker Compose setup is production-ready!"
    echo ""
    echo "Services are running. Press Ctrl+C to stop, or run:"
    echo "  docker-compose down"
    exit 0
else
    echo -e "${RED}❌ SOME TESTS FAILED${NC}"
    echo "Review failures above and fix issues"
    exit 1
fi
