#!/bin/bash
# Ralph for Claude Code - Autonomous AI agent loop
# Adapted from snarktank/ralph for Claude Code CLI (Claude Max subscription)
# Usage: ./ralph.sh [max_iterations]

set -e

MAX_ITERATIONS=${1:-20}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
PRD_FILE="$SCRIPT_DIR/prd.json"
PROGRESS_FILE="$SCRIPT_DIR/progress.txt"
PROMPT_FILE="$SCRIPT_DIR/prompt.md"
ARCHIVE_DIR="$SCRIPT_DIR/archive"
LAST_BRANCH_FILE="$SCRIPT_DIR/.last-branch"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}"
echo "╔═══════════════════════════════════════════════════════════╗"
echo "║           RALPH FOR CLAUDE CODE                           ║"
echo "║           Autonomous Trading Bot Optimizer                ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo -e "${NC}"

# Check prerequisites
if ! command -v claude &> /dev/null; then
    echo -e "${RED}Error: Claude Code CLI not found. Install with: npm install -g @anthropic-ai/claude-code${NC}"
    exit 1
fi

if ! command -v jq &> /dev/null; then
    echo -e "${RED}Error: jq not found. Install with: brew install jq${NC}"
    exit 1
fi

if [ ! -f "$PRD_FILE" ]; then
    echo -e "${RED}Error: prd.json not found at $PRD_FILE${NC}"
    echo "Create it first using the PRD skill or manually."
    exit 1
fi

if [ ! -f "$PROMPT_FILE" ]; then
    echo -e "${RED}Error: prompt.md not found at $PROMPT_FILE${NC}"
    exit 1
fi

# Archive previous run if branch changed
if [ -f "$PRD_FILE" ] && [ -f "$LAST_BRANCH_FILE" ]; then
  CURRENT_BRANCH=$(jq -r '.branchName // empty' "$PRD_FILE" 2>/dev/null || echo "")
  LAST_BRANCH=$(cat "$LAST_BRANCH_FILE" 2>/dev/null || echo "")

  if [ -n "$CURRENT_BRANCH" ] && [ -n "$LAST_BRANCH" ] && [ "$CURRENT_BRANCH" != "$LAST_BRANCH" ]; then
    DATE=$(date +%Y-%m-%d)
    FOLDER_NAME=$(echo "$LAST_BRANCH" | sed 's|^ralph/||')
    ARCHIVE_FOLDER="$ARCHIVE_DIR/$DATE-$FOLDER_NAME"

    echo -e "${YELLOW}Archiving previous run: $LAST_BRANCH${NC}"
    mkdir -p "$ARCHIVE_FOLDER"
    [ -f "$PRD_FILE" ] && cp "$PRD_FILE" "$ARCHIVE_FOLDER/"
    [ -f "$PROGRESS_FILE" ] && cp "$PROGRESS_FILE" "$ARCHIVE_FOLDER/"
    echo "   Archived to: $ARCHIVE_FOLDER"

    # Reset progress file for new run
    echo "# Ralph Progress Log" > "$PROGRESS_FILE"
    echo "Started: $(date)" >> "$PROGRESS_FILE"
    echo "Project: $(jq -r '.project' "$PRD_FILE")" >> "$PROGRESS_FILE"
    echo "---" >> "$PROGRESS_FILE"
  fi
fi

# Track current branch
if [ -f "$PRD_FILE" ]; then
  CURRENT_BRANCH=$(jq -r '.branchName // empty' "$PRD_FILE" 2>/dev/null || echo "")
  if [ -n "$CURRENT_BRANCH" ]; then
    echo "$CURRENT_BRANCH" > "$LAST_BRANCH_FILE"
  fi
fi

# Initialize progress file if it doesn't exist
if [ ! -f "$PROGRESS_FILE" ]; then
  echo "# Ralph Progress Log" > "$PROGRESS_FILE"
  echo "Started: $(date)" >> "$PROGRESS_FILE"
  echo "Project: $(jq -r '.project' "$PRD_FILE")" >> "$PROGRESS_FILE"
  echo "---" >> "$PROGRESS_FILE"
fi

# Show current status
echo -e "${BLUE}Project:${NC} $(jq -r '.project' "$PRD_FILE")"
echo -e "${BLUE}Branch:${NC} $(jq -r '.branchName' "$PRD_FILE")"
TOTAL_STORIES=$(jq '.userStories | length' "$PRD_FILE")
COMPLETED_STORIES=$(jq '[.userStories[] | select(.passes == true)] | length' "$PRD_FILE")
echo -e "${BLUE}Progress:${NC} $COMPLETED_STORIES / $TOTAL_STORIES stories complete"
echo ""

echo -e "${GREEN}Starting Ralph - Max iterations: $MAX_ITERATIONS${NC}"
echo -e "${YELLOW}Using Claude Max subscription for unlimited iterations${NC}"
echo ""

for i in $(seq 1 $MAX_ITERATIONS); do
  echo ""
  echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"
  echo -e "${BLUE}  Ralph Iteration $i of $MAX_ITERATIONS${NC}"
  echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"

  # Show remaining stories
  REMAINING=$(jq '[.userStories[] | select(.passes == false)] | length' "$PRD_FILE")
  NEXT_STORY=$(jq -r '[.userStories[] | select(.passes == false)] | sort_by(.priority) | .[0] | "\(.id): \(.title)"' "$PRD_FILE")
  echo -e "${YELLOW}Remaining stories: $REMAINING${NC}"
  echo -e "${YELLOW}Next: $NEXT_STORY${NC}"
  echo ""

  # Run Claude Code with the prompt
  # Using --dangerously-skip-permissions for autonomous mode (like amp --dangerously-allow-all)
  # The prompt is passed via -p flag
  cd "$PROJECT_DIR"

  OUTPUT=$(claude -p "$(cat "$PROMPT_FILE")" --dangerously-skip-permissions 2>&1 | tee /dev/stderr) || true

  # Check for completion signal
  if echo "$OUTPUT" | grep -q "<promise>COMPLETE</promise>"; then
    echo ""
    echo -e "${GREEN}╔═══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║           RALPH COMPLETED ALL TASKS!                      ║${NC}"
    echo -e "${GREEN}╚═══════════════════════════════════════════════════════════╝${NC}"
    echo "Completed at iteration $i of $MAX_ITERATIONS"
    echo ""
    echo "Final status:"
    jq '.userStories[] | "\(.id): \(.title) - \(if .passes then "✓ PASS" else "✗ FAIL" end)"' "$PRD_FILE"
    exit 0
  fi

  # Check for API limit (Claude Max should not hit this often)
  if echo "$OUTPUT" | grep -qi "rate limit\|quota exceeded\|too many requests"; then
    echo -e "${YELLOW}Rate limit detected. Waiting 60 seconds...${NC}"
    sleep 60
  fi

  # Update progress display
  COMPLETED_STORIES=$(jq '[.userStories[] | select(.passes == true)] | length' "$PRD_FILE")
  echo -e "${GREEN}Progress: $COMPLETED_STORIES / $TOTAL_STORIES stories complete${NC}"

  echo "Iteration $i complete. Continuing in 5 seconds..."
  sleep 5
done

echo ""
echo -e "${YELLOW}Ralph reached max iterations ($MAX_ITERATIONS) without completing all tasks.${NC}"
echo "Check progress.txt for status."
echo ""
echo "Remaining stories:"
jq -r '.userStories[] | select(.passes == false) | "  - \(.id): \(.title)"' "$PRD_FILE"
exit 1
