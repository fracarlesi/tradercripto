#!/bin/bash

# Deploy Trading Bots to Hetzner VPS
# Usage: ./deploy.sh [crypto|paper|ib|all]

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
    --exclude='.env.paper' \
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

# Step 3: Copy .env file(s)
echo "[3/6] Copying .env file(s)..."
scp .env root@$VPS_IP:$DEPLOY_DIR/.env
if [ -f .env.paper ]; then
    scp .env.paper root@$VPS_IP:$DEPLOY_DIR/.env.paper
fi

# Step 4: Stop existing containers
echo "[4/6] Stopping existing containers..."
ssh root@$VPS_IP "cd $DEPLOY_DIR && docker compose down 2>/dev/null || true"

# Step 5: Build and start services
echo "[5/6] Building and starting services..."
case $MODE in
    crypto)
        ssh root@$VPS_IP "cd $DEPLOY_DIR && docker compose build crypto_bot --no-cache && docker compose up -d crypto_bot"
        ;;
    paper)
        ssh root@$VPS_IP "cd $DEPLOY_DIR && docker compose --profile paper build crypto_bot_paper --no-cache && docker compose --profile paper up -d crypto_bot_paper"
        ;;
    ib)
        ssh root@$VPS_IP "cd $DEPLOY_DIR && docker compose --profile ib build ib_bot --no-cache && docker compose --profile ib up -d ib_bot"
        ;;
    all)
        ssh root@$VPS_IP "cd $DEPLOY_DIR && docker compose --profile ib --profile paper build --no-cache && docker compose --profile ib --profile paper up -d"
        ;;
    *)
        echo "Unknown mode: $MODE. Use: crypto, paper, ib, or all"
        exit 1
        ;;
esac

# Step 6: Clean up old Docker images and build cache
echo "[6/6] Cleaning up old Docker images..."
ssh root@$VPS_IP "docker image prune -af && docker builder prune -af" 2>/dev/null || true

# Step 7: Wait and show status
echo "[7/6] Waiting for services to start..."
sleep 10
ssh root@$VPS_IP "cd $DEPLOY_DIR && docker compose --profile ib ps"

echo ""
echo "=== Deployment complete! ==="
echo ""
echo "Services:"
echo "  - Crypto Bot:       docker compose logs -f crypto_bot"
echo "  - Paper Bot:        docker compose --profile paper logs -f crypto_bot_paper"
echo "  - IB Bot:           docker compose --profile ib logs -f ib_bot"
echo ""
echo "Useful commands:"
echo "  ssh root@$VPS_IP"
echo "  cd $DEPLOY_DIR"
echo "  docker compose ps                                # Check crypto bot status"
echo "  docker compose --profile paper ps                # Check paper bot status"
echo "  docker compose --profile ib ps                   # Check IB bot status"
echo "  docker compose restart crypto_bot                # Restart crypto bot"
echo "  docker compose --profile paper restart crypto_bot_paper  # Restart paper bot"
