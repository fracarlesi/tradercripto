"""
Autonomous Research Loop for FLAG-Trader (DeepSeek / model-driven)
===================================================================

Iterates through TRAINING hyperparameter experiments, modifying one parameter
at a time, training a fresh model, validating with the REPLAY ENGINE
(same pipeline as live), and keeping improvements.

Based on the autoresearch pattern (Karpathy):
modify -> train -> evaluate (replay) -> keep/discard -> repeat

IMPORTANT: No trading parameters (TP, SL, threshold) are tuned here.
Everything related to trading decisions comes from the model itself.
We only optimise HOW we train the model to make better decisions.
"""

from __future__ import annotations

import json
import logging
import math
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
import torch

from .reward import REWARD_FUNCTIONS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ReplayMetrics:
    """Metrics from replay engine validation."""

    pnl_pct: float
    profit_factor: float
    sharpe: float
    win_rate: float
    total_trades: int
    max_drawdown_pct: float

    @property
    def score(self) -> float:
        """Single score for comparison. Higher is better."""
        # Sharpe primary, PF and WR secondary
        return self.sharpe * 0.5 + self.profit_factor * 0.3 + self.win_rate * 0.2


@dataclass
class ExperimentResult:
    experiment_id: int
    parameter: str
    value: Any
    baseline_score: float
    result_score: float
    improvement: float
    improvement_pct: float
    kept: bool
    duration_seconds: float
    details: dict = field(default_factory=dict)


@dataclass
class ResearchState:
    """Stato corrente della ricerca, salvato e ripristinabile."""

    best_config: dict
    best_score: float
    experiments_completed: list[ExperimentResult]
    total_time_seconds: float


class AutoResearcher:
    """Autonomous research loop for FLAG-Trader hyperparameter optimization.

    Iterates through experiments, modifying one TRAINING parameter at a time,
    training a model with PPO, validating with the replay engine, and keeping
    improvements.

    The replay engine uses the SAME inference pipeline as the live bot, so
    results are directly comparable to production performance.
    """

    DEFAULT_CONFIG: dict[str, Any] = {
        "model_name": "deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B",
        "freeze_pct": 0.8,
        "lr": 3e-5,
        "gamma": 0.99,
        "gae_lambda": 0.95,
        "clip_range": 0.2,
        "ppo_epochs": 4,
        "entropy_coef": 0.01,
        "value_loss_coef": 0.5,
        "max_grad_norm": 0.5,
        "window_size": 20,
        "transaction_cost_bps": 7,  # realistic: 0.07% round-trip
        "reward_fn": "sharpe_delta",
        "ppo_updates": 100,
        "steps_per_rollout": 50,
    }

    EXPERIMENT_GRID: list[dict[str, Any]] = [
        # Reward function -- what the model optimizes for
        {"param": "reward_fn", "values": ["sharpe_delta", "sortino_delta", "calmar_delta"]},
        # Learning rate -- how fast it learns
        {"param": "lr", "values": [1e-5, 3e-5, 1e-4]},
        # Training duration -- how long it learns
        {"param": "ppo_updates", "values": [50, 100, 200]},
        # Steps per rollout -- how much experience per update
        {"param": "steps_per_rollout", "values": [30, 50, 100]},
        # Architecture -- how much of the LLM to fine-tune
        {"param": "freeze_pct", "values": [0.7, 0.8, 0.9]},
        # PPO hyperparams
        {"param": "clip_range", "values": [0.1, 0.2, 0.3]},
        {"param": "entropy_coef", "values": [0.005, 0.01, 0.05]},
        # Context window -- how much history the model sees
        {"param": "window_size", "values": [20, 50]},
    ]

    # Replay config (not tuned -- mirrors production)
    REPLAY_CONFIG: dict[str, Any] = {
        "initial_capital": 100.0,
        "max_positions": 1,
        "confidence_threshold": 0.6,
        "leverage": 3,
        "max_hold_bars": 24,
        "position_pct": 0.25,
        "use_market_context": True,
        "trigger_mode": True,
        "trigger_threshold": 2.0,
        "trigger_lookback": 20,
    }

    # Number of candles for the OOS replay test (7 days of 15m = 672 bars)
    OOS_BARS = 672

    def __init__(
        self,
        candles_by_symbol: dict[str, list[dict]],
        baseline_config: Optional[dict] = None,
        results_file: Path = Path("experiments.json"),
        device: str = "auto",
    ) -> None:
        self.candles_by_symbol = candles_by_symbol
        self.config: dict[str, Any] = {**self.DEFAULT_CONFIG, **(baseline_config or {})}
        self.results_file = results_file
        self.device = device

        # Split candles: training set = all except last OOS_BARS, test set = last OOS_BARS
        self.train_candles_by_symbol: dict[str, list[dict]] = {}
        self.test_candles_by_symbol: dict[str, list[dict]] = {}
        for symbol, candles in self.candles_by_symbol.items():
            if len(candles) > self.OOS_BARS + 50:
                self.train_candles_by_symbol[symbol] = candles[: -self.OOS_BARS]
                self.test_candles_by_symbol[symbol] = candles[-self.OOS_BARS:]
            else:
                logger.warning(
                    "%s has only %d bars, need >%d for train/test split -- skipping",
                    symbol, len(candles), self.OOS_BARS + 50,
                )

        if not self.train_candles_by_symbol:
            raise ValueError("No symbols have enough data for train/test split")

        self.state = ResearchState(
            best_config=dict(self.config),
            best_score=0.0,
            experiments_completed=[],
            total_time_seconds=0.0,
        )

        if self.results_file.exists():
            self._load_state()

    def run(
        self,
        max_experiments: int = 20,
        time_budget_minutes: float = 300,
    ) -> ResearchState:
        """Main autoresearch loop.

        For each experiment:
          1. Pick next experiment from queue
          2. Modify config with new parameter value
          3. Train model with PPO
          4. Validate with REPLAY ENGINE on held-out data
          5. If replay score improves -> update best config
          6. Log result and save state
          7. Check time budget
        """
        time_budget_seconds = time_budget_minutes * 60
        start_time = time.time()
        experiments = self._generate_experiment_queue()

        # Skip already completed experiments
        completed_keys = {
            (e.parameter, str(e.value)) for e in self.state.experiments_completed
        }
        experiments = [
            e for e in experiments
            if (e["param"], str(e["value"])) not in completed_keys
        ]

        if not experiments:
            logger.info("All experiments already completed")
            return self.state

        # Run baseline if no best_score yet
        if self.state.best_score == 0.0:
            logger.info("Running baseline (train + replay)...")
            baseline_metrics = self._run_experiment(self.config)
            self.state.best_score = baseline_metrics.score
            logger.info(
                "Baseline: score=%.4f (Sharpe=%.3f, PF=%.2f, WR=%.1f%%, PnL=%.2f%%)",
                baseline_metrics.score, baseline_metrics.sharpe,
                baseline_metrics.profit_factor, baseline_metrics.win_rate,
                baseline_metrics.pnl_pct,
            )

        for exp in experiments[:max_experiments]:
            elapsed = time.time() - start_time
            if elapsed > time_budget_seconds:
                logger.info(
                    "Time budget exceeded (%.1fm / %.1fm)",
                    elapsed / 60, time_budget_minutes,
                )
                break

            param, value = exp["param"], exp["value"]

            # Skip if current value equals best config
            if self.state.best_config.get(param) == value:
                continue

            logger.info("=" * 60)
            logger.info(
                "Experiment %d: %s = %s",
                len(self.state.experiments_completed) + 1, param, value,
            )
            logger.info("=" * 60)

            test_config = {**self.state.best_config, param: value}

            exp_start = time.time()
            replay_metrics = self._run_experiment(test_config)
            exp_duration = time.time() - exp_start

            improvement = replay_metrics.score - self.state.best_score
            if self.state.best_score != 0:
                improvement_pct = improvement / abs(self.state.best_score) * 100
            else:
                improvement_pct = 0.0

            # Keep if score improved AND not losing more than 2%
            kept = improvement > 0 and replay_metrics.pnl_pct > -2.0

            result = ExperimentResult(
                experiment_id=len(self.state.experiments_completed) + 1,
                parameter=param,
                value=value,
                baseline_score=self.state.best_score,
                result_score=replay_metrics.score,
                improvement=improvement,
                improvement_pct=improvement_pct,
                kept=kept,
                duration_seconds=exp_duration,
                details={
                    "pnl_pct": replay_metrics.pnl_pct,
                    "profit_factor": replay_metrics.profit_factor,
                    "sharpe": replay_metrics.sharpe,
                    "win_rate": replay_metrics.win_rate,
                    "total_trades": replay_metrics.total_trades,
                    "max_drawdown_pct": replay_metrics.max_drawdown_pct,
                },
            )

            self.state.experiments_completed.append(result)
            self.state.total_time_seconds += exp_duration

            if kept:
                logger.info(
                    "KEPT: %s=%s improved score by %+.1f%% (%.4f -> %.4f)",
                    param, value, improvement_pct,
                    self.state.best_score, replay_metrics.score,
                )
                self.state.best_config[param] = value
                self.state.best_score = replay_metrics.score
            else:
                logger.info(
                    "DISCARDED: %s=%s (score %.4f vs baseline %.4f, PnL %.2f%%)",
                    param, value, replay_metrics.score,
                    self.state.best_score, replay_metrics.pnl_pct,
                )

            self._save_state()

        logger.info("Research complete. Best score: %.4f", self.state.best_score)
        logger.info("Total time: %.1f minutes", self.state.total_time_seconds / 60)

        return self.state

    def _generate_experiment_queue(self) -> list[dict[str, Any]]:
        """Flatten EXPERIMENT_GRID into individual experiments."""
        queue: list[dict[str, Any]] = []
        for group in self.EXPERIMENT_GRID:
            for value in group["values"]:
                queue.append({"param": group["param"], "value": value})
        return queue

    def _run_experiment(self, config: dict[str, Any]) -> ReplayMetrics:
        """Train model with config, then validate with replay engine."""
        from .environment import HyperliquidTradingEnv
        from .model import FlagTraderModel
        from .prompt import PromptBuilder
        from .trainer import PPOTrainer

        window_size = config.get("window_size", 20)

        # 1. Create fresh model
        model = FlagTraderModel(
            model_name=config["model_name"],
            freeze_pct=config.get("freeze_pct", 0.8),
            device=self.device,
        )

        # 2. Create trainer with config params
        prompt_builder = PromptBuilder(candle_window=window_size)
        trainer = PPOTrainer(
            model=model,
            prompt_builder=prompt_builder,
            lr=config.get("lr", 3e-5),
            gamma=config.get("gamma", 0.99),
            gae_lambda=config.get("gae_lambda", 0.95),
            clip_range=config.get("clip_range", 0.2),
            ppo_epochs=config.get("ppo_epochs", 4),
            value_loss_coef=config.get("value_loss_coef", 0.5),
            entropy_coef=config.get("entropy_coef", 0.01),
            max_grad_norm=config.get("max_grad_norm", 0.5),
        )

        # 3. Build training candles as numpy array from the longest symbol
        # (PPO env expects numpy array of shape (N, 5))
        longest_symbol = max(
            self.train_candles_by_symbol, key=lambda s: len(self.train_candles_by_symbol[s])
        )
        train_list = self.train_candles_by_symbol[longest_symbol]
        train_np = np.array(
            [[c["open"], c["high"], c["low"], c["close"], c["volume"]] for c in train_list],
            dtype=np.float64,
        )

        reward_fn_name = config.get("reward_fn", "sharpe_delta")
        if reward_fn_name not in REWARD_FUNCTIONS:
            raise ValueError(
                f"Unknown reward_fn '{reward_fn_name}'. Available: {list(REWARD_FUNCTIONS.keys())}"
            )

        train_env = HyperliquidTradingEnv(
            candles=train_np,
            window_size=window_size,
            transaction_cost_bps=config.get("transaction_cost_bps", 7),
        )

        # 4. Train with PPO
        ppo_updates = config.get("ppo_updates", 100)
        steps_per_rollout = config.get("steps_per_rollout", 50)

        logger.info(
            "Training: %d updates x %d steps, lr=%.1e, model=%s",
            ppo_updates, steps_per_rollout, config.get("lr", 3e-5),
            config["model_name"],
        )

        for update_idx in range(1, ppo_updates + 1):
            trainer.collect_rollout(train_env, num_steps=steps_per_rollout)
            stats = trainer.update()
            if update_idx % 25 == 0:
                logger.info(
                    "  PPO update %d/%d -- policy_loss: %.4f, reward: %.4f",
                    update_idx, ppo_updates,
                    stats.get("policy_loss", 0.0),
                    stats.get("mean_reward", 0.0),
                )

        # 5. Validate with replay engine on held-out test data
        replay_metrics = self._run_replay(model, config)

        logger.info(
            "Replay result: PnL=%.2f%%, PF=%.2f, Sharpe=%.3f, WR=%.1f%%, Trades=%d, Score=%.4f",
            replay_metrics.pnl_pct, replay_metrics.profit_factor,
            replay_metrics.sharpe, replay_metrics.win_rate,
            replay_metrics.total_trades, replay_metrics.score,
        )

        return replay_metrics

    def _run_replay(self, model: "FlagTraderModel", config: dict[str, Any]) -> ReplayMetrics:
        """Run replay engine with trained model on held-out test data.

        Uses the SAME FlagTraderReplay class as the CLI replay script,
        so results are directly comparable to production.
        """
        from ..scripts.replay_flag_trader import FlagTraderReplay
        from .prompt import PromptBuilder

        window_size = config.get("window_size", 20)
        prompt_builder = PromptBuilder(candle_window=window_size)

        replay_config = {
            **self.REPLAY_CONFIG,
            "candle_window": window_size,
        }

        replay = FlagTraderReplay(
            model=model,
            prompt_builder=prompt_builder,
            config=replay_config,
        )

        result = replay.run(self.test_candles_by_symbol)

        return ReplayMetrics(
            pnl_pct=(result.total_pnl / result.initial_capital * 100) if result.initial_capital > 0 else 0.0,
            profit_factor=min(result.profit_factor, 99.99),
            sharpe=result.sharpe_ratio,
            win_rate=result.win_rate,
            total_trades=result.total_trades,
            max_drawdown_pct=result.max_drawdown_pct,
        )

    def _save_state(self) -> None:
        """Save research state to JSON."""
        data = {
            "best_config": self.state.best_config,
            "best_score": self.state.best_score,
            "total_time_seconds": self.state.total_time_seconds,
            "experiments": [asdict(e) for e in self.state.experiments_completed],
        }
        self.results_file.write_text(json.dumps(data, indent=2, default=str))

    def _load_state(self) -> None:
        """Load previous research state."""
        try:
            data = json.loads(self.results_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load state from %s: %s", self.results_file, e)
            return

        self.state.best_config = data.get("best_config", dict(self.config))
        self.state.best_score = data.get("best_score", 0.0)
        self.state.total_time_seconds = data.get("total_time_seconds", 0.0)
        self.state.experiments_completed = [
            ExperimentResult(**e) for e in data.get("experiments", [])
        ]

    def get_summary(self) -> str:
        """Human-readable summary of research progress."""
        lines = [
            "Autoresearch Summary",
            "=" * 40,
            f"Experiments completed: {len(self.state.experiments_completed)}",
            f"Best score: {self.state.best_score:.4f}",
            f"Total time: {self.state.total_time_seconds / 60:.1f} minutes",
            "",
            "Best config:",
        ]
        for k, v in sorted(self.state.best_config.items()):
            lines.append(f"  {k}: {v}")

        kept = [e for e in self.state.experiments_completed if e.kept]
        if kept:
            lines.append(f"\nKept improvements ({len(kept)}):")
            for e in kept:
                lines.append(
                    f"  {e.parameter}={e.value} -> score +{e.improvement_pct:.1f}%"
                )

        discarded = [e for e in self.state.experiments_completed if not e.kept]
        if discarded:
            lines.append(f"\nDiscarded ({len(discarded)}):")
            for e in discarded:
                d = e.details
                lines.append(
                    f"  {e.parameter}={e.value} "
                    f"(PnL={d.get('pnl_pct', 0):.1f}%, PF={d.get('profit_factor', 0):.2f})"
                )

        return "\n".join(lines)
