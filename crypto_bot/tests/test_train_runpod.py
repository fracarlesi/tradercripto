"""Tests for crypto_bot.scripts.train_runpod.

Heavy mocks: we never import torch-backed symbols. The trainer,
model, environment, and data loader are patched so these tests
exercise only the orchestration code in train_runpod.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from crypto_bot.scripts import train_runpod


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("RUNPOD_AUTO_SHUTDOWN", raising=False)
    monkeypatch.delenv("RUNPOD_POD_ID", raising=False)


def _make_args(**overrides: object) -> argparse.Namespace:
    base = {
        "updates": 100,
        "steps": 10,
        "lr": 1e-5,
        "save_every": 25,
        "eval_every": 25,
        "model_name": "fake-model",
        "device": "cpu",
        "symbols": ["BTC"],
        "interval": "15m",
        "days": 30,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_resolves_workspace_path_with_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "ws"
    fake.mkdir()
    monkeypatch.setenv("WORKSPACE_DIR", str(fake))
    assert train_runpod.resolve_workspace_dir() == fake


def test_falls_back_to_local_when_no_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("WORKSPACE_DIR", raising=False)
    # Force /workspace to look absent by monkeypatching Path.exists on that path.
    real_exists = Path.exists

    def fake_exists(self: Path) -> bool:
        if str(self) == "/workspace":
            return False
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.chdir(tmp_path)
    result = train_runpod.resolve_workspace_dir()
    assert result.name == "runs"
    assert result.is_absolute()


def test_resume_from_existing_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))

    # Lay down a fake prior-run checkpoint
    prior = tmp_path / "training_run_20260101_000000" / "checkpoints"
    prior.mkdir(parents=True)
    (prior / "checkpoint_25.pt").write_bytes(b"fake")
    (prior / "checkpoint_50.pt").write_bytes(b"fake")
    (tmp_path / "data" / "candles").mkdir(parents=True)
    (tmp_path / "data" / "candles" / "BTC.parquet").write_bytes(b"x")

    latest, step = train_runpod.find_latest_checkpoint(tmp_path)
    assert latest is not None and latest.name == "checkpoint_50.pt"
    assert step == 50

    # Fake model / env / trainer / data loader
    fake_model = MagicMock()
    fake_trainer = MagicMock()
    fake_trainer.train.return_value = [
        {"mean_reward": 0.1, "policy_loss": 0.0, "value_loss": 0.0}
    ] * 50
    fake_candles = np.zeros((200, 5), dtype=float)

    with (
        patch("crypto_bot.flag_trader.model.FlagTraderModel", return_value=fake_model),
        patch("crypto_bot.flag_trader.prompt.PromptBuilder"),
        patch(
            "crypto_bot.flag_trader.environment.HyperliquidTradingEnv",
            return_value=MagicMock(),
        ),
        patch(
            "crypto_bot.flag_trader.trainer.PPOTrainer",
            return_value=fake_trainer,
        ),
        patch(
            "crypto_bot.scripts.train_flag_trader.load_training_data",
            return_value=(fake_candles, fake_candles),
        ),
        patch.object(train_runpod, "ensure_candles", return_value=tmp_path / "data" / "candles"),
    ):
        args = _make_args(updates=100)
        completed = train_runpod.run_training(args)

    # prior 50 + 50 fake rollout stats => 100 total
    assert completed == 100
    # Resume path: load_trainable called once with the checkpoint
    fake_model.load_trainable.assert_called_once()
    ckpt_arg = fake_model.load_trainable.call_args.args[0]
    assert os.path.basename(str(ckpt_arg)) == "checkpoint_50.pt"
    # Trainer asked for remaining (100 - 50) = 50 updates
    kwargs = fake_trainer.train.call_args.kwargs
    assert kwargs["total_updates"] == 50
    assert kwargs["save_every"] == 25
    # Final model + metadata written to workspace root
    assert (tmp_path / "final_model_metadata.json").exists()
