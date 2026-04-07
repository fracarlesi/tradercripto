"""RunPod training wrapper for FLAG-Trader.

Thin orchestrator on top of the existing PPOTrainer that:
  - Writes all outputs to /workspace/training_run_<ts>/ (fallback ./runs/...)
  - Auto-resumes from the most recent checkpoint_*.pt in any prior run dir
  - Auto-downloads candles if /workspace/data/candles is empty
  - Uses save_every=25 by default (CRITICAL: never lose >25 updates)
  - Saves final_model.pt + final_model_metadata.json to workspace root
  - Optional self-shutdown via RUNPOD_AUTO_SHUTDOWN=1
  - Fails loud: writes training_failed.txt on any exception

Does NOT modify crypto_bot/flag_trader/trainer.py. Resume is weight-level:
we call FlagTraderModel.load_trainable(ckpt) before trainer.train().
The trainer always starts its update counter from 1, so the effective
remaining updates are (total_updates - prior_updates) — we pass this via
--updates from the env and track it in the metadata file.

Usage:
    python -m crypto_bot.scripts.train_runpod
    python -m crypto_bot.scripts.train_runpod --updates 500 --save-every 25
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("train_runpod")

DEFAULT_SYMBOLS = [
    "BTC",
    "ETH",
    "SOL",
    "AVAX",
    "FET",
    "INJ",
    "NEAR",
    "RNDR",
    "ARB",
    "OP",
]
DEFAULT_INTERVAL = "15m"
DEFAULT_DAYS = 365
DEFAULT_UPDATES = 500
DEFAULT_STEPS = 50
DEFAULT_SAVE_EVERY = 25
DEFAULT_EVAL_EVERY = 25
DEFAULT_MODEL_NAME = "Qwen/Qwen2.5-0.5B-Instruct"


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )


def resolve_workspace_dir() -> Path:
    """Return the persistent workspace directory.

    Priority:
      1. env WORKSPACE_DIR (explicit override, used in tests)
      2. /workspace if it exists (RunPod network volume)
      3. ./runs (local fallback)
    """
    override = os.environ.get("WORKSPACE_DIR")
    if override:
        return Path(override)
    runpod_default = Path("/workspace")
    if runpod_default.exists() and runpod_default.is_dir():
        return runpod_default
    return Path("./runs").resolve()


def find_latest_checkpoint(workspace: Path) -> tuple[Path | None, int]:
    """Scan workspace for the most recent checkpoint_<N>.pt across all run dirs.

    Returns (checkpoint_path, completed_updates) or (None, 0) if nothing found.
    """
    if not workspace.exists():
        return None, 0
    latest_path: Path | None = None
    latest_step = 0
    latest_mtime = 0.0
    pattern = re.compile(r"checkpoint_(\d+)\.pt$")
    for run_dir in workspace.glob("training_run_*"):
        ckpt_dir = run_dir / "checkpoints"
        if not ckpt_dir.exists():
            continue
        for ckpt in ckpt_dir.glob("checkpoint_*.pt"):
            match = pattern.search(ckpt.name)
            if not match:
                continue
            step = int(match.group(1))
            mtime = ckpt.stat().st_mtime
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest_step = step
                latest_path = ckpt
    return latest_path, latest_step


def ensure_candles(
    workspace: Path,
    symbols: list[str],
    interval: str,
    days: int,
) -> Path:
    """Ensure candle parquet files exist in <workspace>/data/candles.

    If none found, invokes the download_candles script via subprocess.
    Returns the data directory path.
    """
    data_dir = workspace / "data" / "candles"
    data_dir.mkdir(parents=True, exist_ok=True)
    existing = list(data_dir.glob("*.parquet"))
    if existing:
        logger.info("Found %d existing candle files in %s", len(existing), data_dir)
        return data_dir

    logger.info(
        "No candles found in %s — downloading (symbols=%s, interval=%s, days=%d)",
        data_dir,
        symbols,
        interval,
        days,
    )
    cmd = [
        sys.executable,
        "-m",
        "crypto_bot.scripts.download_candles",
        "--days",
        str(days),
        "--interval",
        interval,
        "--data-dir",
        str(data_dir),
        "--symbols",
        *symbols,
    ]
    logger.info("Running: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)
    return data_dir


def hash_dataset(data_dir: Path) -> str:
    """Stable hash of candle files (names + sizes + mtimes)."""
    h = hashlib.sha256()
    for p in sorted(data_dir.glob("*.parquet")):
        st = p.stat()
        h.update(f"{p.name}:{st.st_size}:{int(st.st_mtime)}".encode())
    return h.hexdigest()[:16]


def _make_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def build_run_dir(workspace: Path, timestamp: str | None = None) -> Path:
    ts = timestamp or _make_timestamp()
    run_dir = workspace / f"training_run_{ts}"
    (run_dir / "checkpoints").mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    return run_dir


def write_metadata(
    path: Path,
    *,
    model_name: str,
    updates_requested: int,
    updates_completed: int,
    steps_per_rollout: int,
    final_reward: float | None,
    duration_sec: float,
    dataset_hash: str,
    resumed_from: str | None,
) -> None:
    payload: dict[str, Any] = {
        "model_name": model_name,
        "arch": "FlagTrader-PPO",
        "updates_requested": updates_requested,
        "updates_completed": updates_completed,
        "steps_per_rollout": steps_per_rollout,
        "final_reward": final_reward,
        "training_duration_sec": duration_sec,
        "dataset_hash": dataset_hash,
        "resumed_from": resumed_from,
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2))


def maybe_shutdown_pod() -> None:
    if os.environ.get("RUNPOD_AUTO_SHUTDOWN") != "1":
        return
    pod_id = os.environ.get("RUNPOD_POD_ID")
    if not pod_id:
        logger.warning("RUNPOD_AUTO_SHUTDOWN=1 but RUNPOD_POD_ID not set — skipping shutdown")
        return
    logger.info("Auto-shutdown requested — stopping pod %s", pod_id)
    try:
        subprocess.run(["runpodctl", "stop", "pod", pod_id], check=False)
    except FileNotFoundError:
        logger.warning("runpodctl not available — cannot auto-shutdown")


def run_training(args: argparse.Namespace) -> int:
    """Core orchestration. Returns number of updates completed by trainer."""
    # Imports deferred so tests can mock without loading torch.
    from crypto_bot.flag_trader.environment import HyperliquidTradingEnv
    from crypto_bot.flag_trader.model import FlagTraderModel
    from crypto_bot.flag_trader.prompt import PromptBuilder
    from crypto_bot.flag_trader.trainer import PPOTrainer
    from crypto_bot.scripts.train_flag_trader import load_training_data

    workspace = resolve_workspace_dir()
    workspace.mkdir(parents=True, exist_ok=True)
    logger.info("Workspace: %s", workspace)

    # 1. Data
    data_dir = ensure_candles(
        workspace, args.symbols, args.interval, args.days
    )
    dataset_hash = hash_dataset(data_dir)
    logger.info("Dataset hash: %s", dataset_hash)

    train_candles, eval_candles = load_training_data(data_dir)
    logger.info(
        "Data loaded: train=%d candles, eval=%d candles",
        len(train_candles),
        len(eval_candles),
    )

    # 2. Resume?
    resume_ckpt, prior_updates = find_latest_checkpoint(workspace)
    if resume_ckpt is not None:
        logger.info(
            "Found prior checkpoint %s (step=%d) — will resume",
            resume_ckpt,
            prior_updates,
        )
    remaining = max(args.updates - prior_updates, 0)
    if remaining == 0:
        logger.info(
            "All %d updates already completed in prior run — nothing to do",
            args.updates,
        )
        return prior_updates

    # 3. Run dir (new per invocation, but checkpoints accumulate across dirs)
    run_dir = build_run_dir(workspace)
    logger.info("Run dir: %s (remaining updates: %d)", run_dir, remaining)

    # 4. Build model + trainer
    logger.info("Loading model: %s", args.model_name)
    model = FlagTraderModel(model_name=args.model_name, device=args.device)
    if resume_ckpt is not None:
        logger.info("Loading weights from %s", resume_ckpt)
        model.load_trainable(resume_ckpt)

    prompt_builder = PromptBuilder()
    train_env = HyperliquidTradingEnv(candles=train_candles)
    eval_env = HyperliquidTradingEnv(candles=eval_candles)
    trainer = PPOTrainer(model=model, prompt_builder=prompt_builder, lr=args.lr)

    # 5. Train
    t0 = time.monotonic()
    logger.info(
        "Starting PPO training | updates=%d | steps=%d | save_every=%d | eval_every=%d",
        remaining,
        args.steps,
        args.save_every,
        args.eval_every,
    )
    stats = trainer.train(
        env=train_env,
        total_updates=remaining,
        steps_per_rollout=args.steps,
        eval_env=eval_env,
        eval_every=args.eval_every,
        save_dir=run_dir / "checkpoints",
        save_every=args.save_every,
    )
    duration = time.monotonic() - t0
    logger.info("Training finished in %.1fs (%d updates)", duration, len(stats))

    final_reward = float(stats[-1].get("mean_reward", 0.0)) if stats else None
    total_completed = prior_updates + len(stats)

    # 6. Save final model + metadata to workspace root (what the user downloads)
    final_model_path = workspace / "final_model.pt"
    model.save_trainable(final_model_path)
    logger.info("Final model saved: %s", final_model_path)
    write_metadata(
        workspace / "final_model_metadata.json",
        model_name=args.model_name,
        updates_requested=args.updates,
        updates_completed=total_completed,
        steps_per_rollout=args.steps,
        final_reward=final_reward,
        duration_sec=duration,
        dataset_hash=dataset_hash,
        resumed_from=str(resume_ckpt) if resume_ckpt else None,
    )
    return total_completed


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="RunPod-ready FLAG-Trader training wrapper")
    p.add_argument("--updates", type=int, default=DEFAULT_UPDATES)
    p.add_argument("--steps", type=int, default=DEFAULT_STEPS)
    p.add_argument("--lr", type=float, default=1e-5)
    p.add_argument("--save-every", type=int, default=DEFAULT_SAVE_EVERY)
    p.add_argument("--eval-every", type=int, default=DEFAULT_EVAL_EVERY)
    p.add_argument("--model-name", type=str, default=DEFAULT_MODEL_NAME)
    p.add_argument("--device", type=str, default="auto", choices=["auto", "cpu", "cuda", "mps"])
    p.add_argument("--symbols", nargs="*", default=DEFAULT_SYMBOLS)
    p.add_argument("--interval", type=str, default=DEFAULT_INTERVAL)
    p.add_argument("--days", type=int, default=DEFAULT_DAYS)
    return p


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    workspace = resolve_workspace_dir()
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        run_training(args)
    except Exception as exc:  # noqa: BLE001 — top-level guard, fail loud
        tb = traceback.format_exc()
        logger.error("TRAINING FAILED: %s\n%s", exc, tb)
        fail_path = workspace / "training_failed.txt"
        try:
            fail_path.write_text(
                f"timestamp={datetime.now(timezone.utc).isoformat()}\n"
                f"reason={exc}\n\n{tb}"
            )
        except OSError:
            pass
        return 1
    finally:
        maybe_shutdown_pod()
    logger.info("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
