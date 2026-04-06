"""Tests for flag_trader.environment — HyperliquidTradingEnv."""

import numpy as np
import pytest

from flag_trader.environment import HyperliquidTradingEnv


@pytest.fixture
def fake_candles() -> np.ndarray:
    """Generate 100 realistic OHLCV candles with random walk prices."""
    rng = np.random.default_rng(42)
    n = 100
    prices = np.empty(n)
    prices[0] = 100.0
    for i in range(1, n):
        prices[i] = prices[i - 1] * (1 + rng.normal(0, 0.005))

    rows = []
    for p in prices:
        o = p * (1 + rng.uniform(-0.002, 0.002))
        h = max(o, p) * (1 + rng.uniform(0, 0.003))
        low = min(o, p) * (1 - rng.uniform(0, 0.003))
        c = p
        v = rng.uniform(1e5, 1e7)
        rows.append([o, h, low, c, v])
    return np.array(rows, dtype=np.float32)


@pytest.fixture
def env(fake_candles: np.ndarray) -> HyperliquidTradingEnv:
    return HyperliquidTradingEnv(fake_candles, initial_cash=1000.0, window_size=20)


def test_env_creation(env: HyperliquidTradingEnv) -> None:
    assert env.action_space.n == 3  # pyright: ignore[reportAttributeAccessIssue]  # test fixture
    assert "candles" in env.observation_space.spaces  # pyright: ignore[reportAttributeAccessIssue]  # test fixture
    assert "portfolio" in env.observation_space.spaces  # pyright: ignore[reportAttributeAccessIssue]  # test fixture
    assert "history" in env.observation_space.spaces  # pyright: ignore[reportAttributeAccessIssue]  # test fixture


def test_reset(env: HyperliquidTradingEnv) -> None:
    obs, info = env.reset()
    assert obs["candles"].shape == (20, 8)
    assert obs["portfolio"].shape == (4,)
    assert obs["history"].shape == (10,)
    assert info["total_value"] == 1000.0


def test_step_buy(env: HyperliquidTradingEnv) -> None:
    env.reset()
    obs, reward, terminated, truncated, info = env.step(2)  # Buy
    assert info["cash"] == 0.0
    assert info["position"] > 0


def test_step_sell_after_buy(env: HyperliquidTradingEnv) -> None:
    env.reset()
    env.step(2)  # Buy
    obs, reward, terminated, truncated, info = env.step(0)  # Sell
    assert info["position"] == 0.0
    assert info["cash"] > 0.0


def test_step_hold(env: HyperliquidTradingEnv) -> None:
    env.reset()
    _, _, _, _, info_before = env.step(1)  # Hold
    cash_before = info_before["cash"]
    pos_before = info_before["position"]
    _, _, _, _, info_after = env.step(1)  # Hold again
    assert info_after["cash"] == cash_before
    assert info_after["position"] == pos_before


def test_action_masking_no_sell(env: HyperliquidTradingEnv) -> None:
    env.reset()
    _, _, _, _, info = env.step(0)  # Sell with no position
    assert info["executed_action"] == 1  # Treated as Hold
    assert info["position"] == 0.0


def test_action_masking_no_buy(env: HyperliquidTradingEnv) -> None:
    env.reset()
    env.step(2)  # Buy (spend all cash)
    _, _, _, _, info = env.step(2)  # Buy again with no cash
    assert info["executed_action"] == 1  # Treated as Hold


def test_episode_terminates(fake_candles: np.ndarray) -> None:
    env = HyperliquidTradingEnv(fake_candles, window_size=20)
    env.reset()
    terminated = False
    steps = 0
    while not terminated:
        _, _, terminated, _, _ = env.step(1)
        steps += 1
    # Should terminate after all candles consumed (100 - 20 = 80 steps)
    assert steps == 80


def test_reward_is_float(env: HyperliquidTradingEnv) -> None:
    env.reset()
    _, reward, _, _, _ = env.step(1)
    assert isinstance(reward, float)


def test_info_contains_pnl(env: HyperliquidTradingEnv) -> None:
    env.reset()
    _, _, _, _, info = env.step(2)  # Buy
    assert "total_value" in info
    assert "unrealized_pnl" in info
    assert "mtm_pnl" in info
