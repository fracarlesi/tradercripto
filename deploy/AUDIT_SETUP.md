# HLQuantBot Autonomous Audit Timer

## What it does

Runs `crypto_bot/scripts/audit_trades.py` inside the `hlquantbot_crypto`
container every 30 minutes (systemd oneshot timer). Loads the last 35
minutes of closed trades from the JSONL outcome logs, infers current bot
state from `docker logs`, applies deterministic anomaly flags (churn,
min-hold violations, streaks, unprotected position loops), and only when
something is actually wrong asks Anthropic Sonnet for a structured verdict.
CRITICAL cases escalate to Opus with a richer log context. Findings become
idempotent GitHub issues (duplicates within 24h become a comment on the
existing issue) and ntfy notifications.

No LLM call happens when the window is clean — expected cost: few cents per
day in calm periods, up to a few euros per day during anomaly storms.

## One-time setup (VPS, as root)

```bash
ssh root@<VPS_IP>
mkdir -p /etc/hlquantbot && chmod 700 /etc/hlquantbot
vi /etc/hlquantbot/audit.env
# paste:
#   ANTHROPIC_API_KEY=sk-ant-...
#   GITHUB_TOKEN=ghp_...
#   GITHUB_REPO=fracarlesi/tradercripto
#   NTFY_TOPIC=...
#   AUDIT_HEARTBEAT=1
chmod 600 /etc/hlquantbot/audit.env
bash /opt/hlquantbot/deploy/install_audit_systemd.sh
```

The installer is idempotent. After the first deploy that includes this
branch, `deploy.sh` will re-run it automatically on every subsequent deploy
(skipped silently if `/etc/hlquantbot/audit.env` is missing).

## Daily operations

```bash
# Follow live audit runs
journalctl -u hlquantbot-audit.service -f

# Next scheduled fire time
systemctl list-timers hlquantbot-audit.timer

# Trigger an audit manually (for testing)
systemctl start hlquantbot-audit.service

# See GitHub issues created by the audit
# https://github.com/fracarlesi/tradercripto/issues?q=is%3Aissue+label%3Abot-anomaly
```

## Cost tracking

Each LLM call appends an entry to `/var/lib/hlquantbot/audit_cost.jsonl`:

```bash
# Total USD spent since install
cat /var/lib/hlquantbot/audit_cost.jsonl | jq -s 'map(.cost_estimate_usd) | add'

# Per-model breakdown
cat /var/lib/hlquantbot/audit_cost.jsonl | jq -r '"\(.model) \(.cost_estimate_usd)"' \
    | awk '{a[$1]+=$2} END {for (m in a) printf "%s %.4f\n", m, a[m]}'
```

## Disable in emergency

```bash
systemctl disable --now hlquantbot-audit.timer
```

## Troubleshooting

- **`ANTHROPIC_API_KEY not set`**: re-check `/etc/hlquantbot/audit.env` and
  `systemctl restart hlquantbot-audit.timer`.
- **`github ... -> 401`**: `GITHUB_TOKEN` is wrong or lacks `repo` scope.
  The audit will still run and send ntfy alerts, issues just get skipped.
- **False positives**: tighten thresholds in `compute_flags()` or raise
  `AUDIT_WINDOW_MINUTES`. Adjust `AUDIT_HEARTBEAT=0` to suppress the
  "audit ok" ntfy ping on clean runs.
- **Runaway costs**: check `/var/lib/hlquantbot/audit_cost.jsonl`. Expected
  steady-state is < EUR 0.10/day. If it spikes, disable the timer first,
  then investigate which anomaly is firing escalations continuously.
- **`docker logs failed`**: the script runs inside the container via
  `docker exec`, so the host's docker CLI must be reachable from within.
  Because the unit uses `docker exec` at the host level (see `ExecStart`),
  this is actually a host-side `docker` call — the container running the
  audit only needs the JSONL files mounted and the Anthropic SDK.
