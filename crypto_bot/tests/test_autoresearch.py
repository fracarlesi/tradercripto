import json

import numpy as np
import pytest
from pathlib import Path

from flag_trader.autoresearch import AutoResearcher, ExperimentResult, ResearchState


@pytest.fixture
def fake_candles_by_symbol() -> dict[str, list[dict]]:
    """Generate fake candles_by_symbol dict with enough data for train/test split."""
    np.random.seed(42)
    n = 2000  # Need > OOS_BARS (672) + 50
    prices = 100 + np.cumsum(np.random.randn(n) * 0.1)
    candles = []
    for i in range(n):
        p = prices[i]
        candles.append({
            "open": float(p),
            "high": float(p + abs(np.random.randn()) * 0.5),
            "low": float(p - abs(np.random.randn()) * 0.5),
            "close": float(p + np.random.randn() * 0.2),
            "volume": float(np.random.rand() * 1000000),
        })
    return {"BTC": candles}


def test_generate_experiment_queue(fake_candles_by_symbol: dict) -> None:
    ar = AutoResearcher(fake_candles_by_symbol)
    queue = ar._generate_experiment_queue()
    assert len(queue) > 0
    assert all("param" in e and "value" in e for e in queue)


def test_experiment_grid_coverage(fake_candles_by_symbol: dict) -> None:
    ar = AutoResearcher(fake_candles_by_symbol)
    queue = ar._generate_experiment_queue()
    params = {e["param"] for e in queue}
    assert "lr" in params
    assert "reward_fn" in params
    assert "freeze_pct" in params
    assert "window_size" in params


def test_default_config(fake_candles_by_symbol: dict) -> None:
    ar = AutoResearcher(fake_candles_by_symbol)
    assert ar.config["lr"] == 3e-5
    assert ar.config["freeze_pct"] == 0.8
    assert ar.config["reward_fn"] == "enhanced_sharpe"


def test_custom_baseline_config(fake_candles_by_symbol: dict) -> None:
    ar = AutoResearcher(fake_candles_by_symbol, baseline_config={"lr": 5e-5})
    assert ar.config["lr"] == 5e-5


def test_save_load_state(tmp_path: Path, fake_candles_by_symbol: dict) -> None:
    results_file = tmp_path / "test_experiments.json"
    ar = AutoResearcher(fake_candles_by_symbol, results_file=results_file)

    ar.state.best_score = 1.5
    ar.state.best_config["lr"] = 5e-5
    ar.state.experiments_completed.append(ExperimentResult(
        experiment_id=1, parameter="lr", value=5e-5,
        baseline_score=1.0, result_score=1.5,
        improvement=0.5, improvement_pct=50.0,
        kept=True, duration_seconds=60.0,
    ))
    ar._save_state()

    assert results_file.exists()
    data = json.loads(results_file.read_text())
    assert data["best_score"] == 1.5
    assert len(data["experiments"]) == 1

    # Load in new instance
    ar2 = AutoResearcher(fake_candles_by_symbol, results_file=results_file)
    assert ar2.state.best_score == 1.5
    assert len(ar2.state.experiments_completed) == 1


def test_get_summary(fake_candles_by_symbol: dict) -> None:
    ar = AutoResearcher(fake_candles_by_symbol)
    ar.state.best_score = 1.5
    summary = ar.get_summary()
    assert "Autoresearch Summary" in summary
    assert "1.5" in summary


def test_skip_completed_experiments(fake_candles_by_symbol: dict, tmp_path: Path) -> None:
    results_file = tmp_path / "experiments.json"
    ar = AutoResearcher(fake_candles_by_symbol, results_file=results_file)

    # Simulate completed experiment
    ar.state.experiments_completed.append(ExperimentResult(
        experiment_id=1, parameter="lr", value=5e-5,
        baseline_score=1.0, result_score=0.8,
        improvement=-0.2, improvement_pct=-20.0,
        kept=False, duration_seconds=30.0,
    ))
    ar._save_state()

    # New instance should skip completed
    ar2 = AutoResearcher(fake_candles_by_symbol, results_file=results_file)
    queue = ar2._generate_experiment_queue()
    completed_keys = {
        (e.parameter, str(e.value)) for e in ar2.state.experiments_completed
    }
    remaining = [
        e for e in queue if (e["param"], str(e["value"])) not in completed_keys
    ]
    assert len(remaining) < len(queue)
