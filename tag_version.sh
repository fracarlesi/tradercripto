#!/bin/bash

# =============================================================================
# Semantic Versioning Helper Script
# =============================================================================
# Creates Git tags following semantic versioning (vMAJOR.MINOR.PATCH)
# Automatically generates changelog from commits since last version
#
# Usage:
#   ./tag_version.sh patch      # Bump patch version (v1.0.0 -> v1.0.1)
#   ./tag_version.sh minor      # Bump minor version (v1.0.0 -> v1.1.0)
#   ./tag_version.sh major      # Bump major version (v1.0.0 -> v2.0.0)
#   ./tag_version.sh v1.2.3     # Create specific version tag
# =============================================================================

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Helper functions
print_error() {
    echo -e "${RED}❌ ERROR: $1${NC}"
}

print_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

print_info() {
    echo -e "${BLUE}ℹ️  $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

print_header() {
    echo ""
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}========================================${NC}"
    echo ""
}

# Validate we're in a Git repository
if ! git rev-parse --git-dir > /dev/null 2>&1; then
    print_error "Not a Git repository"
    exit 1
fi

# Check for uncommitted changes
if ! git diff-index --quiet HEAD --; then
    print_error "You have uncommitted changes. Please commit or stash them first."
    git status --short
    exit 1
fi

# Get current branch
CURRENT_BRANCH=$(git branch --show-current)
if [ "$CURRENT_BRANCH" != "main" ]; then
    print_warning "You are not on the 'main' branch (current: $CURRENT_BRANCH)"
    read -p "Continue anyway? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Get latest version tag
LATEST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || echo "v0.0.0")
print_info "Latest version: $LATEST_TAG"

# Parse version components
if [[ $LATEST_TAG =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
    MAJOR="${BASH_REMATCH[1]}"
    MINOR="${BASH_REMATCH[2]}"
    PATCH="${BASH_REMATCH[3]}"
else
    # No valid tag found, start from v0.0.0
    MAJOR=0
    MINOR=0
    PATCH=0
fi

# Determine new version based on argument
ARG="${1:-}"

if [ -z "$ARG" ]; then
    print_error "Usage: $0 <major|minor|patch|vX.Y.Z>"
    exit 1
fi

case "$ARG" in
    major)
        NEW_MAJOR=$((MAJOR + 1))
        NEW_MINOR=0
        NEW_PATCH=0
        NEW_VERSION="v${NEW_MAJOR}.${NEW_MINOR}.${NEW_PATCH}"
        ;;
    minor)
        NEW_MAJOR=$MAJOR
        NEW_MINOR=$((MINOR + 1))
        NEW_PATCH=0
        NEW_VERSION="v${NEW_MAJOR}.${NEW_MINOR}.${NEW_PATCH}"
        ;;
    patch)
        NEW_MAJOR=$MAJOR
        NEW_MINOR=$MINOR
        NEW_PATCH=$((PATCH + 1))
        NEW_VERSION="v${NEW_MAJOR}.${NEW_MINOR}.${NEW_PATCH}"
        ;;
    v*)
        # Custom version provided
        if [[ $ARG =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
            NEW_VERSION="$ARG"
        else
            print_error "Invalid version format. Expected: vX.Y.Z (e.g., v1.2.3)"
            exit 1
        fi
        ;;
    *)
        print_error "Invalid argument: $ARG"
        print_info "Usage: $0 <major|minor|patch|vX.Y.Z>"
        exit 1
        ;;
esac

# Check if tag already exists
if git rev-parse "$NEW_VERSION" >/dev/null 2>&1; then
    print_error "Tag $NEW_VERSION already exists"
    exit 1
fi

print_header "Creating New Version Tag"
print_info "New version: $NEW_VERSION"

# Generate changelog from commits since last tag
print_info "Generating changelog..."
CHANGELOG=$(git log ${LATEST_TAG}..HEAD --pretty=format:"- %s (%h)" --no-merges)

if [ -z "$CHANGELOG" ]; then
    print_warning "No new commits since $LATEST_TAG"
    CHANGELOG="- No changes"
fi

# Show changelog
print_header "Changelog"
echo "$CHANGELOG"

# Confirm before creating tag
echo ""
print_warning "This will create tag $NEW_VERSION with the above changelog"
read -p "Continue? (y/n) " -n 1 -r
echo

if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    print_info "Aborted"
    exit 0
fi

# Create annotated tag with changelog
TAG_MESSAGE="Release $NEW_VERSION

$CHANGELOG"

git tag -a "$NEW_VERSION" -m "$TAG_MESSAGE"

print_success "Tag $NEW_VERSION created successfully!"

# Ask if user wants to push tag to remote
echo ""
read -p "Push tag to remote? (y/n) " -n 1 -r
echo

if [[ $REPLY =~ ^[Yy]$ ]]; then
    git push origin "$NEW_VERSION"
    print_success "Tag pushed to remote"
fi

print_header "Summary"
print_success "Version $NEW_VERSION is ready"
print_info "Build Docker image with: IMAGE_TAG=$NEW_VERSION docker compose -f docker-compose.simple.yml build"
print_info "Deploy to production with: IMAGE_TAG=$NEW_VERSION ./deploy_to_hetzner.sh <VPS_IP>"
