import json

import numpy as np
import pytest
from pathlib import Path

from flag_trader.autoresearch import AutoResearcher, ExperimentResult, ResearchState


@pytest.fixture
def fake_candles() -> np.ndarray:
    np.random.seed(42)
    prices = 100 + np.cumsum(np.random.randn(10000) * 0.1)
    return np.column_stack([
        prices,
        prices + abs(np.random.randn(10000)) * 0.5,
        prices - abs(np.random.randn(10000)) * 0.5,
        prices + np.random.randn(10000) * 0.2,
        np.random.rand(10000) * 1000000,
    ])


def test_generate_experiment_queue(fake_candles: np.ndarray) -> None:
    ar = AutoResearcher(fake_candles)
    queue = ar._generate_experiment_queue()
    assert len(queue) > 0
    assert all("param" in e and "value" in e for e in queue)


def test_experiment_grid_coverage(fake_candles: np.ndarray) -> None:
    ar = AutoResearcher(fake_candles)
    queue = ar._generate_experiment_queue()
    params = {e["param"] for e in queue}
    assert "lr" in params
    assert "reward_fn" in params
    assert "freeze_pct" in params
    assert "window_size" in params


def test_default_config(fake_candles: np.ndarray) -> None:
    ar = AutoResearcher(fake_candles)
    assert ar.config["lr"] == 1e-5
    assert ar.config["freeze_pct"] == 0.8
    assert ar.config["reward_fn"] == "sharpe_delta"


def test_custom_baseline_config(fake_candles: np.ndarray) -> None:
    ar = AutoResearcher(fake_candles, baseline_config={"lr": 5e-5})
    assert ar.config["lr"] == 5e-5


def test_save_load_state(tmp_path: Path, fake_candles: np.ndarray) -> None:
    results_file = tmp_path / "test_experiments.json"
    ar = AutoResearcher(fake_candles, results_file=results_file)

    ar.state.best_sharpe = 1.5
    ar.state.best_config["lr"] = 5e-5
    ar.state.experiments_completed.append(ExperimentResult(
        experiment_id=1, parameter="lr", value=5e-5,
        baseline_sharpe=1.0, result_sharpe=1.5,
        improvement=0.5, improvement_pct=50.0,
        kept=True, walk_forward_passed=True, duration_seconds=60.0,
    ))
    ar._save_state()

    assert results_file.exists()
    data = json.loads(results_file.read_text())
    assert data["best_sharpe"] == 1.5
    assert len(data["experiments"]) == 1

    # Load in new instance
    ar2 = AutoResearcher(fake_candles, results_file=results_file)
    assert ar2.state.best_sharpe == 1.5
    assert len(ar2.state.experiments_completed) == 1


def test_get_summary(fake_candles: np.ndarray) -> None:
    ar = AutoResearcher(fake_candles)
    ar.state.best_sharpe = 1.5
    summary = ar.get_summary()
    assert "Autoresearch Summary" in summary
    assert "1.5" in summary


def test_skip_completed_experiments(fake_candles: np.ndarray, tmp_path: Path) -> None:
    results_file = tmp_path / "experiments.json"
    ar = AutoResearcher(fake_candles, results_file=results_file)

    # Simulate completed experiment
    ar.state.experiments_completed.append(ExperimentResult(
        experiment_id=1, parameter="lr", value=5e-5,
        baseline_sharpe=1.0, result_sharpe=0.8,
        improvement=-0.2, improvement_pct=-20.0,
        kept=False, walk_forward_passed=False, duration_seconds=30.0,
    ))
    ar._save_state()

    # New instance should skip completed
    ar2 = AutoResearcher(fake_candles, results_file=results_file)
    queue = ar2._generate_experiment_queue()
    completed_keys = {
        (e.parameter, str(e.value)) for e in ar2.state.experiments_completed
    }
    remaining = [
        e for e in queue if (e["param"], str(e["value"])) not in completed_keys
    ]
    assert len(remaining) < len(queue)
