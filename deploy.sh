#!/bin/bash

# Deploy Trading Bots to Hetzner VPS
# Usage: ./deploy.sh [crypto|paper|ib|all]

set -e

# Load deploy config from gitignored file
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ ! -f "$SCRIPT_DIR/deploy.env" ]; then
    echo "ERROR: deploy.env not found. Copy deploy.env.example and fill in your values."
    exit 1
fi
source "$SCRIPT_DIR/deploy.env"

VPS_USER="${VPS_USER:-root}"
MODE="${1:-crypto}"  # Default: crypto only

echo "=== Deploying Trading Bots to $VPS_IP (mode: $MODE) ==="
echo ""

# Step 1: Create directory on VPS
echo "[1/6] Creating directory on VPS..."
ssh $VPS_USER@$VPS_IP "mkdir -p $DEPLOY_DIR/logs $DEPLOY_DIR/ib_bot/logs"

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
    ./ $VPS_USER@$VPS_IP:$DEPLOY_DIR/

# Step 3: Copy .env file(s)
echo "[3/6] Copying .env file(s)..."
scp .env $VPS_USER@$VPS_IP:$DEPLOY_DIR/.env
if [ -f .env.paper ]; then
    scp .env.paper $VPS_USER@$VPS_IP:$DEPLOY_DIR/.env.paper
fi

# Step 4: Stop target container(s) and rebuild
echo "[4/6] Stopping and rebuilding..."
case $MODE in
    crypto)
        ssh $VPS_USER@$VPS_IP "cd $DEPLOY_DIR && docker compose stop crypto_bot && docker compose rm -f crypto_bot && docker compose build crypto_bot --no-cache && docker compose up -d crypto_bot"
        ;;
    paper)
        ssh $VPS_USER@$VPS_IP "cd $DEPLOY_DIR && docker compose --profile paper stop crypto_bot_paper && docker compose --profile paper rm -f crypto_bot_paper && docker compose --profile paper build crypto_bot_paper --no-cache && docker compose --profile paper up -d crypto_bot_paper"
        ;;
    ib)
        ssh $VPS_USER@$VPS_IP "cd $DEPLOY_DIR && docker compose --profile ib stop ib_bot && docker compose --profile ib rm -f ib_bot && docker compose --profile ib build ib_bot --no-cache && docker compose --profile ib up -d ib_bot"
        ;;
    all)
        ssh $VPS_USER@$VPS_IP "cd $DEPLOY_DIR && docker compose --profile ib --profile paper down && docker compose --profile ib --profile paper build --no-cache && docker compose --profile ib --profile paper up -d"
        ;;
    *)
        echo "Unknown mode: $MODE. Use: crypto, paper, ib, or all"
        exit 1
        ;;
esac

# Step 6: Clean up old Docker images and build cache
echo "[6/6] Cleaning up old Docker images..."
ssh $VPS_USER@$VPS_IP "docker image prune -af && docker builder prune -af" 2>/dev/null || true

# Step 7: Wait and show status
echo "[7/6] Waiting for services to start..."
sleep 10
ssh $VPS_USER@$VPS_IP "cd $DEPLOY_DIR && docker compose --profile ib ps"

echo ""
echo "=== Deployment complete! ==="
echo ""
echo "Services:"
echo "  - Crypto Bot:       docker compose logs -f crypto_bot"
echo "  - Paper Bot:        docker compose --profile paper logs -f crypto_bot_paper"
echo "  - IB Bot:           docker compose --profile ib logs -f ib_bot"
echo ""
echo "Useful commands:"
echo "  ssh $VPS_USER@$VPS_IP"
echo "  cd $DEPLOY_DIR"
echo "  docker compose ps                                # Check crypto bot status"
echo "  docker compose --profile paper ps                # Check paper bot status"
echo "  docker compose --profile ib ps                   # Check IB bot status"
echo "  docker compose restart crypto_bot                # Restart crypto bot"
echo "  docker compose --profile paper restart crypto_bot_paper  # Restart paper bot"
