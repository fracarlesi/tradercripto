#!/bin/bash
# RunPod pod entry point: clone repo, install deps, verify GPU, run training.
# Used as the "Container Start Command" on a RunPod pod with a persistent
# network volume mounted at /workspace.
set -euo pipefail

echo "=== RunPod training run started: $(date -u) ==="

REPO_URL="${REPO_URL:-https://github.com/fracarlesi/tradercripto.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
REPO_DIR="/workspace/trading_bots"

# 1. Clone or update repo (idempotent)
cd /workspace
if [ ! -d "${REPO_DIR}/.git" ]; then
  echo "Cloning ${REPO_URL} (branch=${REPO_BRANCH}) -> ${REPO_DIR}"
  git clone --branch "${REPO_BRANCH}" "${REPO_URL}" "${REPO_DIR}"
else
  echo "Updating existing repo at ${REPO_DIR}"
  cd "${REPO_DIR}"
  git fetch origin "${REPO_BRANCH}"
  git checkout "${REPO_BRANCH}"
  git pull --ff-only origin "${REPO_BRANCH}"
fi

# 2. Install dependencies (prefer uv if available, fallback pip)
cd "${REPO_DIR}"
if command -v uv >/dev/null 2>&1; then
  echo "Installing deps via uv"
  uv pip install --system -r requirements.txt
else
  echo "Installing deps via pip"
  pip install --no-cache-dir -r requirements.txt
fi

# 3. Verify GPU
python - <<'PY'
import torch
assert torch.cuda.is_available(), "NO CUDA — aborting"
dev = torch.cuda.get_device_properties(0)
print(f"GPU: {torch.cuda.get_device_name(0)}, VRAM: {dev.total_memory / 1e9:.1f} GB")
PY

# 4. Run training with stdout tee'd to persistent log
LOG_FILE="/workspace/training_log_$(date -u +%Y%m%d_%H%M%S).log"
echo "Training log: ${LOG_FILE}"
cd "${REPO_DIR}"
set +e
python -m crypto_bot.scripts.train_runpod 2>&1 | tee "${LOG_FILE}"
STATUS=${PIPESTATUS[0]}
set -e

echo "=== Training finished: $(date -u) exit=${STATUS} ==="
exit "${STATUS}"
