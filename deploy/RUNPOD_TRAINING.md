# RunPod Training Guide — FLAG-Trader

## What this is

Training the FLAG-Trader PPO model (Qwen 0.5B backbone + value/action heads)
on a RunPod RTX 4090 pod with a persistent network volume. Roughly:

- **Duration**: ~10h for 500 PPO updates
- **GPU cost**: ~$5 (community RTX 4090 @ ~$0.50/h)
- **Volume cost**: ~$1.40/month (20 GB persistent network volume)
- **Checkpoints**: every 25 updates — a crash loses at most ~30min of work

## One-time setup

1. Create a RunPod account and top up $10 of credit.
2. Create a network volume:
   - Name: `trading-bot-training`
   - Size: 20 GB
   - Datacenter: `US-IL-1` (has RTX 4090 High availability)
3. Make sure the repo is pushed to GitHub (default branch `main`).

## Per training run

1. Go to https://console.runpod.io/deploy
2. Filter:
   - **Network volume**: `trading-bot-training` (mounts to `/workspace`)
   - **GPU**: RTX 4090
   - **Template**: `runpod/pytorch:2.4.0-py3.11-cuda12.4-devel-ubuntu22.04`
     (or any PyTorch 2.4+ CUDA 12.x image)
3. **Container Start Command**:
   ```bash
   bash -c "curl -fsSL https://raw.githubusercontent.com/fracarlesi/tradercripto/main/deploy/runpod/run_training.sh | bash"
   ```
4. Click **Deploy**.

The pod will:
- Clone the repo to `/workspace/trading_bots`
- `pip install -r requirements.txt`
- Verify GPU (aborts loudly if CUDA missing)
- Run `python -m crypto_bot.scripts.train_runpod`
- Stream logs to `/workspace/training_log_<timestamp>.log`

## Monitor

From the RunPod web terminal:

```bash
tail -f /workspace/training_log_*.log
```

You'll see per-update lines:

```
Update 42/500 | reward: 0.0031 | policy_loss: 0.002 | value_loss: 0.014
```

## Get the trained model

When the training is complete, from the pod web terminal:

```bash
ls -lh /workspace/final_model.pt /workspace/final_model_metadata.json
```

Download options:

- `runpodctl send /workspace/final_model.pt` → gives you a short-lived link
- Or SCP it out (see RunPod docs for the pod's SSH details)

The metadata file contains: model arch, training params, final reward,
updates completed, duration, dataset hash, resume source.

## STOP THE POD

**IMPORTANT** — after downloading:

1. Go to https://console.runpod.io/pods
2. Click **Stop** on the pod (NOT Terminate — Stop keeps the volume alive)
3. Stop ends GPU billing. The network volume keeps costing ~$1.40/month.

If you want the pod to stop automatically on training completion, set
`RUNPOD_AUTO_SHUTDOWN=1` and `RUNPOD_POD_ID=<pod id>` as pod env vars. The
wrapper will `runpodctl stop pod $RUNPOD_POD_ID` when training finishes.

## Resume after crash

If the pod crashes or you stop it mid-run, just redeploy a new pod with the
same Container Start Command. `train_runpod.py` scans
`/workspace/training_run_*/checkpoints/` for the newest `checkpoint_<N>.pt`,
loads its weights, and only runs the remaining updates (`500 - N`).

Note: the resume is **weight-level**. Optimizer state and the PPO update
counter restart from scratch; we only avoid re-learning everything. This is
an intentional simplification — see `crypto_bot/scripts/train_runpod.py`
docstring and the PR description for details.

## Auto-download of candles

If `/workspace/data/candles/*.parquet` is empty, the wrapper invokes
`crypto_bot.scripts.download_candles` for the default basket
(BTC, ETH, SOL, AVAX, FET, INJ, NEAR, RNDR, ARB, OP — 15m, 365 days).
After the first run the volume has the data cached — subsequent runs skip
the download.

## Total cost expected

| Item | Cost |
|------|------|
| GPU (10h × $0.50/h) | ~$5.00 |
| Network volume (monthly) | ~$1.40 |
| Bandwidth (minimal) | ~$0 |
| **Total first month** | **~$6.40** |
