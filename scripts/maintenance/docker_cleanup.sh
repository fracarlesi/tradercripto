#!/bin/bash
# Docker Cleanup Script - Remove old images and build cache
# Runs weekly to prevent disk space issues
# Created: 2025-11-14

set -e

LOG_FILE="/var/log/trader_bitcoin_docker_cleanup.log"

echo "========================================" | tee -a "$LOG_FILE"
echo "Docker Cleanup - $(date)" | tee -a "$LOG_FILE"
echo "========================================" | tee -a "$LOG_FILE"

# Check disk space before cleanup
echo "Disk space BEFORE cleanup:" | tee -a "$LOG_FILE"
df -h / | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

# Remove unused Docker images (keep images used in last 24h)
echo "Removing unused Docker images..." | tee -a "$LOG_FILE"
docker image prune -af --filter "until=24h" 2>&1 | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

# Remove unused build cache (keep cache used in last 7 days)
echo "Removing unused build cache..." | tee -a "$LOG_FILE"
docker builder prune -af --filter "until=168h" 2>&1 | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

# Remove stopped containers older than 7 days
echo "Removing old stopped containers..." | tee -a "$LOG_FILE"
docker container prune -f --filter "until=168h" 2>&1 | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

# Check disk space after cleanup
echo "Disk space AFTER cleanup:" | tee -a "$LOG_FILE"
df -h / | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

# Alert if disk usage is still high (>80%)
DISK_USAGE=$(df / | tail -1 | awk '{print $5}' | sed 's/%//')
if [ "$DISK_USAGE" -gt 80 ]; then
    echo "WARNING: Disk usage still high after cleanup: ${DISK_USAGE}%" | tee -a "$LOG_FILE"
    echo "Manual intervention may be required!" | tee -a "$LOG_FILE"
else
    echo "SUCCESS: Disk usage is healthy: ${DISK_USAGE}%" | tee -a "$LOG_FILE"
fi

echo "========================================" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"
