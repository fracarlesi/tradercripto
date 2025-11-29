#!/bin/bash

# Deploy Rizzo trading bot (GPT 5.1 via OpenRouter) to Hetzner VPS

set -e

VPS_IP="<VPS_IP_REDACTED>"
DEPLOY_DIR="/opt/trader_bitcoin"

echo "=== Deploying Rizzo Trading Bot to $VPS_IP ==="

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
ssh root@$VPS_IP "chmod +x $DEPLOY_DIR/run_bot.sh"

# Step 4: Build and start postgres only
echo "Building Docker image and starting PostgreSQL..."
ssh root@$VPS_IP "cd $DEPLOY_DIR && docker compose down && docker compose build --no-cache && docker compose up -d postgres"

# Step 5: Wait for postgres
echo "Waiting for PostgreSQL to be ready..."
sleep 10

# Step 6: Skip automatic bot execution (let cron handle it)
# To test manually: ssh root@$VPS_IP "cd $DEPLOY_DIR && docker compose run --rm app python main.py"

echo ""
echo "=== Deployment complete! ==="
echo ""
echo "Next steps on server:"
echo "1. SSH to server: ssh root@$VPS_IP"
echo "2. Configure cron: crontab -e"
echo "3. Add this line:"
echo "   */15 * * * * $DEPLOY_DIR/run_bot.sh"
echo ""
echo "To view logs: tail -f $DEPLOY_DIR/logs/bot.log"
