"""
Autonomous Research Loop for FLAG-Trader
==========================================

Iterates through hyperparameter experiments, modifying one parameter at a time,
running walk-forward validation, and keeping improvements.

Based on the autoresearch pattern (Karpathy):
modify -> train -> evaluate -> keep/discard -> repeat
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .reward import REWARD_FUNCTIONS
from .walk_forward import WalkForwardResult, WalkForwardValidator

logger = logging.getLogger(__name__)


@dataclass
class ExperimentResult:
    experiment_id: int
    parameter: str
    value: Any
    baseline_sharpe: float
    result_sharpe: float
    improvement: float  # result - baseline
    improvement_pct: float  # (result - baseline) / |baseline| * 100
    kept: bool
    walk_forward_passed: bool
    duration_seconds: float
    details: dict = field(default_factory=dict)


@dataclass
class ResearchState:
    """Stato corrente della ricerca, salvato e ripristinabile."""

    best_config: dict
    best_sharpe: float
    experiments_completed: list[ExperimentResult]
    total_time_seconds: float


class AutoResearcher:
    """Autonomous research loop for FLAG-Trader hyperparameter optimization.

    Iterates through experiments, modifying one parameter at a time,
    running walk-forward validation, and keeping improvements.
    """

    DEFAULT_CONFIG: dict[str, Any] = {
        "model_name": "HuggingFaceTB/SmolLM2-135M-Instruct",
        "freeze_pct": 0.8,
        "lr": 1e-5,
        "gamma": 0.99,
        "gae_lambda": 0.95,
        "clip_range": 0.2,
        "ppo_epochs": 4,
        "entropy_coef": 0.01,
        "value_loss_coef": 0.5,
        "max_grad_norm": 0.5,
        "window_size": 20,
        "transaction_cost_bps": 5.0,
        "reward_fn": "sharpe_delta",
        "ppo_updates": 100,
        "steps_per_rollout": 50,
    }

    EXPERIMENT_GRID: list[dict[str, Any]] = [
        # Reward function — most impactful, test first
        {"param": "reward_fn", "values": ["sharpe_delta", "sortino_delta", "calmar_delta"]},
        # Learning rate — second most impactful
        {"param": "lr", "values": [5e-6, 1e-5, 5e-5, 1e-4]},
        # Architecture
        {"param": "freeze_pct", "values": [0.6, 0.7, 0.8, 0.9]},
        # PPO hyperparams
        {"param": "clip_range", "values": [0.1, 0.2, 0.3]},
        {"param": "ppo_epochs", "values": [2, 4, 8]},
        {"param": "entropy_coef", "values": [0.001, 0.01, 0.05]},
        # Environment
        {"param": "window_size", "values": [10, 20, 50]},
        {"param": "transaction_cost_bps", "values": [3, 5, 10]},
    ]

    # Keys passed directly to WalkForwardValidator.run()
    _WF_RUN_KEYS = {
        "model_name", "freeze_pct", "ppo_updates", "steps_per_rollout",
        "lr", "reward_fn", "window_size", "gamma", "gae_lambda",
        "clip_range", "ppo_epochs", "entropy_coef",
    }

    def __init__(
        self,
        candles: np.ndarray,
        baseline_config: Optional[dict] = None,
        results_file: Path = Path("experiments.json"),
        train_months: int = 4,
        test_months: int = 2,
        step_months: int = 1,
    ) -> None:
        self.candles = candles
        self.config: dict[str, Any] = {**self.DEFAULT_CONFIG, **(baseline_config or {})}
        self.results_file = results_file
        self.train_months = train_months
        self.test_months = test_months
        self.step_months = step_months
        self.state = ResearchState(
            best_config=dict(self.config),
            best_sharpe=0.0,
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
          3. Run walk-forward validation
          4. If avg Sharpe OOS improves -> update best config
          5. Log result and save state
          6. Check time budget
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

        # Run baseline if no best_sharpe yet
        if self.state.best_sharpe == 0.0:
            logger.info("Running baseline walk-forward...")
            baseline_result = self._run_experiment(self.config)
            self.state.best_sharpe = baseline_result.avg_sharpe
            logger.info("Baseline Sharpe: %.4f", self.state.best_sharpe)

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
            wf_result = self._run_experiment(test_config)
            exp_duration = time.time() - exp_start

            improvement = wf_result.avg_sharpe - self.state.best_sharpe
            if self.state.best_sharpe != 0:
                improvement_pct = improvement / abs(self.state.best_sharpe) * 100
            else:
                improvement_pct = 0.0
            kept = improvement > 0 and wf_result.passed

            result = ExperimentResult(
                experiment_id=len(self.state.experiments_completed) + 1,
                parameter=param,
                value=value,
                baseline_sharpe=self.state.best_sharpe,
                result_sharpe=wf_result.avg_sharpe,
                improvement=improvement,
                improvement_pct=improvement_pct,
                kept=kept,
                walk_forward_passed=wf_result.passed,
                duration_seconds=exp_duration,
                details={
                    "avg_pf": wf_result.avg_pf,
                    "avg_max_dd": wf_result.avg_max_dd,
                    "windows_profitable": wf_result.windows_profitable,
                    "total_windows": wf_result.total_windows,
                },
            )

            self.state.experiments_completed.append(result)
            self.state.total_time_seconds += exp_duration

            if kept:
                logger.info(
                    "KEPT: %s=%s improved Sharpe by %+.1f%%",
                    param, value, improvement_pct,
                )
                self.state.best_config[param] = value
                self.state.best_sharpe = wf_result.avg_sharpe
            else:
                logger.info(
                    "DISCARDED: %s=%s (Sharpe %.4f vs baseline %.4f)",
                    param, value, wf_result.avg_sharpe, self.state.best_sharpe,
                )

            self._save_state()

        logger.info("Research complete. Best Sharpe: %.4f", self.state.best_sharpe)
        logger.info("Total time: %.1f minutes", self.state.total_time_seconds / 60)

        return self.state

    def _generate_experiment_queue(self) -> list[dict[str, Any]]:
        """Flatten EXPERIMENT_GRID into individual experiments."""
        queue: list[dict[str, Any]] = []
        for group in self.EXPERIMENT_GRID:
            for value in group["values"]:
                queue.append({"param": group["param"], "value": value})
        return queue

    def _run_experiment(self, config: dict[str, Any]) -> WalkForwardResult:
        """Run walk-forward validation with given config."""
        validator = WalkForwardValidator(
            candles=self.candles,
            train_months=self.train_months,
            test_months=self.test_months,
            step_months=self.step_months,
        )

        # Split config into WF.run() params and extras
        run_kwargs: dict[str, Any] = {}
        for key in self._WF_RUN_KEYS:
            if key in config:
                run_kwargs[key] = config[key]

        return validator.run(**run_kwargs)

    def _save_state(self) -> None:
        """Save research state to JSON."""
        data = {
            "best_config": self.state.best_config,
            "best_sharpe": self.state.best_sharpe,
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
        self.state.best_sharpe = data.get("best_sharpe", 0.0)
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
            f"Best Sharpe: {self.state.best_sharpe:.4f}",
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
                    f"  {e.parameter}={e.value} -> Sharpe +{e.improvement_pct:.1f}%"
                )

        return "\n".join(lines)
