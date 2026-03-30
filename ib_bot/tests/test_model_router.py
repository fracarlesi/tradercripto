"""Tests for ib_bot.flag_trader.model_router."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from ib_bot.flag_trader.model_router import (
    ACTION_NAMES,
    LLMModelRouter,
    ModelEntry,
    RouteDecision,
    classify_asset,
)


# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------

class FakeModel:
    """Minimal fake that satisfies TradingModel protocol."""

    def __init__(self, action: int = 2, value: float = 0.8) -> None:
        self.action = action
        self.value = value
        self.call_count = 0

    def get_action(
        self, prompt: str, return_tokens: bool = False
    ) -> tuple[int, float, float, float, float]:
        self.call_count += 1
        return (self.action, self.value, 0.1, 3.0, 1.5)


class FakePromptBuilder:
    """Minimal fake that satisfies PromptBuilder protocol."""

    def build_prompt(
        self,
        candles: list[dict[str, float]],
        portfolio: dict[str, float],
        history: dict[str, list[Any]],
        **kwargs: Any,
    ) -> str:
        return f"fake prompt for {len(candles)} candles"


def _make_candles(n: int = 25) -> list[dict[str, float]]:
    """Generate N fake candle dicts."""
    return [
        {"open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100.5 + i, "volume": 1e6}
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# classify_asset
# ---------------------------------------------------------------------------

class TestClassifyAsset:
    def test_equity(self) -> None:
        assert classify_asset("AAPL") == "equity"
        assert classify_asset("MSFT") == "equity"

    def test_etf(self) -> None:
        assert classify_asset("SPY") == "etf"
        assert classify_asset("QQQ") == "etf"
        assert classify_asset("XLF") == "etf"

    def test_futures(self) -> None:
        assert classify_asset("ES") == "futures"
        assert classify_asset("MES") == "futures"
        assert classify_asset("NQ") == "futures"

    def test_unknown_defaults_to_equity(self) -> None:
        assert classify_asset("UNKNOWN_TICKER") == "equity"


# ---------------------------------------------------------------------------
# LLMModelRouter — registration and access
# ---------------------------------------------------------------------------

class TestModelRouterRegistration:
    def test_register_model(self) -> None:
        router = LLMModelRouter()
        model = FakeModel()
        pb = FakePromptBuilder()
        router.register_model("equity", model, pb)

        assert router.has_model("equity")
        m, p = router.get_model("equity")
        assert m is model
        assert p is pb

    def test_register_factory_lazy(self) -> None:
        router = LLMModelRouter()
        factory_called = False

        def factory():
            nonlocal factory_called
            factory_called = True
            return FakeModel(), FakePromptBuilder()

        router.register_factory("etf", factory)
        assert router.has_model("etf")
        assert not factory_called  # Not loaded yet

        m, p = router.get_model("etf")
        assert factory_called
        assert isinstance(m, FakeModel)

    def test_get_model_fallback_to_equity(self) -> None:
        router = LLMModelRouter()
        equity_model = FakeModel()
        router.register_model("equity", equity_model, FakePromptBuilder())

        # "etf" not registered -> fallback to equity
        m, _ = router.get_model("etf")
        assert m is equity_model

    def test_get_model_no_fallback_raises(self) -> None:
        router = LLMModelRouter()
        with pytest.raises(KeyError, match="No model registered"):
            router.get_model("futures")

    def test_registered_classes(self) -> None:
        router = LLMModelRouter()
        router.register_model("equity", FakeModel(), FakePromptBuilder())
        router.register_model("etf", FakeModel(), FakePromptBuilder())
        assert sorted(router.registered_classes) == ["equity", "etf"]

    def test_has_model_false(self) -> None:
        router = LLMModelRouter()
        assert not router.has_model("equity")


# ---------------------------------------------------------------------------
# LLMModelRouter — route_and_decide
# ---------------------------------------------------------------------------

class TestModelRouterRouting:
    @pytest.mark.asyncio
    async def test_route_and_decide_basic(self) -> None:
        router = LLMModelRouter(confidence_threshold=0.5, candle_window=5)
        model = FakeModel(action=2, value=0.9)
        router.register_model("equity", model, FakePromptBuilder())

        candidates = [{"symbol": "AAPL"}, {"symbol": "MSFT"}]
        candle_cache = {
            "AAPL": _make_candles(10),
            "MSFT": _make_candles(10),
        }
        decisions = await router.route_and_decide(candidates, candle_cache)

        assert len(decisions) == 2
        assert all(isinstance(d, RouteDecision) for d in decisions)
        assert all(d.action_name == "BUY" for d in decisions)
        assert model.call_count == 2

    @pytest.mark.asyncio
    async def test_hold_decisions_filtered_out(self) -> None:
        router = LLMModelRouter(confidence_threshold=0.5, candle_window=5)
        model = FakeModel(action=1, value=0.9)  # HOLD
        router.register_model("equity", model, FakePromptBuilder())

        decisions = await router.route_and_decide(
            [{"symbol": "AAPL"}],
            {"AAPL": _make_candles(10)},
        )
        assert len(decisions) == 0

    @pytest.mark.asyncio
    async def test_low_confidence_filtered_out(self) -> None:
        router = LLMModelRouter(confidence_threshold=0.7, candle_window=5)
        model = FakeModel(action=2, value=0.5)  # BUY but low confidence
        router.register_model("equity", model, FakePromptBuilder())

        decisions = await router.route_and_decide(
            [{"symbol": "AAPL"}],
            {"AAPL": _make_candles(10)},
        )
        assert len(decisions) == 0

    @pytest.mark.asyncio
    async def test_insufficient_candles_skipped(self) -> None:
        router = LLMModelRouter(confidence_threshold=0.5, candle_window=20)
        model = FakeModel(action=2, value=0.9)
        router.register_model("equity", model, FakePromptBuilder())

        decisions = await router.route_and_decide(
            [{"symbol": "AAPL"}],
            {"AAPL": _make_candles(5)},  # Too few candles
        )
        assert len(decisions) == 0
        assert model.call_count == 0

    @pytest.mark.asyncio
    async def test_routes_to_correct_model(self) -> None:
        router = LLMModelRouter(confidence_threshold=0.5, candle_window=5)

        equity_model = FakeModel(action=2, value=0.9)
        etf_model = FakeModel(action=0, value=0.8)

        router.register_model("equity", equity_model, FakePromptBuilder())
        router.register_model("etf", etf_model, FakePromptBuilder())

        candidates = [
            {"symbol": "AAPL"},  # equity
            {"symbol": "SPY"},   # etf
        ]
        candle_cache = {
            "AAPL": _make_candles(10),
            "SPY": _make_candles(10),
        }

        decisions = await router.route_and_decide(candidates, candle_cache)
        assert len(decisions) == 2

        aapl_dec = next(d for d in decisions if d.symbol == "AAPL")
        spy_dec = next(d for d in decisions if d.symbol == "SPY")

        assert aapl_dec.action_name == "BUY"
        assert aapl_dec.asset_class == "equity"
        assert spy_dec.action_name == "SELL"
        assert spy_dec.asset_class == "etf"

        assert equity_model.call_count == 1
        assert etf_model.call_count == 1

    @pytest.mark.asyncio
    async def test_missing_symbol_in_cache_skipped(self) -> None:
        router = LLMModelRouter(confidence_threshold=0.5, candle_window=5)
        router.register_model("equity", FakeModel(), FakePromptBuilder())

        decisions = await router.route_and_decide(
            [{"symbol": "AAPL"}],
            {},  # Empty cache
        )
        assert len(decisions) == 0

    @pytest.mark.asyncio
    async def test_sorted_by_confidence_descending(self) -> None:
        """Test that results are sorted by absolute confidence."""
        router = LLMModelRouter(confidence_threshold=0.5, candle_window=5)

        # Use different models with different confidence values
        class SequentialModel:
            def __init__(self):
                self.values = iter([0.6, 0.9, 0.7])
            def get_action(self, prompt, return_tokens=False):
                return (2, next(self.values), 0.1, 3.0, 1.5)

        router.register_model("equity", SequentialModel(), FakePromptBuilder())

        candidates = [{"symbol": "AAPL"}, {"symbol": "MSFT"}, {"symbol": "NVDA"}]
        cache = {s["symbol"]: _make_candles(10) for s in candidates}

        decisions = await router.route_and_decide(candidates, cache)
        assert len(decisions) == 3
        confidences = [d.confidence for d in decisions]
        assert confidences == sorted(confidences, key=abs, reverse=True)


# ---------------------------------------------------------------------------
# LLMModelRouter — idle unload
# ---------------------------------------------------------------------------

class TestModelRouterIdleUnload:
    def test_unload_idle_models(self) -> None:
        router = LLMModelRouter(idle_timeout_seconds=0)  # Immediate timeout

        called = False
        def factory():
            nonlocal called
            called = True
            return FakeModel(), FakePromptBuilder()

        router.register_factory("etf", factory)
        # Load it
        router.get_model("etf")
        assert called

        # Force idle by setting last_used to 0
        router._entries["etf"].last_used = 0

        unloaded = router.unload_idle_models()
        assert "etf" in unloaded
        assert not router._entries["etf"].loaded

    def test_preloaded_without_factory_not_unloaded(self) -> None:
        router = LLMModelRouter(idle_timeout_seconds=0)
        router.register_model("equity", FakeModel(), FakePromptBuilder())
        router._entries["equity"].last_used = 0

        unloaded = router.unload_idle_models()
        assert "equity" not in unloaded  # No factory, can't re-load

    def test_reload_after_unload(self) -> None:
        router = LLMModelRouter(idle_timeout_seconds=0)
        call_count = 0

        def factory():
            nonlocal call_count
            call_count += 1
            return FakeModel(), FakePromptBuilder()

        router.register_factory("etf", factory)

        # Load
        router.get_model("etf")
        assert call_count == 1

        # Unload
        router._entries["etf"].last_used = 0
        router.unload_idle_models()

        # Re-load
        router.get_model("etf")
        assert call_count == 2


# ---------------------------------------------------------------------------
# LLMModelRouter — load_models from config
# ---------------------------------------------------------------------------

class TestModelRouterLoadModels:
    def test_load_models_registers_factories(self) -> None:
        router = LLMModelRouter()
        config = {
            "equity": {"model_name": "test", "device": "cpu"},
            "etf": {"model_name": "test", "device": "cpu"},
            "futures": {"model_name": "test", "device": "cpu"},
        }
        router.load_models(config)

        assert router.has_model("equity")
        assert router.has_model("etf")
        assert router.has_model("futures")

        # All should be lazy (not yet loaded)
        for ac in ("equity", "etf", "futures"):
            assert not router._entries[ac].loaded
