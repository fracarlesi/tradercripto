"""
Walk-Forward Validation for FLAG-Trader
=========================================

Implements walk-forward (anchored or sliding) validation to assess
out-of-sample robustness of the PPO-trained LLM trading agent.

Each window:
  1. Train a fresh model on the training slice
  2. Test (no training) on the subsequent test slice
  3. Compute trading metrics (Sharpe, PF, MDD, win rate)

Pass/fail criteria ensure the strategy generalises across time.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from .environment import HyperliquidTradingEnv
from .model import FlagTraderModel
from .prompt import PromptBuilder
from .reward import REWARD_FUNCTIONS
from .trainer import PPOTrainer, obs_to_prompt_inputs

logger = logging.getLogger(__name__)


@dataclass
class WindowResult:
    window_id: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    sharpe: float
    profit_factor: float
    max_drawdown_pct: float
    total_trades: int
    win_rate: float
    net_return_pct: float


@dataclass
class WalkForwardResult:
    windows: list[WindowResult]
    avg_sharpe: float
    avg_pf: float
    avg_max_dd: float
    windows_profitable: int
    total_windows: int
    passed: bool


class WalkForwardValidator:
    """Walk-forward validation for FLAG-Trader models.

    Splits candle data into overlapping (train, test) windows and
    trains + tests a fresh model on each window independently.

    Args:
        candles: np.ndarray of shape (num_candles, 5) -- O, H, L, C, V.
        candles_per_month: Number of candles in one month (default: 2880 for 15min).
        train_months: Training window length in months.
        test_months: Test window length in months.
        step_months: Step size between consecutive windows in months.
    """

    # Pass criteria
    MIN_SHARPE = 1.0
    MIN_PF = 1.2
    MAX_DD = 20.0
    MIN_TRADES = 50
    MIN_PROFITABLE_WINDOWS_PCT = 0.67  # 2/3

    def __init__(
        self,
        candles: np.ndarray,
        candles_per_month: int = 2880,
        train_months: int = 4,
        test_months: int = 2,
        step_months: int = 1,
    ) -> None:
        self.candles = candles
        self.candles_per_month = candles_per_month
        self.train_months = train_months
        self.test_months = test_months
        self.step_months = step_months

    def _build_windows(self) -> list[tuple[int, int, int, int]]:
        """Generate (train_start, train_end, test_start, test_end) tuples.

        Sliding window: each subsequent window advances by step_months.
        """
        total_candles = len(self.candles)
        train_len = self.train_months * self.candles_per_month
        test_len = self.test_months * self.candles_per_month
        step_len = self.step_months * self.candles_per_month
        window_len = train_len + test_len

        windows: list[tuple[int, int, int, int]] = []
        start = 0

        while start + window_len <= total_candles:
            train_start = start
            train_end = start + train_len
            test_start = train_end
            test_end = train_end + test_len
            windows.append((train_start, train_end, test_start, test_end))
            start += step_len

        return windows

    def run(
        self,
        model_name: str = "HuggingFaceTB/SmolLM2-135M-Instruct",
        freeze_pct: float = 0.8,
        ppo_updates: int = 100,
        steps_per_rollout: int = 50,
        lr: float = 1e-5,
        reward_fn: str = "sharpe_delta",
        window_size: int = 20,
        **trainer_kwargs: float,
    ) -> WalkForwardResult:
        """Run walk-forward validation across all windows.

        For each window:
          1. Create fresh model
          2. Create train env (candles[train_start:train_end])
          3. Train with PPO
          4. Create test env (candles[test_start:test_end])
          5. Run test on test env (no training)
          6. Compute metrics (sharpe, PF, MDD, trades, win rate)

        Args:
            model_name: HuggingFace model identifier.
            freeze_pct: Fraction of transformer layers to freeze.
            ppo_updates: Number of PPO update cycles per window.
            steps_per_rollout: Steps per rollout collection.
            lr: Learning rate.
            reward_fn: Reward function name (key in REWARD_FUNCTIONS).
            window_size: Candle observation window for the environment.
            **trainer_kwargs: Extra kwargs passed to PPOTrainer.

        Returns:
            WalkForwardResult with per-window metrics and aggregate stats.
        """
        windows = self._build_windows()
        if not windows:
            logger.warning("No windows could be built from data (insufficient candles)")
            return WalkForwardResult(
                windows=[], avg_sharpe=0.0, avg_pf=0.0, avg_max_dd=0.0,
                windows_profitable=0, total_windows=0, passed=False,
            )

        if reward_fn not in REWARD_FUNCTIONS:
            raise ValueError(f"Unknown reward_fn '{reward_fn}'. Available: {list(REWARD_FUNCTIONS.keys())}")

        results: list[WindowResult] = []

        for i, (tr_start, tr_end, te_start, te_end) in enumerate(windows):
            logger.info(
                "Window %d/%d -- train[%d:%d] test[%d:%d]",
                i + 1, len(windows), tr_start, tr_end, te_start, te_end,
            )

            # Fresh model + trainer for each window
            model = FlagTraderModel(model_name=model_name, freeze_pct=freeze_pct)
            prompt_builder = PromptBuilder(candle_window=window_size)
            trainer = PPOTrainer(model=model, prompt_builder=prompt_builder, lr=lr, **trainer_kwargs)  # pyright: ignore[reportArgumentType]  # torch/SDK typing

            # Train environment
            train_candles = self.candles[tr_start:tr_end]
            train_env = HyperliquidTradingEnv(
                candles=train_candles, window_size=window_size,
            )

            # Training loop
            for update_idx in range(1, ppo_updates + 1):
                trainer.collect_rollout(train_env, num_steps=steps_per_rollout)
                stats = trainer.update()
                if update_idx % 25 == 0:
                    logger.info(
                        "  Window %d update %d/%d -- policy_loss: %.4f",
                        i + 1, update_idx, ppo_updates, stats["policy_loss"],
                    )

            # Test on held-out data
            test_candles = self.candles[te_start:te_end]
            wr = self._run_test_window(model, test_candles, window_size, i)
            results.append(wr)

            logger.info(
                "  Window %d result -- Sharpe: %.3f, PF: %.3f, MDD: %.1f%%, Trades: %d, WR: %.1f%%",
                i + 1, wr.sharpe, wr.profit_factor, wr.max_drawdown_pct,
                wr.total_trades, wr.win_rate * 100,
            )

        # Aggregate
        avg_sharpe = sum(w.sharpe for w in results) / len(results) if results else 0.0
        avg_pf = sum(w.profit_factor for w in results) / len(results) if results else 0.0
        avg_max_dd = sum(w.max_drawdown_pct for w in results) / len(results) if results else 0.0
        windows_profitable = sum(1 for w in results if w.net_return_pct > 0)

        wf_result = WalkForwardResult(
            windows=results,
            avg_sharpe=avg_sharpe,
            avg_pf=avg_pf,
            avg_max_dd=avg_max_dd,
            windows_profitable=windows_profitable,
            total_windows=len(results),
            passed=False,
        )
        wf_result.passed = self.passes_criteria(wf_result)
        return wf_result

    def _run_test_window(
        self,
        model: FlagTraderModel,
        test_candles: np.ndarray,
        window_size: int,
        window_id: int,
    ) -> WindowResult:
        """Run trained model on test data without training.

        Runs through all test candles collecting per-trade P&L,
        then computes trading metrics.
        """
        import torch

        model.eval()
        env = HyperliquidTradingEnv(candles=test_candles, window_size=window_size)
        prompt_builder = PromptBuilder(candle_window=window_size)
        obs, _ = env.reset()

        pnl_list: list[float] = []
        initial_value = env.initial_cash
        last_info: dict = {"total_value": initial_value}

        with torch.no_grad():
            while True:
                candles_data, portfolio, history = obs_to_prompt_inputs(obs)
                prompt = prompt_builder.build_prompt(candles_data, portfolio, history)
                action, _, _ = model.get_action(prompt)  # pyright: ignore[reportAssignmentType]  # torch/SDK typing
                obs, reward, terminated, truncated, info = env.step(action)
                last_info = info

                # Track closed-trade P&L
                if info.get("step_pnl", 0.0) != 0.0:
                    pnl_list.append(info["step_pnl"])

                if terminated or truncated:
                    break

        # Final portfolio value
        final_value = last_info.get("total_value", initial_value)
        net_return_pct = ((final_value - initial_value) / initial_value) * 100.0

        metrics = self._compute_metrics(pnl_list)

        return WindowResult(
            window_id=window_id,
            train_start=0,
            train_end=0,
            test_start=0,
            test_end=0,
            sharpe=metrics["sharpe"],
            profit_factor=metrics["profit_factor"],
            max_drawdown_pct=metrics["max_drawdown_pct"],
            total_trades=int(metrics["total_trades"]),
            win_rate=metrics["win_rate"],
            net_return_pct=net_return_pct,
        )

    def _compute_metrics(self, pnl_list: list[float]) -> dict[str, float]:
        """Compute trading metrics from a list of per-trade P&L values.

        Args:
            pnl_list: List of realized P&L for each closed trade.

        Returns:
            Dict with sharpe, profit_factor, max_drawdown_pct, win_rate, total_trades.
        """
        total_trades = len(pnl_list)
        if total_trades == 0:
            return {
                "sharpe": 0.0,
                "profit_factor": 0.0,
                "max_drawdown_pct": 0.0,
                "win_rate": 0.0,
                "total_trades": 0,
            }

        # Sharpe ratio
        mean_pnl = sum(pnl_list) / total_trades
        if total_trades >= 2:
            variance = sum((p - mean_pnl) ** 2 for p in pnl_list) / (total_trades - 1)
            std_pnl = math.sqrt(variance)
            sharpe = mean_pnl / std_pnl if std_pnl > 1e-12 else 0.0
        else:
            sharpe = 0.0

        # Profit factor
        gross_profit = sum(p for p in pnl_list if p > 0)
        gross_loss = abs(sum(p for p in pnl_list if p < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 1e-12 else (
            float("inf") if gross_profit > 0 else 0.0
        )
        # Cap inf for serialisation
        if profit_factor == float("inf"):
            profit_factor = 99.99

        # Max drawdown (on cumulative P&L curve)
        cumsum = []
        running = 0.0
        for p in pnl_list:
            running += p
            cumsum.append(running)

        peak = cumsum[0]
        max_dd = 0.0
        for v in cumsum:
            peak = max(peak, v)
            dd = peak - v
            max_dd = max(max_dd, dd)
        # Express as percentage of peak (or initial if peak <= 0)
        max_dd_pct = (max_dd / peak * 100.0) if peak > 1e-12 else 0.0

        # Win rate
        wins = sum(1 for p in pnl_list if p > 0)
        win_rate = wins / total_trades

        return {
            "sharpe": sharpe,
            "profit_factor": profit_factor,
            "max_drawdown_pct": max_dd_pct,
            "win_rate": win_rate,
            "total_trades": float(total_trades),
        }

    def passes_criteria(self, result: WalkForwardResult) -> bool:
        """Check if walk-forward results meet all pass criteria.

        Criteria:
          - avg_sharpe >= MIN_SHARPE
          - avg_pf >= MIN_PF
          - avg_max_dd <= MAX_DD
          - profitable windows >= MIN_PROFITABLE_WINDOWS_PCT
        """
        if result.total_windows == 0:
            return False
        profitable_pct = result.windows_profitable / result.total_windows
        return (
            result.avg_sharpe >= self.MIN_SHARPE
            and result.avg_pf >= self.MIN_PF
            and result.avg_max_dd <= self.MAX_DD
            and profitable_pct >= self.MIN_PROFITABLE_WINDOWS_PCT
        )

    def save_results(self, result: WalkForwardResult, path: Path) -> None:
        """Save walk-forward results as JSON."""
        data = {
            "summary": {
                "avg_sharpe": result.avg_sharpe,
                "avg_pf": result.avg_pf,
                "avg_max_dd": result.avg_max_dd,
                "windows_profitable": result.windows_profitable,
                "total_windows": result.total_windows,
                "passed": result.passed,
            },
            "windows": [asdict(w) for w in result.windows],
        }
        path.write_text(json.dumps(data, indent=2))
        logger.info("Walk-forward results saved to %s", path)
