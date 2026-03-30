"""Tests for LLMEquityStrategy — mock model, decision-to-setup conversion."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ib_bot.core.enums import Direction, SetupType
from ib_bot.core.models import TradeSetup
from ib_bot.flag_trader.agent import (
    ExitDecision,
    IBFlagTraderAgent,
    IBFlagTraderConfig,
    TradeDecision,
)
from ib_bot.flag_trader.equity_prompt import EquityPromptBuilder
from ib_bot.strategies.llm_equity import LLMEquityStrategy


@pytest.fixture
def sample_candles() -> dict[str, list[dict[str, float]]]:
    """Candle cache with 20+ candles for two symbols."""

    def make_candles(base: float, count: int = 25) -> list[dict[str, float]]:
        return [
            {
                "open": base + i * 0.5,
                "high": base + i * 0.5 + 1.0,
                "low": base + i * 0.5 - 0.5,
                "close": base + i * 0.5 + 0.3,
                "volume": 1_000_000 + i * 10_000,
            }
            for i in range(count)
        ]

    return {
        "AAPL": make_candles(150.0),
        "MSFT": make_candles(380.0),
        "TSLA": make_candles(200.0),
    }


@pytest.fixture
def mock_model():
    """Mock EquityFlagTraderModel that returns predictable results."""
    model = MagicMock()
    # Default: BUY with high confidence
    # Returns: (action_id, state_value, log_prob, tp_pct, sl_pct)
    import torch

    model.get_action.return_value = (2, 0.85, torch.tensor([-0.3]), 3.5, 1.5)
    return model


@pytest.fixture
def prompt_builder() -> EquityPromptBuilder:
    return EquityPromptBuilder(candle_window=20)


@pytest.fixture
def agent(mock_model, prompt_builder) -> IBFlagTraderAgent:
    config = IBFlagTraderConfig(confidence_threshold=0.5)
    return IBFlagTraderAgent(
        config=config,
        model=mock_model,
        prompt_builder=prompt_builder,
    )


@pytest.fixture
def strategy(agent) -> LLMEquityStrategy:
    return LLMEquityStrategy(agent=agent)


class TestLLMEquityStrategy:
    """Tests for LLMEquityStrategy.evaluate_candidates()."""

    @pytest.mark.asyncio
    async def test_buy_decision_produces_long_setup(
        self, strategy: LLMEquityStrategy, sample_candles
    ):
        candidates = [{"symbol": "AAPL"}]
        setups = await strategy.evaluate_candidates(candidates, sample_candles)
        assert len(setups) == 1
        setup = setups[0]
        assert isinstance(setup, TradeSetup)
        assert setup.symbol == "AAPL"
        assert setup.direction == Direction.LONG
        assert setup.setup_type == SetupType.LLM_EQUITY_LONG
        assert setup.asset_class == "equity"
        assert setup.source == "llm_equity"

    @pytest.mark.asyncio
    async def test_sell_decision_produces_short_setup(
        self, strategy: LLMEquityStrategy, mock_model, sample_candles
    ):
        import torch

        # Override model to return SELL
        mock_model.get_action.return_value = (0, 0.75, torch.tensor([-0.5]), 4.0, 2.0)
        candidates = [{"symbol": "MSFT"}]
        setups = await strategy.evaluate_candidates(candidates, sample_candles)
        assert len(setups) == 1
        setup = setups[0]
        assert setup.direction == Direction.SHORT
        assert setup.setup_type == SetupType.LLM_EQUITY_SHORT

    @pytest.mark.asyncio
    async def test_hold_produces_no_setup(
        self, strategy: LLMEquityStrategy, mock_model, sample_candles
    ):
        import torch

        mock_model.get_action.return_value = (1, 0.5, torch.tensor([-0.1]), 2.0, 1.0)
        candidates = [{"symbol": "AAPL"}]
        setups = await strategy.evaluate_candidates(candidates, sample_candles)
        assert len(setups) == 0

    @pytest.mark.asyncio
    async def test_low_confidence_filtered(
        self, strategy: LLMEquityStrategy, mock_model, sample_candles
    ):
        import torch

        # Confidence 0.3 < threshold 0.5
        mock_model.get_action.return_value = (2, 0.3, torch.tensor([-0.2]), 2.0, 1.0)
        candidates = [{"symbol": "AAPL"}]
        setups = await strategy.evaluate_candidates(candidates, sample_candles)
        assert len(setups) == 0

    @pytest.mark.asyncio
    async def test_insufficient_candles_skipped(
        self, strategy: LLMEquityStrategy, sample_candles
    ):
        # Only 5 candles — below window of 20
        short_cache = {"AAPL": sample_candles["AAPL"][:5]}
        candidates = [{"symbol": "AAPL"}]
        setups = await strategy.evaluate_candidates(candidates, short_cache)
        assert len(setups) == 0

    @pytest.mark.asyncio
    async def test_missing_symbol_skipped(
        self, strategy: LLMEquityStrategy, sample_candles
    ):
        candidates = [{"symbol": "UNKNOWN"}]
        setups = await strategy.evaluate_candidates(candidates, sample_candles)
        assert len(setups) == 0

    @pytest.mark.asyncio
    async def test_multiple_candidates(
        self, strategy: LLMEquityStrategy, sample_candles
    ):
        candidates = [
            {"symbol": "AAPL", "sector": "Technology"},
            {"symbol": "MSFT", "sector": "Technology"},
            {"symbol": "TSLA", "sector": "Consumer Discretionary"},
        ]
        setups = await strategy.evaluate_candidates(candidates, sample_candles)
        assert len(setups) == 3
        symbols = {s.symbol for s in setups}
        assert symbols == {"AAPL", "MSFT", "TSLA"}

    @pytest.mark.asyncio
    async def test_stop_and_target_prices_long(
        self, strategy: LLMEquityStrategy, mock_model, sample_candles
    ):
        """For LONG: stop below entry, target above entry."""
        import torch

        # tp=3.5%, sl=1.5%
        mock_model.get_action.return_value = (2, 0.85, torch.tensor([-0.3]), 3.5, 1.5)
        candidates = [{"symbol": "AAPL"}]
        setups = await strategy.evaluate_candidates(candidates, sample_candles)
        assert len(setups) == 1
        setup = setups[0]
        assert setup.stop_price < setup.entry_price
        assert setup.target_price > setup.entry_price

    @pytest.mark.asyncio
    async def test_stop_and_target_prices_short(
        self, strategy: LLMEquityStrategy, mock_model, sample_candles
    ):
        """For SHORT: stop above entry, target below entry."""
        import torch

        mock_model.get_action.return_value = (0, 0.80, torch.tensor([-0.4]), 4.0, 2.0)
        candidates = [{"symbol": "AAPL"}]
        setups = await strategy.evaluate_candidates(candidates, sample_candles)
        assert len(setups) == 1
        setup = setups[0]
        assert setup.stop_price > setup.entry_price
        assert setup.target_price < setup.entry_price

    @pytest.mark.asyncio
    async def test_confidence_in_valid_range(
        self, strategy: LLMEquityStrategy, sample_candles
    ):
        candidates = [{"symbol": "AAPL"}]
        setups = await strategy.evaluate_candidates(candidates, sample_candles)
        assert len(setups) == 1
        assert Decimal("0") <= setups[0].confidence <= Decimal("1")


class TestIBFlagTraderAgentExit:
    """Tests for IBFlagTraderAgent.evaluate_position()."""

    @pytest.mark.asyncio
    async def test_long_sell_triggers_close(
        self, agent: IBFlagTraderAgent, mock_model, sample_candles
    ):
        import torch

        # Model says SELL on a LONG position
        mock_model.get_action.return_value = (0, 0.7, torch.tensor([-0.3]), 2.0, 1.0)
        exit_decision = await agent.evaluate_position(
            symbol="AAPL",
            direction="long",
            entry_price=150.0,
            pnl_pct=2.0,
            candles=sample_candles["AAPL"],
        )
        assert exit_decision.should_close is True
        assert exit_decision.reason == "model_reversal"
        assert exit_decision.action_name == "SELL"

    @pytest.mark.asyncio
    async def test_long_hold_stays_open(
        self, agent: IBFlagTraderAgent, mock_model, sample_candles
    ):
        import torch

        mock_model.get_action.return_value = (1, 0.5, torch.tensor([-0.1]), 2.0, 1.0)
        exit_decision = await agent.evaluate_position(
            symbol="AAPL",
            direction="long",
            entry_price=150.0,
            pnl_pct=2.0,
            candles=sample_candles["AAPL"],
        )
        assert exit_decision.should_close is False
        assert exit_decision.reason == "hold"

    @pytest.mark.asyncio
    async def test_short_buy_triggers_close(
        self, agent: IBFlagTraderAgent, mock_model, sample_candles
    ):
        import torch

        mock_model.get_action.return_value = (2, 0.8, torch.tensor([-0.2]), 3.0, 1.5)
        exit_decision = await agent.evaluate_position(
            symbol="AAPL",
            direction="short",
            entry_price=160.0,
            pnl_pct=3.0,
            candles=sample_candles["AAPL"],
        )
        assert exit_decision.should_close is True
        assert exit_decision.reason == "model_reversal"

    @pytest.mark.asyncio
    async def test_insufficient_candles_returns_no_close(
        self, agent: IBFlagTraderAgent
    ):
        exit_decision = await agent.evaluate_position(
            symbol="AAPL",
            direction="long",
            entry_price=150.0,
            pnl_pct=0.0,
            candles=[{"open": 150, "high": 151, "low": 149, "close": 150, "volume": 1000}],
        )
        assert exit_decision.should_close is False
        assert exit_decision.reason == "insufficient_data"
