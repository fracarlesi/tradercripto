"""
PPO Trainer for FLAG-Trader
============================

Proximal Policy Optimization training loop that connects:
- HyperliquidTradingEnv (Gymnasium env)
- FlagTraderModel (SmolLM2 + policy/value heads)
- PromptBuilder (observation -> structured text prompt)

Based on FLAG-Trader paper Algorithm 1.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Optional

import numpy as np
import torch
import torch.nn as nn

from .environment import HyperliquidTradingEnv
from .model import FlagTraderModel
from .prompt import PromptBuilder

logger = logging.getLogger(__name__)


def obs_to_prompt_inputs(
    obs: dict[str, np.ndarray],
) -> tuple[list[dict[str, float]], dict[str, float], dict[str, list]]:
    """Convert environment observation (numpy arrays) to PromptBuilder format.

    Args:
        obs: Dict with 'candles' (N,5), 'portfolio' (4,), 'history' (10,).

    Returns:
        (candles_list, portfolio_dict, history_dict) ready for PromptBuilder.build_prompt().
    """
    candles_arr = obs["candles"]  # (window_size, 5) -- O, H, L, C, V
    candles_list = [
        {
            "open": float(row[0]),
            "high": float(row[1]),
            "low": float(row[2]),
            "close": float(row[3]),
            "volume": float(row[4]),
        }
        for row in candles_arr
    ]

    portfolio_arr = obs["portfolio"]  # (4,) -- cash, position_value, unrealized_pnl, total_value
    portfolio_dict = {
        "cash_balance": float(portfolio_arr[0]),
        "asset_position": float(portfolio_arr[1]),
        "total_account_value": float(portfolio_arr[3]),
    }

    history_arr = obs["history"]  # (10,) -- recent rewards
    history_dict: dict[str, list] = {
        "recent_rewards": [float(x) for x in history_arr],
        "net_values": [],
        "actions": [],
    }

    return candles_list, portfolio_dict, history_dict


class RolloutBuffer:
    """Stores transitions collected during rollout for PPO update."""

    def __init__(self) -> None:
        self.states: list[str] = []  # prompt strings
        self.actions: list[int] = []  # action indices
        self.rewards: list[float] = []  # step rewards
        self.values: list[float] = []  # V(s) estimates
        self.log_probs: list[torch.Tensor] = []  # log pi(a|s)
        self.dones: list[bool] = []  # episode done flags
        # Cached tokenized inputs (avoid re-tokenizing in update)
        self.input_ids: list[torch.Tensor] = []
        self.attention_masks: list[torch.Tensor] = []

    def add(
        self,
        state: str,
        action: int,
        reward: float,
        value: float,
        log_prob: torch.Tensor,
        done: bool,
        input_ids: torch.Tensor | None = None,
        attention_mask: torch.Tensor | None = None,
    ) -> None:
        self.states.append(state)
        self.actions.append(action)
        self.rewards.append(reward)
        self.values.append(value)
        self.log_probs.append(log_prob)
        self.dones.append(done)
        if input_ids is not None:
            self.input_ids.append(input_ids.cpu())
        if attention_mask is not None:
            self.attention_masks.append(attention_mask.cpu())

    def clear(self) -> None:
        self.states.clear()
        self.actions.clear()
        self.rewards.clear()
        self.values.clear()
        self.log_probs.clear()
        self.dones.clear()
        self.input_ids.clear()
        self.attention_masks.clear()

    def __len__(self) -> int:
        return len(self.states)


class PPOTrainer:
    """PPO training loop for FlagTraderModel.

    Args:
        model: FlagTraderModel with policy and value heads.
        prompt_builder: Converts observations to structured text prompts.
        lr: Learning rate for AdamW optimizer.
        gamma: Discount factor.
        gae_lambda: GAE lambda for advantage estimation.
        clip_range: PPO clipping epsilon.
        ppo_epochs: Number of optimization epochs per update.
        value_loss_coef: Weight for value loss in total loss.
        entropy_coef: Weight for entropy bonus in total loss.
        max_grad_norm: Maximum gradient norm for clipping.
    """

    def __init__(
        self,
        model: FlagTraderModel,
        prompt_builder: PromptBuilder,
        lr: float = 1e-5,
        gamma: float = 0.99,
        gae_lambda: float = 0.95,
        clip_range: float = 0.2,
        ppo_epochs: int = 4,
        value_loss_coef: float = 0.5,
        entropy_coef: float = 0.01,
        max_grad_norm: float = 0.5,
    ) -> None:
        self.model = model
        self.prompt_builder = prompt_builder
        self.gamma = gamma
        self.gae_lambda = gae_lambda
        self.clip_range = clip_range
        self.ppo_epochs = ppo_epochs
        self.value_loss_coef = value_loss_coef
        self.entropy_coef = entropy_coef
        self.max_grad_norm = max_grad_norm

        self.optimizer = torch.optim.AdamW(model.get_trainable_params(), lr=lr)
        self.buffer = RolloutBuffer()

    def _obs_to_prompt(self, obs: dict[str, np.ndarray]) -> str:
        """Convert env observation to prompt string."""
        candles, portfolio, history = obs_to_prompt_inputs(obs)
        return self.prompt_builder.build_prompt(candles, portfolio, history)

    @torch.no_grad()
    def collect_rollout(
        self, env: HyperliquidTradingEnv, num_steps: int = 100
    ) -> dict[str, float]:
        """Collect transitions from environment into the rollout buffer.

        Args:
            env: Trading environment instance.
            num_steps: Number of steps to collect.

        Returns:
            Stats dict with mean_reward, num_episodes_completed, mean_episode_return.
        """
        self.model.eval()
        self.buffer.clear()

        obs, _ = env.reset()
        episode_returns: list[float] = []
        current_episode_return = 0.0
        all_rewards: list[float] = []

        for _ in range(num_steps):
            prompt = self._obs_to_prompt(obs)
            result = self.model.get_action(prompt, return_tokens=True)
            action, value, log_prob, input_ids, attention_mask = result

            next_obs, reward, terminated, truncated, info = env.step(action)
            done = terminated or truncated

            self.buffer.add(
                prompt, action, reward, value, log_prob, done,
                input_ids=input_ids, attention_mask=attention_mask,
            )
            current_episode_return += reward
            all_rewards.append(reward)

            if done:
                episode_returns.append(current_episode_return)
                current_episode_return = 0.0
                obs, _ = env.reset()
            else:
                obs = next_obs

        return {
            "mean_reward": float(np.mean(all_rewards)) if all_rewards else 0.0,
            "num_episodes_completed": len(episode_returns),
            "mean_episode_return": float(np.mean(episode_returns)) if episode_returns else 0.0,
        }

    def compute_gae(
        self,
        rewards: list[float],
        values: list[float],
        dones: list[bool],
        last_value: float = 0.0,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """Generalized Advantage Estimation.

        Args:
            rewards: Per-step rewards from rollout.
            values: Per-step value estimates V(s).
            dones: Per-step done flags.
            last_value: Bootstrap value for the last state (0 if terminal).

        Returns:
            (advantages, returns) as tensors of shape (num_steps,).
        """
        n = len(rewards)
        advantages = torch.zeros(n)
        last_gae = 0.0

        for t in reversed(range(n)):
            next_value = last_value if t == n - 1 else values[t + 1]
            next_non_terminal = 0.0 if dones[t] else 1.0

            delta = rewards[t] + self.gamma * next_value * next_non_terminal - values[t]
            last_gae = delta + self.gamma * self.gae_lambda * next_non_terminal * last_gae
            advantages[t] = last_gae

        returns = advantages + torch.tensor(values, dtype=torch.float32)
        return advantages, returns

    def _pad_and_batch_tokens(self) -> tuple[torch.Tensor, torch.Tensor]:
        """Pad cached token tensors and stack into batches.

        If tokens were cached during collect_rollout, use those.
        Otherwise, batch-tokenize all prompts at once.

        Returns:
            (input_ids, attention_mask) both of shape (N, max_seq_len).
        """
        if self.buffer.input_ids:
            # Use cached tokens - pad to same length
            max_len = max(t.shape[1] for t in self.buffer.input_ids)
            pad_id = self.model.tokenizer.pad_token_id or 0

            padded_ids = []
            padded_masks = []
            for ids, mask in zip(self.buffer.input_ids, self.buffer.attention_masks):
                seq_len = ids.shape[1]
                if seq_len < max_len:
                    pad_len = max_len - seq_len
                    ids = torch.cat([ids, torch.full((1, pad_len), pad_id, dtype=ids.dtype)], dim=1)
                    mask = torch.cat([mask, torch.zeros(1, pad_len, dtype=mask.dtype)], dim=1)
                padded_ids.append(ids)
                padded_masks.append(mask)

            return torch.cat(padded_ids, dim=0), torch.cat(padded_masks, dim=0)
        else:
            # Fallback: batch tokenize all prompts
            tokens = self.model.tokenizer(
                self.buffer.states,
                return_tensors="pt",
                max_length=512,
                truncation=True,
                padding=True,
            )
            return tokens["input_ids"], tokens["attention_mask"]

    def update(self, mini_batch_size: int = 16) -> dict[str, float]:
        """PPO policy gradient update using collected rollout buffer.

        Processes samples in mini-batches for GPU efficiency.
        Uses AMP (mixed precision) on CUDA for faster forward/backward.

        Args:
            mini_batch_size: Number of samples per mini-batch.

        Returns:
            Dict with policy_loss, value_loss, entropy, approx_kl.
        """
        self.model.train()

        # Compute GAE
        advantages, returns = self.compute_gae(
            self.buffer.rewards,
            self.buffer.values,
            self.buffer.dones,
        )

        # Normalize advantages
        if len(advantages) > 1:
            advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)

        # Old log probs (detached)
        old_log_probs = torch.stack(self.buffer.log_probs).detach()
        actions_tensor = torch.tensor(self.buffer.actions, dtype=torch.long)

        # Batch tokenize once (or use cached tokens)
        all_input_ids, all_attention_masks = self._pad_and_batch_tokens()

        # AMP scaler for CUDA mixed precision
        use_amp = self.model.device.type == "cuda"
        scaler = torch.amp.GradScaler("cuda", enabled=use_amp) if use_amp else None

        total_policy_loss = 0.0
        total_value_loss = 0.0
        total_entropy = 0.0
        total_kl = 0.0
        num_batches = 0
        n = len(self.buffer)

        for _ in range(self.ppo_epochs):
            # Shuffle indices for mini-batching
            indices = torch.randperm(n)

            for start in range(0, n, mini_batch_size):
                end = min(start + mini_batch_size, n)
                batch_idx = indices[start:end]

                b_input_ids = all_input_ids[batch_idx].to(self.model.device)
                b_attention_mask = all_attention_masks[batch_idx].to(self.model.device)
                b_actions = actions_tensor[batch_idx].to(self.model.device)
                b_old_log_probs = old_log_probs[batch_idx].to(self.model.device)
                b_advantages = advantages[batch_idx].to(self.model.device)
                b_returns = returns[batch_idx].to(self.model.device)

                # Forward + loss computation (all under AMP if CUDA)
                amp_ctx = torch.amp.autocast("cuda") if use_amp else contextlib.nullcontext()
                with amp_ctx:
                    new_log_probs, new_values, entropy = self.model.evaluate_actions(
                        b_input_ids, b_attention_mask, b_actions
                    )
                    ratio = torch.exp(new_log_probs - b_old_log_probs)
                    surr1 = ratio * b_advantages
                    surr2 = torch.clamp(ratio, 1.0 - self.clip_range, 1.0 + self.clip_range) * b_advantages
                    policy_loss = -torch.min(surr1, surr2).mean()
                    value_loss = nn.functional.mse_loss(new_values, b_returns)
                    loss = policy_loss + self.value_loss_coef * value_loss - self.entropy_coef * entropy.mean()

                self.optimizer.zero_grad()
                if use_amp and scaler is not None:
                    scaler.scale(loss).backward()
                    scaler.unscale_(self.optimizer)
                    nn.utils.clip_grad_norm_(self.model.get_trainable_params(), self.max_grad_norm)
                    scaler.step(self.optimizer)
                    scaler.update()
                else:
                    loss.backward()
                    nn.utils.clip_grad_norm_(self.model.get_trainable_params(), self.max_grad_norm)
                    self.optimizer.step()

                # Accumulate stats
                total_policy_loss += policy_loss.item()
                total_value_loss += value_loss.item()
                total_entropy += entropy.mean().item()
                with torch.no_grad():
                    total_kl += (b_old_log_probs - new_log_probs).mean().item()
                num_batches += 1

        self.buffer.clear()

        denom = max(num_batches, 1)
        return {
            "policy_loss": total_policy_loss / denom,
            "value_loss": total_value_loss / denom,
            "entropy": total_entropy / denom,
            "approx_kl": total_kl / denom,
        }

    def train(
        self,
        env: HyperliquidTradingEnv,
        total_updates: int = 1000,
        steps_per_rollout: int = 100,
        eval_env: Optional[HyperliquidTradingEnv] = None,
        eval_every: int = 50,
        save_dir: Path = Path("models/flag_trader"),
        save_every: int = 100,
    ) -> list[dict]:
        """Main training loop.

        Args:
            env: Training environment.
            total_updates: Number of PPO update cycles.
            steps_per_rollout: Steps per rollout collection.
            eval_env: Optional separate environment for evaluation.
            eval_every: Run evaluation every N updates.
            save_dir: Directory for checkpoint saves.
            save_every: Save checkpoint every N updates.

        Returns:
            List of stats dicts from each update.
        """
        save_dir.mkdir(parents=True, exist_ok=True)
        all_stats: list[dict] = []

        for update_idx in range(1, total_updates + 1):
            # Collect rollout
            rollout_stats = self.collect_rollout(env, steps_per_rollout)

            # PPO update
            update_stats = self.update()

            # Merge stats
            stats = {**rollout_stats, **update_stats, "update": update_idx}
            all_stats.append(stats)

            # Log every 10 updates
            if update_idx % 10 == 0:
                logger.info(
                    "Update %d/%d | reward: %.4f | policy_loss: %.4f | value_loss: %.4f",
                    update_idx,
                    total_updates,
                    stats["mean_reward"],
                    stats["policy_loss"],
                    stats["value_loss"],
                )
                print(
                    f"Update {update_idx}/{total_updates} | "
                    f"reward: {stats['mean_reward']:.4f} | "
                    f"policy_loss: {stats['policy_loss']:.4f} | "
                    f"value_loss: {stats['value_loss']:.4f}"
                )

            # Evaluate periodically
            if eval_env is not None and update_idx % eval_every == 0:
                eval_stats = self.evaluate(eval_env)
                stats["eval_result"] = eval_stats
                logger.info(
                    "Eval @ %d | mean_return: %.4f | std_return: %.4f",
                    update_idx,
                    eval_stats["mean_return"],
                    eval_stats["std_return"],
                )
                print(
                    f"  Eval @ {update_idx} | "
                    f"mean_return: {eval_stats['mean_return']:.4f} | "
                    f"std_return: {eval_stats['std_return']:.4f}"
                )

            # Save checkpoint
            if update_idx % save_every == 0:
                ckpt_path = save_dir / f"checkpoint_{update_idx}.pt"
                self.model.save_trainable(ckpt_path)
                logger.info("Saved checkpoint: %s", ckpt_path)

        # Save final checkpoint
        final_path = save_dir / "checkpoint_final.pt"
        self.model.save_trainable(final_path)
        logger.info("Saved final checkpoint: %s", final_path)

        return all_stats

    @torch.no_grad()
    def evaluate(
        self, env: HyperliquidTradingEnv, num_episodes: int = 5
    ) -> dict[str, float | list]:
        """Evaluate policy without training.

        Args:
            env: Environment for evaluation.
            num_episodes: Number of episodes to run.

        Returns:
            Dict with mean_return, std_return, mean_episode_length, actions_distribution.
        """
        self.model.eval()
        episode_returns: list[float] = []
        episode_lengths: list[int] = []
        action_counts = [0, 0, 0]  # Sell, Hold, Buy

        for _ in range(num_episodes):
            obs, _ = env.reset()
            ep_return = 0.0
            ep_length = 0

            while True:
                prompt = self._obs_to_prompt(obs)
                action, _, _ = self.model.get_action(prompt)
                obs, reward, terminated, truncated, info = env.step(action)

                ep_return += reward
                ep_length += 1
                action_counts[action] += 1

                if terminated or truncated:
                    break

            episode_returns.append(ep_return)
            episode_lengths.append(ep_length)

        total_actions = sum(action_counts) or 1
        return {
            "mean_return": float(np.mean(episode_returns)),
            "std_return": float(np.std(episode_returns)),
            "mean_episode_length": float(np.mean(episode_lengths)),
            "actions_distribution": [c / total_actions for c in action_counts],
        }
