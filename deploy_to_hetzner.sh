#!/bin/bash

# =============================================================================
# Automated Deployment Script for Hetzner VPS
# =============================================================================
# This script automates the entire deployment process to a fresh Hetzner VPS
#
# Prerequisites:
# 1. A Hetzner VPS running Ubuntu 22.04 (CPX11 recommended: 2 vCPU, 2GB RAM, €3.79/mo)
# 2. SSH access to the VPS: ssh root@YOUR_VPS_IP
# 3. .env.production file with your API credentials (see .env.production.example)
#
# Usage:
#   ./deploy_to_hetzner.sh YOUR_VPS_IP
#
# Example:
#   ./deploy_to_hetzner.sh 95.217.123.45
# =============================================================================

set -e  # Exit on any error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Helper functions
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

# Check arguments
if [ $# -ne 1 ]; then
    print_error "Usage: $0 YOUR_VPS_IP"
    echo "Example: $0 95.217.123.45"
    exit 1
fi

VPS_IP=$1
VPS_USER="root"
APP_DIR="/opt/trader_bitcoin"

print_header "Bitcoin Trading Bot - Deployment to Hetzner VPS"
print_info "Target VPS: $VPS_IP"
print_info "User: $VPS_USER"
print_info "App Directory: $APP_DIR"

# Check if .env.production exists
if [ ! -f .env.production ]; then
    print_error ".env.production file not found!"
    echo ""
    echo "Please create .env.production with your API credentials:"
    echo "  cp .env.production.example .env.production"
    echo "  nano .env.production  # Edit with your keys"
    echo ""
    exit 1
fi

# Version tagging before deployment
print_header "Version Tagging"
print_info "Current deployment uses Docker image tag: \${IMAGE_TAG:-latest}"
echo ""

# Get current commit info
CURRENT_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
CURRENT_BRANCH=$(git branch --show-current 2>/dev/null || echo "unknown")
LATEST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || echo "none")

print_info "Current Git status:"
echo "  Branch: $CURRENT_BRANCH"
echo "  Commit: $CURRENT_COMMIT"
echo "  Latest tag: $LATEST_TAG"
echo ""

# Prompt for version tag
print_warning "Do you want to tag this deployment with a version?"
echo "Options:"
echo "  1. Use existing/latest tag (default): $LATEST_TAG"
echo "  2. Create new patch version (e.g., v1.0.0 → v1.0.1)"
echo "  3. Create new minor version (e.g., v1.0.0 → v1.1.0)"
echo "  4. Create new major version (e.g., v1.0.0 → v2.0.0)"
echo "  5. Specify custom version (e.g., v1.2.3)"
echo "  6. Skip versioning (use 'latest' tag)"
echo ""
read -p "Enter choice [1-6]: " version_choice

case "$version_choice" in
    1|"")
        # Use latest tag
        if [ "$LATEST_TAG" != "none" ]; then
            IMAGE_TAG="$LATEST_TAG"
            print_success "Using existing tag: $IMAGE_TAG"
        else
            print_warning "No existing tags found, using 'latest'"
            IMAGE_TAG="latest"
        fi
        ;;
    2)
        # Create patch version
        if [ -x "./tag_version.sh" ]; then
            ./tag_version.sh patch
            IMAGE_TAG=$(git describe --tags --abbrev=0)
            print_success "Created patch version: $IMAGE_TAG"
        else
            print_error "tag_version.sh not found or not executable"
            exit 1
        fi
        ;;
    3)
        # Create minor version
        if [ -x "./tag_version.sh" ]; then
            ./tag_version.sh minor
            IMAGE_TAG=$(git describe --tags --abbrev=0)
            print_success "Created minor version: $IMAGE_TAG"
        else
            print_error "tag_version.sh not found or not executable"
            exit 1
        fi
        ;;
    4)
        # Create major version
        if [ -x "./tag_version.sh" ]; then
            ./tag_version.sh major
            IMAGE_TAG=$(git describe --tags --abbrev=0)
            print_success "Created major version: $IMAGE_TAG"
        else
            print_error "tag_version.sh not found or not executable"
            exit 1
        fi
        ;;
    5)
        # Custom version
        read -p "Enter version (e.g., v1.2.3): " custom_version
        if [ -x "./tag_version.sh" ]; then
            ./tag_version.sh "$custom_version"
            IMAGE_TAG=$(git describe --tags --abbrev=0)
            print_success "Created custom version: $IMAGE_TAG"
        else
            print_error "tag_version.sh not found or not executable"
            exit 1
        fi
        ;;
    6)
        # Skip versioning
        IMAGE_TAG="latest"
        print_info "Skipping versioning, using 'latest' tag"
        ;;
    *)
        print_error "Invalid choice"
        exit 1
        ;;
esac

# Export IMAGE_TAG for docker compose
export IMAGE_TAG

print_success "Deployment will use Docker image: trader_bitcoin:$IMAGE_TAG"
echo ""

# Check SSH connection
print_header "Step 1: Testing SSH Connection"
if ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no $VPS_USER@$VPS_IP "echo 'SSH connection successful'" > /dev/null 2>&1; then
    print_success "SSH connection to $VPS_IP successful"
else
    print_error "Cannot connect to VPS via SSH"
    echo ""
    echo "Make sure:"
    echo "  1. VPS is running and accessible"
    echo "  2. SSH key is configured: ssh-copy-id $VPS_USER@$VPS_IP"
    echo "  3. Or you can manually enter password when prompted"
    echo ""
    exit 1
fi

# Install Docker on VPS
print_header "Step 2: Installing Docker and Dependencies"
ssh $VPS_USER@$VPS_IP << 'ENDSSH'
set -e

echo "Updating package list..."
apt-get update -qq

echo "Installing prerequisites..."
apt-get install -y -qq apt-transport-https ca-certificates curl software-properties-common git

echo "Adding Docker repository..."
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | apt-key add -
add-apt-repository -y "deb [arch=amd64] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable"

echo "Installing Docker..."
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin

echo "Starting Docker service..."
systemctl start docker
systemctl enable docker

echo "✓ Docker installation complete"
docker --version
docker compose version
ENDSSH
print_success "Docker and dependencies installed on VPS"

# Create app directory
print_header "Step 3: Creating Application Directory"
ssh $VPS_USER@$VPS_IP "mkdir -p $APP_DIR"
print_success "App directory created: $APP_DIR"

# Copy project files to VPS
print_header "Step 4: Copying Project Files"
print_info "This may take a few minutes..."

# Create temporary archive
TEMP_ARCHIVE=$(mktemp)
tar czf "$TEMP_ARCHIVE" \
    --exclude='node_modules' \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.venv' \
    --exclude='backend/data.db*' \
    --exclude='frontend/dist' \
    --exclude='frontend/.vite' \
    --exclude='.env' \
    --exclude='.env.local' \
    backend/ frontend/ docker-compose.simple.yml Dockerfile README.md package.json pnpm-lock.yaml pnpm-workspace.yaml

# Copy archive to VPS
scp "$TEMP_ARCHIVE" $VPS_USER@$VPS_IP:"$APP_DIR/project.tar.gz"

# Extract on VPS
ssh $VPS_USER@$VPS_IP << ENDSSH
cd $APP_DIR
tar xzf project.tar.gz
rm project.tar.gz
ENDSSH

# Clean up local archive
rm "$TEMP_ARCHIVE"

print_success "Project files copied to VPS"

# Copy .env.production
print_header "Step 5: Setting Up Environment Variables"
scp .env.production $VPS_USER@$VPS_IP:"$APP_DIR/.env"
print_success ".env.production copied to VPS as .env"

# Build and start Docker containers
print_header "Step 6: Building and Starting Docker Containers"
print_info "Building Docker image (this will take 5-10 minutes)..."

ssh $VPS_USER@$VPS_IP << ENDSSH
cd $APP_DIR

# Build and start using simplified compose file
docker compose -f docker-compose.simple.yml up -d --build

echo ""
echo "Waiting for container to be ready..."
sleep 10

# Check container status
docker compose -f docker-compose.simple.yml ps
ENDSSH

print_success "Docker containers built and started"

# Run database migrations
print_header "Step 7: Running Database Migrations"
ssh $VPS_USER@$VPS_IP << ENDSSH
cd $APP_DIR

# Run migrations inside container (backend code is at /app, not /app/backend)
docker compose -f docker-compose.simple.yml exec -T app sh -c "alembic upgrade head"
ENDSSH
print_success "Database migrations complete"

# Health check
print_header "Step 8: Verifying Deployment"
sleep 5  # Wait for app to fully start

# Check if app is responding
if ssh $VPS_USER@$VPS_IP "curl -f http://localhost:5611/api/health" > /dev/null 2>&1; then
    print_success "Application is running and responding to health checks"
else
    print_warning "Application started but health check failed - checking logs..."
    ssh $VPS_USER@$VPS_IP "cd $APP_DIR && docker compose -f docker-compose.simple.yml logs --tail=50"
fi

# Print deployment summary
print_header "Deployment Complete! 🚀"

echo ""
echo -e "${GREEN}Your Bitcoin Trading Bot is now running 24/7 on Hetzner VPS!${NC}"
echo ""
echo "Access your bot at:"
echo -e "  ${BLUE}http://$VPS_IP:5611${NC}"
echo ""
echo "Useful commands:"
echo -e "  ${YELLOW}View logs:${NC}"
echo "    ssh $VPS_USER@$VPS_IP 'cd $APP_DIR && docker compose -f docker-compose.simple.yml logs -f'"
echo ""
echo -e "  ${YELLOW}Stop bot:${NC}"
echo "    ssh $VPS_USER@$VPS_IP 'cd $APP_DIR && docker compose -f docker-compose.simple.yml stop'"
echo ""
echo -e "  ${YELLOW}Start bot:${NC}"
echo "    ssh $VPS_USER@$VPS_IP 'cd $APP_DIR && docker compose -f docker-compose.simple.yml start'"
echo ""
echo -e "  ${YELLOW}Restart bot:${NC}"
echo "    ssh $VPS_USER@$VPS_IP 'cd $APP_DIR && docker compose -f docker-compose.simple.yml restart'"
echo ""
echo -e "  ${YELLOW}View status:${NC}"
echo "    ssh $VPS_USER@$VPS_IP 'cd $APP_DIR && docker compose -f docker-compose.simple.yml ps'"
echo ""
echo -e "  ${YELLOW}Update bot (after git push):${NC}"
echo "    ./deploy_to_hetzner.sh $VPS_IP"
echo ""

print_info "Next steps:"
echo "  1. Check logs to ensure trading bot is syncing: ssh $VPS_USER@$VPS_IP 'cd $APP_DIR && docker compose -f docker-compose.simple.yml logs -f'"
echo "  2. Open http://$VPS_IP:5611 in browser to see trading dashboard"
echo "  3. Monitor your first AI trading decision (happens every 3 minutes)"
echo ""

print_warning "IMPORTANT: Configure firewall to allow port 5611"
echo "Run on VPS: ufw allow 5611/tcp && ufw enable"
echo ""

print_success "Deployment successful! Your bot is trading 24/7 ⚡"
