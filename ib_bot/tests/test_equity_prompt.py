"""Tests for EquityPromptBuilder — prompt building and action parsing."""

import json
import pytest

from ib_bot.flag_trader.equity_prompt import EquityPromptBuilder


@pytest.fixture
def builder() -> EquityPromptBuilder:
    return EquityPromptBuilder(candle_window=5, decimal_places=4)


@pytest.fixture
def sample_candles() -> list[dict[str, float]]:
    """5 daily candles for AAPL-like stock at ~$150."""
    return [
        {"open": 148.0, "high": 150.5, "low": 147.5, "close": 150.0, "volume": 50_000_000},
        {"open": 150.0, "high": 152.0, "low": 149.0, "close": 151.0, "volume": 48_000_000},
        {"open": 151.0, "high": 153.0, "low": 150.0, "close": 152.5, "volume": 52_000_000},
        {"open": 152.5, "high": 154.0, "low": 151.0, "close": 153.0, "volume": 55_000_000},
        {"open": 153.0, "high": 155.0, "low": 152.0, "close": 154.0, "volume": 60_000_000},
    ]


@pytest.fixture
def sample_portfolio() -> dict[str, float]:
    return {
        "cash_balance": 100000.0,
        "asset_position": 0.0,
        "total_account_value": 100000.0,
    }


@pytest.fixture
def sample_history() -> dict:
    return {
        "recent_rewards": [0.01, -0.005, 0.02],
        "net_values": [100000, 100100, 100050],
        "actions": ["HOLD", "BUY", "HOLD"],
    }


class TestBuildPrompt:
    """Tests for EquityPromptBuilder.build_prompt()."""

    def test_contains_equity_preambolo(
        self, builder: EquityPromptBuilder, sample_candles, sample_portfolio, sample_history
    ):
        prompt = builder.build_prompt(sample_candles, sample_portfolio, sample_history)
        assert "US equity trading agent" in prompt
        assert "cryptocurrency" not in prompt

    def test_contains_ibkr_costs(
        self, builder: EquityPromptBuilder, sample_candles, sample_portfolio, sample_history
    ):
        prompt = builder.build_prompt(sample_candles, sample_portfolio, sample_history)
        assert "$0.005 per share" in prompt
        assert "IBKR" in prompt

    def test_contains_action_choices(
        self, builder: EquityPromptBuilder, sample_candles, sample_portfolio, sample_history
    ):
        prompt = builder.build_prompt(sample_candles, sample_portfolio, sample_history)
        assert "{Buy, Sell, Hold}" in prompt

    def test_contains_normalized_prices(
        self, builder: EquityPromptBuilder, sample_candles, sample_portfolio, sample_history
    ):
        prompt = builder.build_prompt(sample_candles, sample_portfolio, sample_history)
        # First candle close is base, so close should be 0.0
        assert '"close": 0.0' in prompt

    def test_candle_normalization(self, builder: EquityPromptBuilder):
        """Prices should be normalized as % change from first close."""
        candles = [
            {"open": 100.0, "high": 105.0, "low": 95.0, "close": 100.0, "volume": 1000},
            {"open": 102.0, "high": 108.0, "low": 99.0, "close": 105.0, "volume": 1200},
        ]
        normalized = builder._normalize_candles(candles)
        assert len(normalized) == 2
        # First candle: close should be 0.0 (base)
        assert normalized[0]["close"] == 0.0
        # Second candle: close = (105/100 - 1) = 0.05
        assert normalized[1]["close"] == 0.05
        # Second candle: high = (108/100 - 1) = 0.08
        assert normalized[1]["high"] == 0.08

    def test_empty_candles_normalization(self, builder: EquityPromptBuilder):
        assert builder._normalize_candles([]) == []

    def test_sector_included(
        self, builder: EquityPromptBuilder, sample_candles, sample_portfolio, sample_history
    ):
        prompt = builder.build_prompt(
            sample_candles, sample_portfolio, sample_history, sector="Technology"
        )
        assert "Sector: Technology" in prompt

    def test_sector_not_included_when_none(
        self, builder: EquityPromptBuilder, sample_candles, sample_portfolio, sample_history
    ):
        prompt = builder.build_prompt(sample_candles, sample_portfolio, sample_history)
        assert "Sector:" not in prompt

    def test_market_context_included(
        self, builder: EquityPromptBuilder, sample_candles, sample_portfolio, sample_history
    ):
        ctx = [{"symbol": "SPY", "pct_1d": 0.5, "pct_5d": -1.2, "trend": "bullish"}]
        prompt = builder.build_prompt(
            sample_candles, sample_portfolio, sample_history, market_context=ctx
        )
        assert "SPY:" in prompt
        assert "1d: +0.5%" in prompt
        assert "trend: bullish" in prompt

    def test_position_info_included(
        self, builder: EquityPromptBuilder, sample_candles, sample_portfolio, sample_history
    ):
        pos_info = {
            "direction": "long",
            "symbol": "AAPL",
            "entry_price": 150.0,
            "pnl_pct": 2.5,
        }
        prompt = builder.build_prompt(
            sample_candles, sample_portfolio, sample_history, position_info=pos_info
        )
        assert "LONG AAPL" in prompt
        assert "$150.00" in prompt
        assert "+2.5%" in prompt

    def test_similar_trades_text(
        self, builder: EquityPromptBuilder, sample_candles, sample_portfolio, sample_history
    ):
        prompt = builder.build_prompt(
            sample_candles, sample_portfolio, sample_history,
            similar_trades_text="Similar trade: AAPL BUY +3.2%",
        )
        assert "Similar trade: AAPL BUY +3.2%" in prompt

    def test_output_action_format(
        self, builder: EquityPromptBuilder, sample_candles, sample_portfolio, sample_history
    ):
        prompt = builder.build_prompt(sample_candles, sample_portfolio, sample_history)
        assert '{"Action": "Buy"}' in prompt

    def test_trims_to_candle_window(self, builder: EquityPromptBuilder, sample_portfolio, sample_history):
        """Builder should trim candles to candle_window (5)."""
        candles = [
            {"open": 100 + i, "high": 102 + i, "low": 99 + i, "close": 101 + i, "volume": 1000}
            for i in range(10)
        ]
        prompt = builder.build_prompt(candles, sample_portfolio, sample_history)
        # Parse the JSON state from the prompt
        state_start = prompt.index("Current State:\n") + len("Current State:\n")
        state_end = prompt.index("\n\nOutput Action:")
        state = json.loads(prompt[state_start:state_end])
        assert len(state["historical_prices"]) == 5  # trimmed to window

    def test_portfolio_in_prompt(
        self, builder: EquityPromptBuilder, sample_candles, sample_portfolio, sample_history
    ):
        prompt = builder.build_prompt(sample_candles, sample_portfolio, sample_history)
        assert "100000.0" in prompt  # cash_balance and total_account_value


class TestParseAction:
    """Tests for EquityPromptBuilder.parse_action()."""

    def test_json_format(self, builder: EquityPromptBuilder):
        assert builder.parse_action('{"Action": "Buy"}') == 2
        assert builder.parse_action('{"Action": "Sell"}') == 0
        assert builder.parse_action('{"Action": "Hold"}') == 1

    def test_case_insensitive(self, builder: EquityPromptBuilder):
        assert builder.parse_action('{"Action": "buy"}') == 2
        assert builder.parse_action('{"Action": "SELL"}') == 0
        assert builder.parse_action('{"action": "hold"}') == 1

    def test_bare_keyword(self, builder: EquityPromptBuilder):
        assert builder.parse_action("Buy") == 2
        assert builder.parse_action("sell") == 0
        assert builder.parse_action("HOLD") == 1

    def test_keyword_in_text(self, builder: EquityPromptBuilder):
        assert builder.parse_action("I think we should buy AAPL") == 2
        assert builder.parse_action("Time to sell the position") == 0

    def test_action_colon_format(self, builder: EquityPromptBuilder):
        assert builder.parse_action("Action: Buy") == 2
        assert builder.parse_action('Action: "Sell"') == 0

    def test_qwen_thinking_block(self, builder: EquityPromptBuilder):
        text = '<think>Analyzing the market...</think> {"Action": "Buy"}'
        assert builder.parse_action(text) == 2

    def test_default_to_hold(self, builder: EquityPromptBuilder):
        assert builder.parse_action("gibberish") == 1
        assert builder.parse_action("") == 1

    def test_json_embedded_in_text(self, builder: EquityPromptBuilder):
        text = 'Based on analysis, my decision is {"Action": "Sell"}'
        assert builder.parse_action(text) == 0
