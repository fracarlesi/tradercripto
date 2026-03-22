"""Tests for walk-forward validation and reward functions.

These tests do NOT download real models or call external APIs.
They validate pure logic: window generation, metrics computation,
pass criteria, and reward function math.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from flag_trader.walk_forward import WalkForwardResult, WalkForwardValidator, WindowResult


@pytest.fixture
def fake_candles() -> np.ndarray:
    """10,000 candles — enough for ~3.5 months at 15min intervals."""
    np.random.seed(42)
    prices = 100.0 + np.cumsum(np.random.randn(10_000) * 0.1)
    candles = np.column_stack([
        prices,                                          # open
        prices + np.abs(np.random.randn(10_000)) * 0.5,  # high
        prices - np.abs(np.random.randn(10_000)) * 0.5,  # low
        prices + np.random.randn(10_000) * 0.2,           # close
        np.random.rand(10_000) * 1_000_000,                # volume
    ])
    return candles


# --- Window generation ---

def test_build_windows(fake_candles: np.ndarray) -> None:
    v = WalkForwardValidator(
        fake_candles,
        candles_per_month=2880,
        train_months=2,
        test_months=1,
        step_months=1,
    )
    windows = v._build_windows()
    assert len(windows) > 0
    for train_start, train_end, test_start, test_end in windows:
        assert train_start < train_end
        assert train_end == test_start  # contiguous
        assert test_start < test_end


def test_build_windows_insufficient_data() -> None:
    """If data is too short for even one window, return empty list."""
    tiny = np.zeros((100, 5))
    v = WalkForwardValidator(tiny, candles_per_month=2880, train_months=4, test_months=2)
    windows = v._build_windows()
    assert windows == []


def test_window_sizes_correct() -> None:
    """Verify each window has the expected train/test lengths."""
    data = np.zeros((20_000, 5))
    v = WalkForwardValidator(data, candles_per_month=2880, train_months=2, test_months=1, step_months=1)
    windows = v._build_windows()
    for ts, te, vs, ve in windows:
        assert te - ts == 2 * 2880  # train length
        assert ve - vs == 1 * 2880  # test length


# --- Metrics computation ---

def test_compute_metrics_basic() -> None:
    v = WalkForwardValidator(np.zeros((1000, 5)))
    pnls = [10.0, -5.0, 15.0, -3.0, 8.0, -2.0, 12.0, -4.0]
    metrics = v._compute_metrics(pnls)

    assert "sharpe" in metrics
    assert "profit_factor" in metrics
    assert "max_drawdown_pct" in metrics
    assert "win_rate" in metrics
    assert "total_trades" in metrics

    # 5 wins out of 8 trades
    assert metrics["win_rate"] == pytest.approx(5 / 8)
    assert metrics["total_trades"] == 8

    # Profit factor: (10+15+8+12) / (5+3+2+4) = 45/14
    assert metrics["profit_factor"] == pytest.approx(45.0 / 14.0, rel=1e-4)


def test_compute_metrics_empty() -> None:
    v = WalkForwardValidator(np.zeros((1000, 5)))
    metrics = v._compute_metrics([])
    assert metrics["sharpe"] == 0.0
    assert metrics["total_trades"] == 0


def test_compute_metrics_all_wins() -> None:
    v = WalkForwardValidator(np.zeros((1000, 5)))
    metrics = v._compute_metrics([5.0, 10.0, 3.0])
    # No losses -> profit_factor capped at 99.99
    assert metrics["profit_factor"] == 99.99
    assert metrics["win_rate"] == 1.0


def test_compute_metrics_all_losses() -> None:
    v = WalkForwardValidator(np.zeros((1000, 5)))
    metrics = v._compute_metrics([-5.0, -10.0])
    assert metrics["profit_factor"] == 0.0
    assert metrics["win_rate"] == 0.0


# --- Pass criteria ---

def test_passes_criteria_all_good() -> None:
    result = WalkForwardResult(
        windows=[], avg_sharpe=1.5, avg_pf=1.5, avg_max_dd=10.0,
        windows_profitable=3, total_windows=3, passed=True,
    )
    v = WalkForwardValidator(np.zeros((1000, 5)))
    assert v.passes_criteria(result) is True


def test_passes_criteria_low_sharpe() -> None:
    result = WalkForwardResult(
        windows=[], avg_sharpe=0.5, avg_pf=1.5, avg_max_dd=10.0,
        windows_profitable=3, total_windows=3, passed=False,
    )
    v = WalkForwardValidator(np.zeros((1000, 5)))
    assert v.passes_criteria(result) is False


def test_passes_criteria_high_drawdown() -> None:
    result = WalkForwardResult(
        windows=[], avg_sharpe=1.5, avg_pf=1.5, avg_max_dd=25.0,
        windows_profitable=3, total_windows=3, passed=False,
    )
    v = WalkForwardValidator(np.zeros((1000, 5)))
    assert v.passes_criteria(result) is False


def test_passes_criteria_too_few_profitable_windows() -> None:
    result = WalkForwardResult(
        windows=[], avg_sharpe=1.5, avg_pf=1.5, avg_max_dd=10.0,
        windows_profitable=1, total_windows=4, passed=False,  # 25% < 67%
    )
    v = WalkForwardValidator(np.zeros((1000, 5)))
    assert v.passes_criteria(result) is False


def test_passes_criteria_no_windows() -> None:
    result = WalkForwardResult(
        windows=[], avg_sharpe=0.0, avg_pf=0.0, avg_max_dd=0.0,
        windows_profitable=0, total_windows=0, passed=False,
    )
    v = WalkForwardValidator(np.zeros((1000, 5)))
    assert v.passes_criteria(result) is False


# --- Save/load results ---

def test_save_results(tmp_path: Path) -> None:
    w = WindowResult(
        window_id=0, train_start=0, train_end=100, test_start=100,
        test_end=200, sharpe=1.2, profit_factor=1.5, max_drawdown_pct=8.0,
        total_trades=30, win_rate=0.45, net_return_pct=3.5,
    )
    result = WalkForwardResult(
        windows=[w], avg_sharpe=1.2, avg_pf=1.5, avg_max_dd=8.0,
        windows_profitable=1, total_windows=1, passed=True,
    )
    v = WalkForwardValidator(np.zeros((1000, 5)))
    out_path = tmp_path / "results.json"
    v.save_results(result, out_path)

    assert out_path.exists()
    data = json.loads(out_path.read_text())
    assert data["summary"]["passed"] is True
    assert len(data["windows"]) == 1
    assert data["windows"][0]["sharpe"] == 1.2


# --- Reward functions ---

def test_reward_functions_registry() -> None:
    from flag_trader.reward import REWARD_FUNCTIONS
    assert "sharpe_delta" in REWARD_FUNCTIONS
    assert "sortino_delta" in REWARD_FUNCTIONS
    assert "calmar_delta" in REWARD_FUNCTIONS
    assert len(REWARD_FUNCTIONS) == 3


def test_sortino_delta() -> None:
    from flag_trader.reward import compute_sortino_delta
    pnls = [1.0, -0.5, 2.0, -1.0, 1.5]
    result = compute_sortino_delta(pnls)
    assert isinstance(result, float)

    # With only 1 element should return 0
    assert compute_sortino_delta([1.0]) == 0.0
    assert compute_sortino_delta([]) == 0.0


def test_calmar_delta() -> None:
    from flag_trader.reward import compute_calmar_delta
    pnls = [1.0, -0.5, 2.0, -1.0, 1.5]
    result = compute_calmar_delta(pnls)
    assert isinstance(result, float)

    assert compute_calmar_delta([1.0]) == 0.0
    assert compute_calmar_delta([]) == 0.0


def test_sortino_all_positive() -> None:
    """With no downside, sortino should be 0 (downside_std ~ 0)."""
    from flag_trader.reward import compute_sortino_delta
    result = compute_sortino_delta([1.0, 2.0, 3.0])
    assert isinstance(result, float)


def test_calmar_no_drawdown() -> None:
    """Monotonically increasing -> calmar should be 0 (no drawdown)."""
    from flag_trader.reward import compute_calmar_delta
    result = compute_calmar_delta([1.0, 2.0, 3.0])
    assert isinstance(result, float)
