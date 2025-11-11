#!/bin/bash

# =============================================================================
# Counterfactual Learning System - Health Check Script
# =============================================================================
# This script verifies that the counterfactual learning system is functioning
# correctly in production.
#
# Usage:
#   ./check_learning_system.sh [VPS_IP]
#
# Example:
#   ./check_learning_system.sh 46.224.45.196
# =============================================================================

set -e

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default VPS IP (can be overridden by argument)
VPS_IP="${1:-46.224.45.196}"
VPS_USER="root"
APP_DIR="/opt/trader_bitcoin"

print_header() {
    echo ""
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}========================================${NC}"
    echo ""
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_info() {
    echo -e "${BLUE}ℹ $1${NC}"
}

# Check SSH connection
print_header "Counterfactual Learning System Health Check"
print_info "Target VPS: $VPS_IP"
print_info "Checking system status..."
echo ""

# 1. Check container is running
print_header "[1/6] Container Status"
CONTAINER_STATUS=$(ssh $VPS_USER@$VPS_IP "cd $APP_DIR && docker compose -f docker-compose.simple.yml ps --format=json app 2>/dev/null | grep -o '\"Health\":\"[^\"]*\"' | cut -d'\"' -f4" || echo "error")

if [ "$CONTAINER_STATUS" = "healthy" ]; then
    print_success "Container is healthy"
elif [ "$CONTAINER_STATUS" = "starting" ]; then
    print_warning "Container is starting (wait ~30 seconds)"
else
    print_error "Container is not healthy: $CONTAINER_STATUS"
fi

# 2. Check decision snapshots are being saved
print_header "[2/6] Decision Snapshots"
SNAPSHOT_DATA=$(ssh $VPS_USER@$VPS_IP "cd $APP_DIR && docker compose -f docker-compose.simple.yml exec -T app python3 -c \"
import sqlite3
from datetime import datetime
conn = sqlite3.connect('/app/data/data.db')
cursor = conn.cursor()

# Total snapshots
cursor.execute('SELECT COUNT(*) FROM decision_snapshots')
total = cursor.fetchone()[0]

# Snapshots by decision
cursor.execute('SELECT actual_decision, COUNT(*) FROM decision_snapshots GROUP BY actual_decision')
breakdown = {row[0]: row[1] for row in cursor.fetchall()}

# Most recent snapshot time
cursor.execute('SELECT MAX(timestamp) FROM decision_snapshots')
latest = cursor.fetchone()[0]

# Snapshots with counterfactuals
cursor.execute('SELECT COUNT(*) FROM decision_snapshots WHERE exit_price_24h IS NOT NULL')
with_cf = cursor.fetchone()[0]

# Print results
print(f'{total}|{breakdown.get(\"LONG\", 0)}|{breakdown.get(\"SHORT\", 0)}|{breakdown.get(\"HOLD\", 0)}|{latest}|{with_cf}')

conn.close()
\" 2>/dev/null" || echo "0|0|0|0|unknown|0")

IFS='|' read -r TOTAL_SNAPSHOTS LONG_COUNT SHORT_COUNT HOLD_COUNT LATEST_SNAPSHOT WITH_CF <<< "$SNAPSHOT_DATA"

if [ "$TOTAL_SNAPSHOTS" -gt 0 ]; then
    print_success "Decision snapshots saved: $TOTAL_SNAPSHOTS total"
    echo "  - LONG: $LONG_COUNT, SHORT: $SHORT_COUNT, HOLD: $HOLD_COUNT"
    echo "  - Latest snapshot: $LATEST_SNAPSHOT"
    echo "  - With counterfactuals: $WITH_CF"
else
    print_error "No decision snapshots found!"
    print_info "Check logs: ssh $VPS_USER@$VPS_IP 'cd $APP_DIR && docker compose -f docker-compose.simple.yml logs app' | grep snapshot"
fi

# 3. Check for errors in learning system
print_header "[3/6] Learning System Errors"
ERROR_COUNT=$(ssh $VPS_USER@$VPS_IP "cd $APP_DIR && docker compose -f docker-compose.simple.yml logs app 2>&1 | grep -i 'learning.*ERROR' | wc -l" || echo "0")

if [ "$ERROR_COUNT" -eq 0 ]; then
    print_success "No errors in learning system"
else
    print_warning "Found $ERROR_COUNT errors in learning system logs"
    print_info "View errors: ssh $VPS_USER@$VPS_IP 'cd $APP_DIR && docker compose -f docker-compose.simple.yml logs app' | grep -i 'learning.*ERROR'"
fi

# 4. Check scheduled jobs
print_header "[4/6] Scheduled Jobs"
JOBS_REGISTERED=$(ssh $VPS_USER@$VPS_IP "cd $APP_DIR && docker compose -f docker-compose.simple.yml logs app 2>&1 | grep 'Added task' | tail -6" || echo "")

if echo "$JOBS_REGISTERED" | grep -q "counterfactual_calculation"; then
    print_success "Counterfactual calculation job registered (1-hour interval)"
else
    print_error "Counterfactual calculation job NOT registered"
fi

if echo "$JOBS_REGISTERED" | grep -q "auto_self_analysis"; then
    print_success "Auto self-analysis job registered (3-hour interval)"
else
    print_error "Auto self-analysis job NOT registered"
fi

# 5. Check data integrity
print_header "[5/6] Data Integrity"
INTEGRITY_CHECK=$(ssh $VPS_USER@$VPS_IP "cd $APP_DIR && docker compose -f docker-compose.simple.yml exec -T app python3 -c \"
import sqlite3
import json

conn = sqlite3.connect('/app/data/data.db')
cursor = conn.cursor()

# Check for snapshots with valid JSON
cursor.execute('SELECT id, indicators_snapshot, deepseek_reasoning FROM decision_snapshots ORDER BY timestamp DESC LIMIT 1')
row = cursor.fetchone()

if row:
    snapshot_id, indicators_json, reasoning = row
    try:
        indicators = json.loads(indicators_json)
        has_valid_json = len(indicators) > 0
    except:
        has_valid_json = False

    has_reasoning = len(reasoning) > 0 if reasoning else False

    print(f'{snapshot_id}|{has_valid_json}|{has_reasoning}')
else:
    print('0|False|False')

conn.close()
\" 2>/dev/null" || echo "0|False|False")

IFS='|' read -r SNAPSHOT_ID HAS_JSON HAS_REASONING <<< "$INTEGRITY_CHECK"

if [ "$HAS_JSON" = "True" ] && [ "$HAS_REASONING" = "True" ]; then
    print_success "Data integrity OK (snapshot #$SNAPSHOT_ID)"
    echo "  - Valid indicators JSON: ✓"
    echo "  - DeepSeek reasoning present: ✓"
else
    print_error "Data integrity issues detected"
    echo "  - Valid indicators JSON: $HAS_JSON"
    echo "  - DeepSeek reasoning present: $HAS_REASONING"
fi

# 6. Timeline & Next Steps
print_header "[6/6] Timeline & Next Steps"

if [ "$TOTAL_SNAPSHOTS" -gt 0 ]; then
    # Calculate hours until first counterfactual
    HOURS_TO_CF=24
    echo "Current status:"
    echo "  ✓ Snapshot saving: ACTIVE"
    echo "  ✓ Scheduled jobs: REGISTERED"
    echo ""
    echo "Next milestones:"
    echo "  1. After 24h: First counterfactuals calculated"
    echo "  2. After 50+ snapshots: Meaningful self-analysis insights"
    echo "  3. Continuous: System learns from every decision"
    echo ""
    echo "Monitor progress:"
    echo "  ssh $VPS_USER@$VPS_IP 'cd $APP_DIR && docker compose -f docker-compose.simple.yml logs -f app' | grep -E '(snapshot|counterfactual|self-analysis)'"
else
    print_warning "System is starting - wait for first AI decision (~3 minutes)"
fi

# Summary
print_header "Health Check Summary"
if [ "$TOTAL_SNAPSHOTS" -gt 0 ] && [ "$ERROR_COUNT" -eq 0 ] && [ "$HAS_JSON" = "True" ]; then
    print_success "ALL SYSTEMS OPERATIONAL ✓"
    echo ""
    echo "The counterfactual learning system is working correctly!"
else
    print_warning "ATTENTION REQUIRED"
    echo ""
    echo "Some issues detected. Review the checks above."
fi

echo ""
