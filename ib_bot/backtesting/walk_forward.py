"""
IB Backtesting - Walk-Forward Validation Engine
=================================================

Splits a date range into rolling train/validate windows and performs:
1. Parameter sweep on each train period (in-sample)
2. Best config selection by robustness score (not just PnL)
3. Out-of-sample validation on the validate period
4. Aggregation of OOS results across all windows

This prevents overfitting by ensuring the selected parameters
generalize to unseen data in every window.

Usage:
    python -m ib_bot.backtesting walk-forward --days 90 --train 30 --validate 10 --step 5
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field, replace, asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .config import IBBacktestConfig
from .orb_detector import detect_opening_range
from .robustness import (
    calculate_robustness_score,
    classify_robustness,
    stats_from_backtest_result,
)
from .session import get_trading_days
from .simulator import ORBSimulator
from .simulator_ema import EMASimulator, EMAStrategyConfig
from .slippage import SlippageScenario, apply_slippage, run_slippage_scenarios
from .stats import IBBacktestResult

logger = logging.getLogger(__name__)

RESULTS_DIR = Path(__file__).parent / "results"


# =========================================================================
# Configuration dataclasses
# =========================================================================

@dataclass
class WalkForwardConfig:
    """Configuration for walk-forward validation."""

    total_days: int = 90
    train_days: int = 30
    validate_days: int = 10
    step_days: int = 5
    symbols: list[str] = field(default_factory=lambda: ["MES", "MNQ"])
    strategy: str = "orb"  # "orb" or "ema_momentum"
    account_size: float = 10_000.0
    slippage_scenario: str | None = None  # None, "normal", "all", etc.

    # Parameter grid to sweep (strategy-specific)
    # For ORB: keys like "reward_risk_ratio", "breakout_buffer_ticks", etc.
    # For EMA: keys like "ema_fast", "ema_slow", "atr_stop_multiplier", etc.
    param_grid: dict[str, list[Any]] = field(default_factory=dict)


@dataclass
class WindowResult:
    """Results from a single walk-forward window."""

    window_id: int
    train_start: str
    train_end: str
    validate_start: str
    validate_end: str
    best_params: dict[str, Any]
    in_sample_pnl: float
    in_sample_trades: int
    in_sample_pf: float
    in_sample_sharpe: float
    out_sample_pnl: float
    out_sample_trades: int
    out_sample_pf: float
    out_sample_sharpe: float
    robustness_score: float
    robustness_class: str


@dataclass
class WalkForwardResult:
    """Aggregated walk-forward validation result."""

    config: WalkForwardConfig
    windows: list[WindowResult]

    @property
    def total_oos_pnl(self) -> float:
        return sum(w.out_sample_pnl for w in self.windows)

    @property
    def total_oos_trades(self) -> int:
        return sum(w.out_sample_trades for w in self.windows)

    @property
    def oos_profit_factor(self) -> float:
        gross_profit = sum(
            w.out_sample_pnl for w in self.windows if w.out_sample_pnl > 0
        )
        gross_loss = abs(sum(
            w.out_sample_pnl for w in self.windows if w.out_sample_pnl < 0
        ))
        return gross_profit / gross_loss if gross_loss > 0 else 999.0

    @property
    def windows_profitable(self) -> int:
        return sum(1 for w in self.windows if w.out_sample_pnl > 0)

    @property
    def consistency_score(self) -> float:
        """Percentage of windows that were profitable OOS."""
        if not self.windows:
            return 0.0
        return self.windows_profitable / len(self.windows) * 100.0

    @property
    def degradation_ratio(self) -> float:
        """OOS PnL / IS PnL. Values near 1.0 = good generalization."""
        total_is = sum(w.in_sample_pnl for w in self.windows)
        if total_is == 0:
            return 0.0
        return self.total_oos_pnl / total_is

    @property
    def total_is_pnl(self) -> float:
        return sum(w.in_sample_pnl for w in self.windows)

    @property
    def avg_robustness(self) -> float:
        if not self.windows:
            return 0.0
        return sum(w.robustness_score for w in self.windows) / len(self.windows)


# =========================================================================
# Default parameter grids
# =========================================================================

DEFAULT_ORB_GRID: dict[str, list[Any]] = {
    "reward_risk_ratio": [1.5, 2.0, 2.5],
    "breakout_buffer_ticks": [1, 2, 3],
    "max_entry_time": ["11:30", "14:00"],
    "min_range_ticks": [4, 8],
    "max_range_ticks": [80, 120],
}

DEFAULT_EMA_GRID: dict[str, list[Any]] = {
    "ema_fast": [5, 9],
    "ema_slow": [13, 21],
    "atr_stop_multiplier": [1.5, 2.0, 3.0],
    "reward_risk_ratio": [1.5, 2.0],
}


# =========================================================================
# Walk-Forward Engine
# =========================================================================

class WalkForwardEngine:
    """Walk-forward validation engine.

    Orchestrates rolling-window train/validate cycles using existing
    simulators (ORBSimulator, EMASimulator).
    """

    def __init__(self) -> None:
        self._bars_by_day: dict[str, dict[str, list[dict[str, Any]]]] = {}

    def set_data(
        self,
        bars_by_day: dict[str, dict[str, list[dict[str, Any]]]],
    ) -> None:
        """Set the full dataset. Must be called before run().

        Args:
            bars_by_day: {date_str: {symbol: [bar_dicts]}} covering the
                         entire walk-forward period.
        """
        self._bars_by_day = bars_by_day

    def run(self, config: WalkForwardConfig) -> WalkForwardResult:
        """Execute walk-forward validation.

        Args:
            config: Walk-forward configuration.

        Returns:
            WalkForwardResult with all window results and aggregated metrics.
        """
        if not self._bars_by_day:
            raise ValueError(
                "No data loaded. Call set_data() before run()."
            )

        # Determine available trading days from the data
        all_dates = sorted(self._bars_by_day.keys())
        if not all_dates:
            raise ValueError("No trading days in data.")

        # Build rolling windows
        windows = self._build_windows(all_dates, config)
        logger.info(
            "Walk-forward: %d windows (train=%dd, validate=%dd, step=%dd)",
            len(windows), config.train_days, config.validate_days,
            config.step_days,
        )

        results: list[WindowResult] = []

        for i, (train_dates, val_dates) in enumerate(windows):
            logger.info(
                "Window %d/%d: train %s->%s (%d days), validate %s->%s (%d days)",
                i + 1, len(windows),
                train_dates[0], train_dates[-1], len(train_dates),
                val_dates[0], val_dates[-1], len(val_dates),
            )

            # Filter data for train and validate periods
            train_data = {d: self._bars_by_day[d] for d in train_dates if d in self._bars_by_day}
            val_data = {d: self._bars_by_day[d] for d in val_dates if d in self._bars_by_day}

            if not train_data or not val_data:
                logger.warning("Window %d: insufficient data, skipping", i + 1)
                continue

            # Run sweep on train data
            best_params, is_result, robustness = self._sweep_train(
                train_data, config,
            )

            # Run best config on validate data
            oos_result = self._run_single(val_data, best_params, config)

            window_result = WindowResult(
                window_id=i + 1,
                train_start=train_dates[0],
                train_end=train_dates[-1],
                validate_start=val_dates[0],
                validate_end=val_dates[-1],
                best_params=best_params,
                in_sample_pnl=is_result.net_pnl,
                in_sample_trades=is_result.count,
                in_sample_pf=is_result.profit_factor,
                in_sample_sharpe=is_result.sharpe,
                out_sample_pnl=oos_result.net_pnl,
                out_sample_trades=oos_result.count,
                out_sample_pf=oos_result.profit_factor,
                out_sample_sharpe=oos_result.sharpe,
                robustness_score=robustness,
                robustness_class=classify_robustness(robustness),
            )
            results.append(window_result)

            logger.info(
                "  Window %d result: IS=$%.2f (%d trades), "
                "OOS=$%.2f (%d trades), robustness=%.1f (%s)",
                i + 1,
                is_result.net_pnl, is_result.count,
                oos_result.net_pnl, oos_result.count,
                robustness, classify_robustness(robustness),
            )

        wf_result = WalkForwardResult(config=config, windows=results)

        # Save results
        self._save_results(wf_result)

        return wf_result

    # ------------------------------------------------------------------
    # Window construction
    # ------------------------------------------------------------------

    def _build_windows(
        self,
        all_dates: list[str],
        config: WalkForwardConfig,
    ) -> list[tuple[list[str], list[str]]]:
        """Build rolling train/validate date windows.

        Returns list of (train_dates, validate_dates) tuples.
        """
        total = len(all_dates)
        window_size = config.train_days + config.validate_days
        step = config.step_days

        windows: list[tuple[list[str], list[str]]] = []
        start = 0

        while start + window_size <= total:
            train_dates = all_dates[start:start + config.train_days]
            val_start = start + config.train_days
            val_dates = all_dates[val_start:val_start + config.validate_days]

            if len(train_dates) >= config.train_days and len(val_dates) >= config.validate_days:
                windows.append((train_dates, val_dates))

            start += step

        return windows

    # ------------------------------------------------------------------
    # Train sweep
    # ------------------------------------------------------------------

    def _sweep_train(
        self,
        train_data: dict[str, dict[str, list[dict[str, Any]]]],
        config: WalkForwardConfig,
    ) -> tuple[dict[str, Any], IBBacktestResult, float]:
        """Sweep parameter grid on training data, return best by robustness.

        Returns:
            (best_params, best_result, best_robustness_score)
        """
        param_grid = config.param_grid
        if not param_grid:
            param_grid = (
                DEFAULT_ORB_GRID if config.strategy == "orb"
                else DEFAULT_EMA_GRID
            )

        # Generate all parameter combinations
        combos = _generate_param_combos(param_grid)

        if not combos:
            # No grid defined, run with defaults
            combos = [{}]

        best_score = -1.0
        best_params: dict[str, Any] = {}
        best_result: IBBacktestResult | None = None

        for params in combos:
            result = self._run_single(train_data, params, config)
            stats = stats_from_backtest_result(result)
            score = calculate_robustness_score(stats)

            if score > best_score:
                best_score = score
                best_params = params
                best_result = result

        if best_result is None:
            # Fallback: run with empty params
            best_result = self._run_single(train_data, {}, config)
            best_score = calculate_robustness_score(
                stats_from_backtest_result(best_result)
            )

        return best_params, best_result, best_score

    # ------------------------------------------------------------------
    # Single simulation run
    # ------------------------------------------------------------------

    def _run_single(
        self,
        data: dict[str, dict[str, list[dict[str, Any]]]],
        params: dict[str, Any],
        config: WalkForwardConfig,
    ) -> IBBacktestResult:
        """Run a single simulation with given params on given data.

        Supports both ORB and EMA strategies.
        """
        if config.strategy == "ema_momentum":
            return self._run_ema(data, params, config)
        else:
            return self._run_orb(data, params, config)

    def _run_orb(
        self,
        data: dict[str, dict[str, list[dict[str, Any]]]],
        params: dict[str, Any],
        config: WalkForwardConfig,
    ) -> IBBacktestResult:
        """Run ORB simulator with params."""
        cfg = IBBacktestConfig(
            symbols=config.symbols,
            account_size=config.account_size,
        )
        # Apply param overrides
        for key, value in params.items():
            if hasattr(cfg, key):
                object.__setattr__(cfg, key, value)

        sim = ORBSimulator(cfg)
        sim.run(data, detect_opening_range)

        return IBBacktestResult(
            label=f"ORB {params}",
            trades=sim.trades,
            equity_curve=sim.equity_curve,
            daily_results=sim.daily_results,
            initial_equity=config.account_size,
        )

    def _run_ema(
        self,
        data: dict[str, dict[str, list[dict[str, Any]]]],
        params: dict[str, Any],
        config: WalkForwardConfig,
    ) -> IBBacktestResult:
        """Run EMA simulator with params."""
        cfg = EMAStrategyConfig(
            symbols=config.symbols,
            account_size=config.account_size,
        )
        # Apply param overrides
        for key, value in params.items():
            if hasattr(cfg, key):
                object.__setattr__(cfg, key, value)

        sim = EMASimulator(cfg)
        sim.run(data)

        return IBBacktestResult(
            label=f"EMA {params}",
            trades=sim.trades,
            equity_curve=sim.equity_curve,
            daily_results=sim.daily_results,
            initial_equity=config.account_size,
        )

    # ------------------------------------------------------------------
    # Results persistence
    # ------------------------------------------------------------------

    def _save_results(self, result: WalkForwardResult) -> None:
        """Save walk-forward results to CSV and JSON."""
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)

        timestamp = date.today().isoformat()
        strategy = result.config.strategy
        base_name = f"wf_{strategy}_{timestamp}"

        # --- JSON ---
        json_path = RESULTS_DIR / f"{base_name}.json"
        json_data = {
            "config": {
                "total_days": result.config.total_days,
                "train_days": result.config.train_days,
                "validate_days": result.config.validate_days,
                "step_days": result.config.step_days,
                "symbols": result.config.symbols,
                "strategy": result.config.strategy,
                "account_size": result.config.account_size,
            },
            "summary": {
                "total_oos_pnl": result.total_oos_pnl,
                "total_is_pnl": result.total_is_pnl,
                "total_oos_trades": result.total_oos_trades,
                "oos_profit_factor": result.oos_profit_factor,
                "consistency_score": result.consistency_score,
                "degradation_ratio": result.degradation_ratio,
                "avg_robustness": result.avg_robustness,
                "windows_profitable": result.windows_profitable,
                "total_windows": len(result.windows),
            },
            "windows": [asdict(w) for w in result.windows],
        }
        with open(json_path, "w") as f:
            json.dump(json_data, f, indent=2, default=str)
        logger.info("Walk-forward JSON saved: %s", json_path)

        # --- CSV ---
        csv_path = RESULTS_DIR / f"{base_name}.csv"
        if result.windows:
            fieldnames = list(asdict(result.windows[0]).keys())
            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for w in result.windows:
                    row = asdict(w)
                    # Convert dict to string for CSV
                    row["best_params"] = json.dumps(row["best_params"])
                    writer.writerow(row)
            logger.info("Walk-forward CSV saved: %s", csv_path)


# =========================================================================
# Parameter grid expansion
# =========================================================================

def _generate_param_combos(
    grid: dict[str, list[Any]],
) -> list[dict[str, Any]]:
    """Generate all combinations from a parameter grid.

    Example:
        {"a": [1, 2], "b": [3, 4]}
        -> [{"a": 1, "b": 3}, {"a": 1, "b": 4},
            {"a": 2, "b": 3}, {"a": 2, "b": 4}]
    """
    if not grid:
        return [{}]

    keys = list(grid.keys())
    values = list(grid.values())

    combos: list[dict[str, Any]] = [{}]
    for key, vals in zip(keys, values):
        new_combos: list[dict[str, Any]] = []
        for combo in combos:
            for val in vals:
                new_combo = {**combo, key: val}
                new_combos.append(new_combo)
        combos = new_combos

    return combos


# =========================================================================
# Printing / reporting
# =========================================================================

def print_walk_forward_summary(result: WalkForwardResult) -> None:
    """Print formatted walk-forward validation summary."""
    print()
    print("=" * 80)
    print("  WALK-FORWARD VALIDATION RESULTS")
    print("=" * 80)
    print()
    print(f"  Strategy:        {result.config.strategy}")
    print(f"  Symbols:         {', '.join(result.config.symbols)}")
    print(f"  Windows:         {len(result.windows)} "
          f"(train={result.config.train_days}d, "
          f"validate={result.config.validate_days}d, "
          f"step={result.config.step_days}d)")
    print()

    # --- Aggregate OOS metrics ---
    print("  --- Out-of-Sample Aggregate ---")
    print(f"  Total OOS P&L:       ${result.total_oos_pnl:+,.2f}")
    print(f"  Total OOS Trades:    {result.total_oos_trades}")
    print(f"  OOS Profit Factor:   {result.oos_profit_factor:.2f}")
    print(f"  Consistency:         {result.windows_profitable}/{len(result.windows)} "
          f"windows profitable ({result.consistency_score:.0f}%)")
    print(f"  Degradation Ratio:   {result.degradation_ratio:.2f} "
          f"(OOS/IS, 1.0 = perfect)")
    print(f"  Avg Robustness:      {result.avg_robustness:.1f}/100")
    print()

    # --- Per-window table ---
    print("  --- Window Details ---")
    header = (
        f"  {'#':>3s} | {'Train':^21s} | {'Validate':^21s} | "
        f"{'IS P&L':>9s} | {'OOS P&L':>9s} | {'OOS PF':>7s} | "
        f"{'Rob':>5s} | {'Class':<10s}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    for w in result.windows:
        line = (
            f"  {w.window_id:>3d} | "
            f"{w.train_start}~{w.train_end} | "
            f"{w.validate_start}~{w.validate_end} | "
            f"${w.in_sample_pnl:>+8,.2f} | "
            f"${w.out_sample_pnl:>+8,.2f} | "
            f"{w.out_sample_pf:>7.2f} | "
            f"{w.robustness_score:>5.1f} | "
            f"{w.robustness_class:<10s}"
        )
        print(line)

    print()

    # --- Verdict ---
    if result.consistency_score >= 60 and result.degradation_ratio >= 0.3:
        verdict = "PASS - Strategy shows reasonable OOS generalization"
    elif result.consistency_score >= 40:
        verdict = "MARGINAL - Some OOS degradation, use caution"
    else:
        verdict = "FAIL - Heavy overfitting detected, do NOT deploy"

    print(f"  VERDICT: {verdict}")
    print()
    print("=" * 80)
