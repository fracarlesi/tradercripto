#!/bin/bash

# Deploy Trading Bots to Hetzner VPS
# Usage: ./deploy.sh [crypto|ib|all]

set -e

VPS_IP="<VPS_IP_REDACTED>"
DEPLOY_DIR="/opt/hlquantbot"
MODE="${1:-crypto}"  # Default: crypto only

echo "=== Deploying Trading Bots to $VPS_IP (mode: $MODE) ==="
echo ""

# Step 1: Create directory on VPS
echo "[1/6] Creating directory on VPS..."
ssh root@$VPS_IP "mkdir -p $DEPLOY_DIR/logs $DEPLOY_DIR/ib_bot/logs"

# Step 2: Copy files to VPS
echo "[2/6] Copying files to VPS..."
rsync -avz --delete \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.env' \
    --exclude='logs' \
    --exclude='venv' \
    --exclude='.venv' \
    --exclude='.claude' \
    --exclude='.serena' \
    --exclude='.beads' \
    --exclude='.pytest_cache' \
    --exclude='.ruff_cache' \
    --exclude='*.log' \
    --exclude='node_modules' \
    --exclude='firebase-debug.log' \
    ./ root@$VPS_IP:$DEPLOY_DIR/

# Step 3: Copy .env file
echo "[3/6] Copying .env file..."
scp .env root@$VPS_IP:$DEPLOY_DIR/.env

# Step 4: Stop existing containers
echo "[4/6] Stopping existing containers..."
ssh root@$VPS_IP "cd $DEPLOY_DIR && docker compose down 2>/dev/null || true"

# Step 5: Build and start services
echo "[5/6] Building and starting services..."
case $MODE in
    crypto)
        ssh root@$VPS_IP "cd $DEPLOY_DIR && docker compose build crypto_bot --no-cache && docker compose up -d crypto_bot"
        ;;
    ib)
        ssh root@$VPS_IP "cd $DEPLOY_DIR && docker compose --profile ib build ib_bot --no-cache && docker compose --profile ib up -d ib_bot"
        ;;
    all)
        ssh root@$VPS_IP "cd $DEPLOY_DIR && docker compose --profile ib build --no-cache && docker compose --profile ib up -d"
        ;;
    *)
        echo "Unknown mode: $MODE. Use: crypto, ib, or all"
        exit 1
        ;;
esac

# Step 6: Wait and show status
echo "[6/6] Waiting for services to start..."
sleep 10
ssh root@$VPS_IP "cd $DEPLOY_DIR && docker compose --profile ib ps"

echo ""
echo "=== Deployment complete! ==="
echo ""
echo "Services:"
echo "  - Crypto Bot: docker compose logs -f crypto_bot"
echo "  - IB Bot:     docker compose --profile ib logs -f ib_bot"
echo ""
echo "Useful commands:"
echo "  ssh root@$VPS_IP"
echo "  cd $DEPLOY_DIR"
echo "  docker compose ps                      # Check crypto bot status"
echo "  docker compose --profile ib ps         # Check all bots status"
echo "  docker compose restart crypto_bot      # Restart crypto bot"
