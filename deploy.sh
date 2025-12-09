#!/bin/bash

# Deploy MAINNET trading bot to Hetzner VPS

set -e

VPS_IP="<VPS_IP_REDACTED>"
DEPLOY_DIR="/opt/trader_bitcoin"

echo "=== Deploying MAINNET Trading Bot to $VPS_IP ==="
echo "WARNING: This is REAL MONEY trading!"
echo ""

# Step 1: Copy files to VPS
echo "Copying files to VPS..."
ssh root@$VPS_IP "mkdir -p $DEPLOY_DIR/logs"
rsync -avz --delete \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.env' \
    --exclude='.env.backup' \
    --exclude='deploy.sh.backup' \
    --exclude='logs' \
    --exclude='venv' \
    --exclude='.claude' \
    ./ root@$VPS_IP:$DEPLOY_DIR/

# Step 2: Copy .env file
echo "Copying .env file..."
scp .env root@$VPS_IP:$DEPLOY_DIR/.env

# Step 3: Set permissions
echo "Setting permissions..."
ssh root@$VPS_IP "chmod +x $DEPLOY_DIR/run_bot.sh 2>/dev/null || true"

# Step 4: Build and start postgres only
echo "Building Docker image and starting PostgreSQL..."
ssh root@$VPS_IP "cd $DEPLOY_DIR && docker compose down && docker compose build --no-cache && docker compose up -d postgres"

# Step 5: Wait for postgres
echo "Waiting for PostgreSQL to be ready..."
sleep 60

# Step 6: Start dashboard and app (daemon mode)
echo "Starting dashboard and trading bot..."
ssh root@$VPS_IP "cd $DEPLOY_DIR && docker compose up -d dashboard app"

# Step 7: Show status
echo ""
echo "=== MAINNET Deployment complete! ==="
echo ""
echo "Ambiente: MAINNET (Hyperliquid REAL)"
echo "Dashboard: http://$VPS_IP:5611/"
echo "API Status: http://$VPS_IP:8080/status"
echo "PostgreSQL: porta 5432"
echo ""
echo "Il bot gira come daemon (non serve cron)."
echo ""
echo "Comandi utili:"
echo "  ssh root@$VPS_IP"
echo "  cd $DEPLOY_DIR"
echo "  docker compose logs -f app    # Vedi logs"
echo "  docker compose restart app    # Riavvia"
