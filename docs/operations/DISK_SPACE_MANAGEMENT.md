# Disk Space Management - Automated Docker Cleanup

**Date**: 2025-11-14
**Status**: ✅ Implemented
**VPS**: 46.224.45.196

---

## 🎯 Overview

Automated weekly cleanup of Docker images and build cache to prevent disk space exhaustion on production VPS.

### Problem Solved

- **Initial Issue**: Disk at 100% (36G/38G) caused database I/O errors and system failure
- **Root Cause**: Accumulation of old Docker images from multiple deployments
- **Solution**: Weekly automated cleanup + monitoring

---

## 📋 Automated Cleanup Configuration

### Cron Job Schedule

```bash
# Weekly cleanup: Every Sunday at 3:00 AM
0 3 * * 0 /opt/trader_bitcoin/scripts/maintenance/docker_cleanup.sh
```

**Why Sunday 3:00 AM?**
- Low trading activity time
- Minimal user impact
- Gives time for manual intervention before Monday trading

### What Gets Cleaned

1. **Docker Images** (older than 24h):
   ```bash
   docker image prune -af --filter "until=24h"
   ```
   - Removes unused images from previous deployments
   - Keeps images used in last 24h (current production image)

2. **Build Cache** (older than 7 days):
   ```bash
   docker builder prune -af --filter "until=168h"
   ```
   - Removes build artifacts from old builds
   - Keeps recent cache for faster rebuilds

3. **Stopped Containers** (older than 7 days):
   ```bash
   docker container prune -f --filter "until=168h"
   ```
   - Removes old stopped containers
   - Cleanup of failed deployments

---

## 📊 Monitoring

### Log File Location

```bash
/var/log/trader_bitcoin_docker_cleanup.log
```

### Check Last Cleanup

```bash
ssh root@46.224.45.196 'tail -50 /var/log/trader_bitcoin_docker_cleanup.log'
```

**Expected Output:**
```
========================================
Docker Cleanup - Sun Nov 17 03:00:01 UTC 2025
========================================
Disk space BEFORE cleanup:
Filesystem      Size  Used Avail Use% Mounted on
/dev/sda1        38G  12G   24G  34% /

Removing unused Docker images...
deleted: sha256:abc123...
deleted: sha256:def456...
Total reclaimed space: 8.5GB

Removing unused build cache...
Total:	2.3GB

Removing old stopped containers...
Total reclaimed space: 150MB

Disk space AFTER cleanup:
Filesystem      Size  Used Avail Use% Mounted on
/dev/sda1        38G  1.5G   35G  5% /

SUCCESS: Disk usage is healthy: 5%
========================================
```

### Manual Disk Space Check

```bash
# Current disk usage
ssh root@46.224.45.196 'df -h /'

# Docker disk usage breakdown
ssh root@46.224.45.196 'docker system df'
```

---

## 🚨 Alerts & Thresholds

### Automated Warning

The cleanup script includes an automatic check:
```bash
if [ "$DISK_USAGE" -gt 80 ]; then
    echo "WARNING: Disk usage still high after cleanup: ${DISK_USAGE}%"
fi
```

**If disk >80% after cleanup:**
- Manual intervention required
- Check log file for details: `/var/log/trader_bitcoin_docker_cleanup.log`
- Investigate unexpected disk usage (database, logs, etc.)

### Manual Cleanup (Emergency)

If disk space is critical before next scheduled cleanup:

```bash
# Stop trading system temporarily
ssh root@46.224.45.196 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml stop'

# Run aggressive cleanup (removes ALL unused data)
ssh root@46.224.45.196 'docker system prune -af --volumes'

# Check space reclaimed
ssh root@46.224.45.196 'df -h /'

# Restart trading system
ssh root@46.224.45.196 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml start'
```

**⚠️ WARNING**: `--volumes` flag will remove database if not mounted correctly! Only use if you understand Docker volume management.

---

## 🔧 Manual Testing

### Test Cleanup Script

```bash
# Test script execution
ssh root@46.224.45.196 '/opt/trader_bitcoin/scripts/maintenance/docker_cleanup.sh'

# Verify cron job is scheduled
ssh root@46.224.45.196 'crontab -l'
```

**Expected Cron Output:**
```
# Docker cleanup - Weekly (Sunday 3:00 AM)
0 3 * * 0 /opt/trader_bitcoin/scripts/maintenance/docker_cleanup.sh
```

---

## 📈 Historical Context

### 2025-11-14: Initial Crisis

**Problem:**
- Disk: 100% (36G/38G used)
- Error: `sqlite3.OperationalError: disk I/O error`
- Impact: Trading system down

**Resolution:**
```bash
docker system prune -af --volumes
# Reclaimed: 30.27GB
# Result: 15% usage (5.2G/38G)
```

**Lesson Learned:** Need automated cleanup to prevent recurrence

---

## 🔄 Maintenance Schedule

| Task | Frequency | Script | Purpose |
|------|-----------|--------|---------|
| **Docker cleanup** | Weekly (Sunday 3AM) | `docker_cleanup.sh` | Prevent disk full |
| **Log rotation** | Daily (system logrotate) | N/A | Prevent log buildup |
| **Disk check** | Manual (as needed) | `df -h /` | Monitor usage |

---

## 📝 Troubleshooting

### Issue: Cleanup Script Not Running

**Diagnosis:**
```bash
# Check cron service status
ssh root@46.224.45.196 'systemctl status cron'

# Check cron logs
ssh root@46.224.45.196 'grep docker_cleanup /var/log/syslog'
```

**Solution:**
```bash
# Restart cron service
ssh root@46.224.45.196 'systemctl restart cron'
```

---

### Issue: Disk Still Full After Cleanup

**Possible Causes:**
1. Database growth (portfolio snapshots, decision history)
2. Application logs not rotated
3. WebSocket cache persistence files

**Investigation:**
```bash
# Find largest directories
ssh root@46.224.45.196 'du -sh /opt/trader_bitcoin/* | sort -h'

# Check database size
ssh root@46.224.45.196 'du -sh /opt/trader_bitcoin/data/data.db'

# Check Docker volumes
ssh root@46.224.45.196 'docker volume ls && docker system df -v'
```

---

## ✅ Success Metrics

**Target:** Maintain disk usage <50% between cleanups

**Current Status:**
- Baseline after manual cleanup: **15%** (5.2G/38G)
- Expected growth per week: ~2-3GB (deployments + build cache)
- Expected after weekly cleanup: 20-30%

**Monitor:** If usage >50% before next cleanup, investigate growth rate

---

## 📚 Related Documentation

- **Deployment Roadmap**: `backend/docs/DEPLOYMENT_ROADMAP.md` (Priority 1: Monitoring & Alerting)
- **Scheduled Jobs**: `backend/docs/operations/SCHEDULED_JOBS.md`
- **System Health**: Check via `/api/readiness` endpoint

---

## 🎯 Future Enhancements (Optional)

1. **Email alerts** when disk >70% after cleanup
2. **Prometheus disk metrics** for real-time monitoring
3. **Database size monitoring** (portfolio_snapshots table growth)
4. **Log aggregation** (ship logs off-server for analysis)

For implementation details, see `backend/docs/DEPLOYMENT_ROADMAP.md`.
