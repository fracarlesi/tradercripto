#!/bin/bash
# Start Ralph in Docker
# Usage: ./start-ralph.sh [max_iterations]

MAX_ITERATIONS=${1:-50}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "╔═══════════════════════════════════════════════════════════╗"
echo "║           RALPH FOR CLAUDE CODE - DOCKER                  ║"
echo "║           Trading Bot Optimizer                           ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo ""
echo "Max iterations: $MAX_ITERATIONS"
echo "Using Claude Max subscription credentials from ~/.claude"
echo ""

cd "$SCRIPT_DIR"

# Build and run
docker compose build
docker compose run --rm ralph ./scripts/ralph/ralph.sh "$MAX_ITERATIONS"
