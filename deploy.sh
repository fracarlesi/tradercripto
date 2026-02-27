#!/bin/bash

# Deploy HLQuantBot to Hetzner VPS

set -e

VPS_IP="<VPS_IP_REDACTED>"
DEPLOY_DIR="/opt/hlquantbot"

echo "=== Deploying HLQuantBot to $VPS_IP ==="
echo ""

# Step 1: Create directory on VPS
echo "[1/6] Creating directory on VPS..."
ssh root@$VPS_IP "mkdir -p $DEPLOY_DIR/logs"

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
    --exclude='.pytest_cache' \
    --exclude='*.log' \
    --exclude='node_modules' \
    ./ root@$VPS_IP:$DEPLOY_DIR/

# Step 3: Copy .env file
echo "[3/6] Copying .env file..."
scp .env root@$VPS_IP:$DEPLOY_DIR/.env

# Step 4: Stop existing containers
echo "[4/6] Stopping existing containers..."
ssh root@$VPS_IP "cd $DEPLOY_DIR && docker compose down 2>/dev/null || true"

# Step 5: Build and start services
echo "[5/6] Building and starting services..."
ssh root@$VPS_IP "cd $DEPLOY_DIR && docker compose build --no-cache && docker compose up -d"

# Step 6: Wait and show status
echo "[6/6] Waiting for services to start..."
sleep 10
ssh root@$VPS_IP "cd $DEPLOY_DIR && docker compose ps"

echo ""
echo "=== Deployment complete! ==="
echo ""
echo "Services:"
echo "  - Bot: Running as daemon"
echo ""
echo "Useful commands:"
echo "  ssh root@$VPS_IP"
echo "  cd $DEPLOY_DIR"
echo "  docker compose logs -f bot       # View bot logs"
echo "  docker compose restart bot       # Restart bot"
echo "  docker compose ps                # Check status"
