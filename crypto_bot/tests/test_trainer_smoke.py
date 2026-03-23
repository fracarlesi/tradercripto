"""Smoke tests for PPO Trainer — no model download required."""

from unittest.mock import MagicMock

import numpy as np
import pytest
import torch

from flag_trader.environment import HyperliquidTradingEnv
from flag_trader.prompt import PromptBuilder
from flag_trader.trainer import PPOTrainer, RolloutBuffer, obs_to_prompt_inputs


# --- RolloutBuffer tests ---

def test_rollout_buffer_add_clear_len():
    buf = RolloutBuffer()
    assert len(buf) == 0

    buf.add("prompt1", 1, 0.5, 0.3, torch.tensor([-1.0]), False)
    buf.add("prompt2", 2, -0.1, 0.2, torch.tensor([-0.5]), True)
    assert len(buf) == 2
    assert buf.actions == [1, 2]

    buf.clear()
    assert len(buf) == 0
    assert buf.states == []


# --- GAE tests ---

def _make_trainer_with_mock_model() -> PPOTrainer:
    model = MagicMock()
    model.get_trainable_params.return_value = [torch.nn.Parameter(torch.zeros(2))]
    model.device = torch.device("cpu")
    prompt_builder = PromptBuilder()
    return PPOTrainer(model=model, prompt_builder=prompt_builder)


def test_compute_gae_known_values():
    trainer = _make_trainer_with_mock_model()

    rewards = [1.0, 1.0, 1.0]
    values = [0.5, 0.5, 0.5]
    dones = [False, False, True]

    advantages, returns = trainer.compute_gae(rewards, values, dones, last_value=0.0)

    assert advantages.shape == (3,)
    assert returns.shape == (3,)
    # Last step: done=True, so delta = 1.0 + 0 - 0.5 = 0.5, advantage = 0.5
    assert abs(advantages[2].item() - 0.5) < 1e-5
    # returns = advantages + values
    assert abs(returns[2].item() - 1.0) < 1e-5


def test_compute_gae_all_done():
    trainer = _make_trainer_with_mock_model()

    rewards = [1.0, 2.0]
    values = [0.0, 0.0]
    dones = [True, True]

    advantages, returns = trainer.compute_gae(rewards, values, dones)
    # Each step is independent (done=True), advantage = reward - value
    assert abs(advantages[0].item() - 1.0) < 1e-5
    assert abs(advantages[1].item() - 2.0) < 1e-5


# --- obs_to_prompt_inputs conversion test ---

def test_obs_to_prompt_inputs():
    obs = {
        "candles": np.ones((20, 5), dtype=np.float32),
        "portfolio": np.array([0.5, 0.3, 0.1, 0.9], dtype=np.float32),
        "history": np.zeros(10, dtype=np.float32),
    }
    candles, portfolio, history = obs_to_prompt_inputs(obs)

    assert len(candles) == 20
    assert candles[0]["open"] == 1.0
    assert portfolio["cash_balance"] == pytest.approx(0.5)
    assert portfolio["total_account_value"] == pytest.approx(0.9)
    assert len(history["recent_rewards"]) == 10


# --- Training loop smoke test (mock model, no download) ---

def test_training_loop_no_crash():
    """1 update with 5 steps using a mock model should not crash."""
    candles = np.random.rand(50, 5).astype(np.float32) * 100 + 1
    env = HyperliquidTradingEnv(candles=candles, window_size=5)

    model = MagicMock()
    model.device = torch.device("cpu")
    model.get_trainable_params.return_value = [torch.nn.Parameter(torch.randn(4, 3))]

    # get_action returns (action, value, log_prob, tp_pct, sl_pct, input_ids, attention_mask)
    model.get_action.return_value = (
        1, 0.1, torch.tensor(-1.0), 2.5, 1.0,
        torch.ones(1, 10, dtype=torch.long),
        torch.ones(1, 10, dtype=torch.long),
    )

    # evaluate_actions returns (log_probs, values, entropy, tp_pct, sl_pct)
    model.evaluate_actions.return_value = (
        torch.tensor([-1.0], requires_grad=True),
        torch.tensor([0.1], requires_grad=True),
        torch.tensor([0.5], requires_grad=True),
        torch.tensor([2.5], requires_grad=True),
        torch.tensor([1.0], requires_grad=True),
    )

    # Mock tokenizer
    model.tokenizer.return_value = {
        "input_ids": torch.ones(1, 10, dtype=torch.long),
        "attention_mask": torch.ones(1, 10, dtype=torch.long),
    }

    trainer = PPOTrainer(model=model, prompt_builder=PromptBuilder(candle_window=5), ppo_epochs=1)

    rollout_stats = trainer.collect_rollout(env, num_steps=5)
    assert rollout_stats["mean_reward"] is not None
    assert len(trainer.buffer) == 5

    update_stats = trainer.update()
    assert "policy_loss" in update_stats
    assert "value_loss" in update_stats
    assert len(trainer.buffer) == 0  # cleared after update
