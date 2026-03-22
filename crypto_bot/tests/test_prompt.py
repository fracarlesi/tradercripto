"""Tests for flag_trader.prompt — PromptBuilder."""

import pytest

from flag_trader.prompt import PromptBuilder


@pytest.fixture
def builder() -> PromptBuilder:
    return PromptBuilder(candle_window=5)


@pytest.fixture
def sample_candles() -> list[dict[str, float]]:
    return [
        {"open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 5000.0},
        {"open": 101.0, "high": 103.0, "low": 100.0, "close": 102.0, "volume": 6000.0},
        {"open": 102.0, "high": 104.0, "low": 101.0, "close": 103.0, "volume": 5500.0},
    ]


@pytest.fixture
def sample_portfolio() -> dict[str, float]:
    return {"cash_balance": 500.0, "asset_position": 0.5, "total_account_value": 1000.0}


@pytest.fixture
def sample_history() -> dict[str, list]:
    return {"recent_rewards": [0.1, -0.05], "net_values": [1000.0, 1010.0], "actions": ["Buy", "Hold"]}


def test_prompt_contains_sections(
    builder: PromptBuilder, sample_candles, sample_portfolio, sample_history
) -> None:
    prompt = builder.build_prompt(sample_candles, sample_portfolio, sample_history)
    assert "Task:" in prompt
    assert "Legible Actions:" in prompt
    assert "Current State:" in prompt
    assert "Output Action:" in prompt


def test_prompt_deterministic(
    builder: PromptBuilder, sample_candles, sample_portfolio, sample_history
) -> None:
    p1 = builder.build_prompt(sample_candles, sample_portfolio, sample_history)
    p2 = builder.build_prompt(sample_candles, sample_portfolio, sample_history)
    assert p1 == p2


def test_parse_action_json(builder: PromptBuilder) -> None:
    assert builder.parse_action('{"Action": "Buy"}') == 2


def test_parse_action_bare(builder: PromptBuilder) -> None:
    assert builder.parse_action("Sell") == 0


def test_parse_action_fallback(builder: PromptBuilder) -> None:
    assert builder.parse_action("nonsense xyz 123") == 1  # Hold


def test_price_normalization(builder: PromptBuilder, sample_candles) -> None:
    normalized = builder._normalize_candles(sample_candles)
    # First candle close = 101.0, so first normalized close = 0.0
    assert normalized[0]["close"] == 0.0
    # Second candle close = 102.0, pct change ~= 0.0099
    assert abs(normalized[1]["close"] - (102.0 / 101.0 - 1.0)) < 1e-3
