#!/bin/bash
# Idempotent installer for the HLQuantBot audit systemd timer.
# Run as root on the VPS. Safe to re-run (copies units, reloads, enables).

set -euo pipefail

ENV_FILE="/etc/hlquantbot/audit.env"
UNIT_SRC_DIR="$(cd "$(dirname "$0")/systemd" && pwd)"
UNIT_DST_DIR="/etc/systemd/system"

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must run as root" >&2
    exit 1
fi

# 1) Ensure env file exists (create template on first run, then exit)
if [ ! -f "$ENV_FILE" ]; then
    mkdir -p /etc/hlquantbot
    chmod 700 /etc/hlquantbot
    cat > "$ENV_FILE" <<'EOF'
# HLQuantBot audit timer env — fill in and re-run install_audit_systemd.sh
# ANTHROPIC_API_KEY=sk-ant-...
# GITHUB_TOKEN=ghp_...
# GITHUB_REPO=fracarlesi/tradercripto
# NTFY_TOPIC=...
# AUDIT_HEARTBEAT=1
# AUDIT_WINDOW_MINUTES=35
EOF
    chmod 600 "$ENV_FILE"
    echo "Created template $ENV_FILE — edit it and re-run this script."
    exit 1
fi

# 2) Validate required vars
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "ERROR: ANTHROPIC_API_KEY not set in $ENV_FILE" >&2
    exit 1
fi
if [ -z "${GITHUB_TOKEN:-}" ]; then
    echo "WARN: GITHUB_TOKEN not set — issues will be skipped, only ntfy alerts"
fi
if [ -z "${GITHUB_REPO:-}" ]; then
    echo "WARN: GITHUB_REPO not set — defaulting to fracarlesi/tradercripto"
fi

# 3) Create a minimal host venv just for the audit script (stdlib + anthropic).
#    Kept separate from the container image so we don't need to shell into it.
VENV_DIR="/opt/hlquantbot/.audit-venv"
if [ ! -x "$VENV_DIR/bin/python" ]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet "anthropic>=0.40.0"

# 3b) Install unit files
install -m 0644 "$UNIT_SRC_DIR/hlquantbot-audit.service" "$UNIT_DST_DIR/hlquantbot-audit.service"
install -m 0644 "$UNIT_SRC_DIR/hlquantbot-audit.timer"   "$UNIT_DST_DIR/hlquantbot-audit.timer"

# 4) Cost log dir
mkdir -p /var/lib/hlquantbot
chmod 755 /var/lib/hlquantbot

# 5) Reload + enable + start timer
systemctl daemon-reload
systemctl enable --now hlquantbot-audit.timer

echo ""
echo "=== Audit timer installed ==="
systemctl status hlquantbot-audit.timer --no-pager || true
echo ""
echo "Useful commands:"
echo "  journalctl -u hlquantbot-audit.service -f     # follow audit runs"
echo "  systemctl list-timers hlquantbot-audit.timer  # next fire time"
echo "  systemctl start hlquantbot-audit.service      # trigger run now"
echo "  systemctl disable --now hlquantbot-audit.timer  # disable"
