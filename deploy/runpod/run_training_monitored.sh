#!/bin/bash
# RunPod pod entry point — MONITORED version.
# Pushes heartbeat + watchdog alerts to ntfy.sh so the user knows
# immediately if training stalls, instead of burning $0.60/hr blind.
#
# Required env vars (set in pod "Environment variables"):
#   NTFY_TOPIC       — e.g. hlquantbot-fc2026
#   RUNPOD_API_KEY   — optional, for auto-terminate on completion
#   RUNPOD_POD_ID    — optional, paired with RUNPOD_API_KEY
set -uo pipefail

NTFY_TOPIC="${NTFY_TOPIC:-}"
NTFY_URL="https://ntfy.sh/${NTFY_TOPIC}"
REPO_URL="${REPO_URL:-https://github.com/fracarlesi/tradercripto.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
REPO_DIR="/workspace/trading_bots"
STATUS_FILE="/workspace/training.status"
LOG_FILE="/workspace/training_log_$(date -u +%Y%m%d_%H%M%S).log"

notify() {
  local priority="${1:-default}"
  local title="${2:-RunPod}"
  local body="${3:-}"
  if [ -n "${NTFY_TOPIC}" ]; then
    curl -fsS -X POST "${NTFY_URL}" \
      -H "Title: ${title}" \
      -H "Priority: ${priority}" \
      -H "Tags: runpod,training" \
      -d "${body}" >/dev/null 2>&1 || true
  fi
  echo "[notify:${priority}] ${title} — ${body}"
}

write_status() {
  echo "$(date -u +%s) $*" > "${STATUS_FILE}"
}

# ==================== HEARTBEAT (background) ====================
# Every 5min: GPU util, VRAM, log tail, training.status age.
# Watchdog: if GPU util <5% for 3 consecutive samples (15min) → CRITICAL.
heartbeat_loop() {
  local low_gpu_streak=0
  while true; do
    sleep 300
    if [ ! -f "${STATUS_FILE}" ]; then
      notify high "RunPod STALLED" "No status file after 5min — container wedged?"
      continue
    fi
    local gpu_util mem_used
    gpu_util=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -1)
    mem_used=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader 2>/dev/null | head -1)
    local last_log
    last_log=$(tail -n 2 "${LOG_FILE}" 2>/dev/null | tr '\n' ' ' | cut -c1-200)
    notify default "RunPod HB" "GPU ${gpu_util}% | VRAM ${mem_used} | ${last_log}"
    if [ -n "${gpu_util}" ] && [ "${gpu_util}" -lt 5 ]; then
      low_gpu_streak=$((low_gpu_streak + 1))
      if [ "${low_gpu_streak}" -ge 3 ]; then
        notify urgent "RunPod STALL" "GPU <5% for 15min — training frozen. Check pod."
        low_gpu_streak=0
      fi
    else
      low_gpu_streak=0
    fi
  done
}

# ==================== MAIN ====================
trap 'notify urgent "RunPod CRASHED" "Script died unexpectedly at $(date -u)"' ERR
write_status "bootstrap"
notify default "RunPod START" "Pod bootstrap at $(date -u)"

cd /workspace
if [ ! -d "${REPO_DIR}/.git" ]; then
  git clone --branch "${REPO_BRANCH}" "${REPO_URL}" "${REPO_DIR}"
else
  cd "${REPO_DIR}"
  git fetch origin "${REPO_BRANCH}"
  git checkout "${REPO_BRANCH}"
  git pull --ff-only origin "${REPO_BRANCH}"
fi
write_status "deps"

cd "${REPO_DIR}"
if command -v uv >/dev/null 2>&1; then
  uv pip install --system -r requirements.txt
else
  pip install --no-cache-dir -r requirements.txt
fi
write_status "gpu_check"

python - <<'PY' || { curl -fsS -X POST "https://ntfy.sh/${NTFY_TOPIC}" -H "Priority: urgent" -H "Title: RunPod NO GPU" -d "CUDA not available, aborting" >/dev/null 2>&1; exit 1; }
import torch
assert torch.cuda.is_available(), "NO CUDA"
dev = torch.cuda.get_device_properties(0)
print(f"GPU: {torch.cuda.get_device_name(0)}, VRAM: {dev.total_memory / 1e9:.1f} GB")
PY

notify default "RunPod TRAINING" "Deps installed, GPU verified, starting training"
write_status "training"
heartbeat_loop &
HB_PID=$!

set +e
python -m crypto_bot.scripts.train_runpod 2>&1 | tee "${LOG_FILE}"
STATUS=${PIPESTATUS[0]}
set -e
kill "${HB_PID}" 2>/dev/null || true

if [ "${STATUS}" -eq 0 ]; then
  FINAL_MODEL=$(find /workspace -name "final_model.pt" -mmin -60 2>/dev/null | head -1)
  if [ -n "${FINAL_MODEL}" ]; then
    SIZE=$(du -h "${FINAL_MODEL}" | cut -f1)
    notify high "RunPod DONE ✅" "final_model.pt created (${SIZE}) at ${FINAL_MODEL}"
    write_status "done_success"
  else
    notify urgent "RunPod DONE no model" "Exit 0 but no final_model.pt found — investigate"
    write_status "done_nomodel"
  fi
else
  notify urgent "RunPod FAILED" "Exit code ${STATUS} — check logs"
  write_status "failed ${STATUS}"
fi

# Auto-terminate to stop billing (if API key provided)
if [ -n "${RUNPOD_API_KEY:-}" ] && [ -n "${RUNPOD_POD_ID:-}" ]; then
  notify default "RunPod AUTO-TERMINATE" "Stopping pod ${RUNPOD_POD_ID} to halt billing"
  curl -fsS -X POST "https://api.runpod.io/graphql?api_key=${RUNPOD_API_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"query\":\"mutation { podTerminate(input: {podId: \\\"${RUNPOD_POD_ID}\\\"}) }\"}" \
    >/dev/null 2>&1 || notify urgent "RunPod AUTO-TERMINATE FAILED" "Manual terminate needed"
fi

exit "${STATUS}"
