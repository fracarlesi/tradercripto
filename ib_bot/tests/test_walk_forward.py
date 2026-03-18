"""Tests for walk-forward validation engine."""

import pytest
from typing import Any

from ib_bot.backtesting.walk_forward import (
    WalkForwardConfig,
    WalkForwardEngine,
    WalkForwardResult,
    _generate_param_combos,
)


def test_param_combo_generation() -> None:
    """Generate all combinations from a parameter grid."""
    grid = {"a": [1, 2], "b": [3, 4]}
    combos = _generate_param_combos(grid)
    assert len(combos) == 4
    assert {"a": 1, "b": 3} in combos
    assert {"a": 2, "b": 4} in combos


def test_param_combo_empty() -> None:
    """Empty grid returns single empty dict."""
    combos = _generate_param_combos({})
    assert combos == [{}]


def test_param_combo_single() -> None:
    """Single param with 3 values returns 3 combos."""
    grid = {"x": [10, 20, 30]}
    combos = _generate_param_combos(grid)
    assert len(combos) == 3


def test_window_building() -> None:
    """Windows don't overlap train/validate periods."""
    engine = WalkForwardEngine()
    # 50 trading dates
    all_dates = [f"2026-01-{i+1:02d}" for i in range(50)]

    config = WalkForwardConfig(
        train_days=20,
        validate_days=10,
        step_days=5,
    )
    windows = engine._build_windows(all_dates, config)

    assert len(windows) > 0

    for train_dates, val_dates in windows:
        assert len(train_dates) == 20
        assert len(val_dates) == 10
        # No overlap between train and validate
        train_set = set(train_dates)
        val_set = set(val_dates)
        assert train_set.isdisjoint(val_set)


def test_no_data_raises() -> None:
    """Engine raises if run() called without set_data()."""
    engine = WalkForwardEngine()
    config = WalkForwardConfig()
    with pytest.raises(ValueError, match="No data"):
        engine.run(config)


def test_wf_result_properties() -> None:
    """WalkForwardResult computes aggregate properties correctly."""
    from ib_bot.backtesting.walk_forward import WindowResult

    w1 = WindowResult(
        window_id=1,
        train_start="2026-01-01", train_end="2026-01-30",
        validate_start="2026-01-31", validate_end="2026-02-09",
        best_params={},
        in_sample_pnl=100.0, in_sample_trades=20,
        in_sample_pf=1.5, in_sample_sharpe=1.0,
        out_sample_pnl=50.0, out_sample_trades=10,
        out_sample_pf=1.2, out_sample_sharpe=0.8,
        robustness_score=65.0, robustness_class="robust",
    )
    w2 = WindowResult(
        window_id=2,
        train_start="2026-02-01", train_end="2026-02-28",
        validate_start="2026-03-01", validate_end="2026-03-10",
        best_params={},
        in_sample_pnl=80.0, in_sample_trades=15,
        in_sample_pf=1.3, in_sample_sharpe=0.9,
        out_sample_pnl=-20.0, out_sample_trades=8,
        out_sample_pf=0.8, out_sample_sharpe=-0.2,
        robustness_score=45.0, robustness_class="acceptable",
    )

    result = WalkForwardResult(config=WalkForwardConfig(), windows=[w1, w2])

    assert result.total_oos_pnl == 30.0
    assert result.total_oos_trades == 18
    assert result.windows_profitable == 1
    assert result.consistency_score == 50.0
    assert result.total_is_pnl == 180.0
    assert abs(result.degradation_ratio - 30.0 / 180.0) < 0.01
