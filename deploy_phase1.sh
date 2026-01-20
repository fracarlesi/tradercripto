#!/bin/bash
# =============================================================================
# HLQuantBot Phase 1 Deployment Script
# =============================================================================
# Deploys Phase 1 (P0) improvements to Hetzner VPS:
# - Cooldown system
# - Performance metrics
# - Graduated ROI
# - Protection system
# - Config updates (leverage 5x, disable shorts)
# =============================================================================

set -e  # Exit on error

# Configuration
SERVER_IP="<VPS_IP_REDACTED>"
SERVER_USER="root"
DEPLOY_DIR="/opt/hlquantbot"
DB_NAME="trading_db"
DB_USER="trader"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║        HLQuantBot Phase 1 Deployment to Production            ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""

# Step 1: Pre-flight checks
echo -e "${YELLOW}[1/8] Pre-flight checks...${NC}"
if ! ssh -o ConnectTimeout=5 ${SERVER_USER}@${SERVER_IP} "echo 'SSH connection OK'" > /dev/null 2>&1; then
    echo -e "${RED}ERROR: Cannot connect to server ${SERVER_IP}${NC}"
    exit 1
fi
echo -e "${GREEN}✓ SSH connection OK${NC}"

# Step 2: Backup database
echo ""
echo -e "${YELLOW}[2/8] Backing up database...${NC}"
BACKUP_FILE="backup_$(date +%Y%m%d_%H%M%S).sql"
ssh ${SERVER_USER}@${SERVER_IP} "docker exec hlquantbot_postgres pg_dump -U ${DB_USER} ${DB_NAME} > /tmp/${BACKUP_FILE}"
ssh ${SERVER_USER}@${SERVER_IP} "cp /tmp/${BACKUP_FILE} ${DEPLOY_DIR}/backups/"
echo -e "${GREEN}✓ Database backed up to ${DEPLOY_DIR}/backups/${BACKUP_FILE}${NC}"

# Step 3: Sync code (excluding tests, pycache, etc.)
echo ""
echo -e "${YELLOW}[3/8] Syncing code to production...${NC}"
rsync -avz --delete \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.pytest_cache' \
    --exclude '.ruff_cache' \
    --exclude 'tests/' \
    --exclude '.env' \
    --exclude '*.log' \
    --exclude '.git' \
    simple_bot/ ${SERVER_USER}@${SERVER_IP}:${DEPLOY_DIR}/simple_bot/

echo -e "${GREEN}✓ Code synced${NC}"

# Step 4: Sync migrations
echo ""
echo -e "${YELLOW}[4/8] Syncing database migrations...${NC}"
rsync -avz database/migrations/ ${SERVER_USER}@${SERVER_IP}:${DEPLOY_DIR}/database/migrations/
echo -e "${GREEN}✓ Migrations synced${NC}"

# Step 5: Run migrations
echo ""
echo -e "${YELLOW}[5/8] Running database migrations...${NC}"
ssh ${SERVER_USER}@${SERVER_IP} << 'ENDSSH'
cd /opt/hlquantbot
docker exec -i hlquantbot_postgres psql -U trader -d trading_db < database/migrations/006_add_cooldowns.sql
docker exec -i hlquantbot_postgres psql -U trader -d trading_db < database/migrations/007_add_protections.sql
ENDSSH
echo -e "${GREEN}✓ Migrations applied${NC}"

# Step 6: Verify migrations
echo ""
echo -e "${YELLOW}[6/8] Verifying migrations...${NC}"
ssh ${SERVER_USER}@${SERVER_IP} << 'ENDSSH'
docker exec -i hlquantbot_postgres psql -U trader -d trading_db << 'ENDPSQL'
\dt cooldowns
\dt protections
SELECT COUNT(*) as cooldowns_table_exists FROM cooldowns LIMIT 0;
SELECT COUNT(*) as protections_table_exists FROM protections LIMIT 0;
ENDPSQL
ENDSSH
echo -e "${GREEN}✓ Migrations verified${NC}"

# Step 7: Restart bot
echo ""
echo -e "${YELLOW}[7/8] Restarting trading bot...${NC}"
ssh ${SERVER_USER}@${SERVER_IP} "cd ${DEPLOY_DIR} && docker compose restart bot"
sleep 5
echo -e "${GREEN}✓ Bot restarted${NC}"

# Step 8: Verify services
echo ""
echo -e "${YELLOW}[8/8] Verifying services...${NC}"
ssh ${SERVER_USER}@${SERVER_IP} "cd ${DEPLOY_DIR} && docker compose ps"
echo ""

# Check logs
echo -e "${YELLOW}Checking recent logs for errors...${NC}"
ssh ${SERVER_USER}@${SERVER_IP} "cd ${DEPLOY_DIR} && docker compose logs --tail=20 bot | grep -iE 'error|warning|cooldown|protection' || echo 'No errors in recent logs'"
echo ""

echo -e "${GREEN}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║              Phase 1 Deployment Complete!                     ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${GREEN}✓ New Features Deployed:${NC}"
echo "  - Cooldown system (3 triggers: SL streak, drawdown, low performance)"
echo "  - Performance metrics (Sharpe, Sortino, Calmar, Max DD)"
echo "  - Graduated ROI (6 time-based thresholds)"
echo "  - Protection system (4 protections: StoplossGuard, MaxDrawdown, CooldownPeriod, LowPerformance)"
echo "  - Config updates (Leverage 1x→5x, Risk 1%→2%, Shorts disabled)"
echo ""
echo -e "${YELLOW}Next Steps:${NC}"
echo "  1. Monitor dashboard: http://<VPS_IP_REDACTED>:5000/"
echo "  2. Check bot logs: ssh ${SERVER_USER}@${SERVER_IP} 'cd ${DEPLOY_DIR} && docker compose logs -f bot'"
echo "  3. Watch for cooldown/protection triggers in first 24h"
echo "  4. Verify performance metrics calculating correctly"
echo "  5. Monitor Telegram alerts for new safety notifications"
echo ""
echo -e "${GREEN}Dashboard URL: http://<VPS_IP_REDACTED>:5000/${NC}"
echo -e "${GREEN}Frontend URL: http://<VPS_IP_REDACTED>:5611/${NC}"
echo ""
