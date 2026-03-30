"""
LLM Model Router — Routes Candidates to Specialized Models
============================================================

Manages multiple FlagTrader models (equity, ETF, futures) and routes
scan candidates to the correct model based on asset class.

Features:
- Register/unregister models per asset class
- On-demand (lazy) loading to save RAM
- Automatic unload after configurable timeout
- Fallback to equity model if specialized model not available
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ib_bot.scanner.universe import ETF_UNIVERSE, FUTURES_UNIVERSE

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocols for model and prompt builder
# ---------------------------------------------------------------------------

@runtime_checkable
class TradingModel(Protocol):
    """Protocol for any FLAG-Trader model variant."""

    def get_action(
        self, prompt: str, return_tokens: bool = False
    ) -> tuple[int, float, Any, float, float]: ...


@runtime_checkable
class PromptBuilder(Protocol):
    """Protocol for any prompt builder variant."""

    def build_prompt(
        self,
        candles: list[dict[str, float]],
        portfolio: dict[str, float],
        history: dict[str, list[Any]],
        **kwargs: Any,
    ) -> str: ...


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ModelEntry:
    """Registration entry for a model + prompt builder pair."""

    asset_class: str
    model: TradingModel | None = None
    prompt_builder: PromptBuilder | None = None
    factory: Any | None = None  # Callable[[], tuple[TradingModel, PromptBuilder]]
    last_used: float = 0.0
    loaded: bool = False


@dataclass
class RouteDecision:
    """Result of routing and evaluating a single candidate."""

    symbol: str
    asset_class: str
    action: int  # 0=Sell, 1=Hold, 2=Buy
    action_name: str
    confidence: float
    tp_pct: float
    sl_pct: float
    log_prob: float = 0.0


ACTION_NAMES = {0: "SELL", 1: "HOLD", 2: "BUY"}

# Sets for fast lookup
_ETF_SET: set[str] = set(ETF_UNIVERSE)
_FUTURES_SET: set[str] = set(FUTURES_UNIVERSE)


def classify_asset(symbol: str) -> str:
    """Determine asset class from symbol.

    Args:
        symbol: Ticker symbol.

    Returns:
        One of "equity", "etf", "futures".
    """
    if symbol in _FUTURES_SET:
        return "futures"
    if symbol in _ETF_SET:
        return "etf"
    return "equity"


# ---------------------------------------------------------------------------
# LLMModelRouter
# ---------------------------------------------------------------------------

class LLMModelRouter:
    """Routes scan candidates to the appropriate specialized LLM model.

    Supports on-demand loading: models are loaded only when needed
    and can be unloaded after a configurable idle timeout to save RAM.

    Usage:
        router = LLMModelRouter()
        router.register_model("equity", model, prompt_builder)
        # or with lazy loading:
        router.register_factory("etf", lambda: (ETFModel(), ETFPromptBuilder()))

        decisions = await router.route_and_decide(candidates, candle_cache)
    """

    def __init__(
        self,
        confidence_threshold: float = 0.6,
        candle_window: int = 20,
        idle_timeout_seconds: float = 300.0,
    ) -> None:
        self._entries: dict[str, ModelEntry] = {}
        self.confidence_threshold = confidence_threshold
        self.candle_window = candle_window
        self.idle_timeout_seconds = idle_timeout_seconds

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_model(
        self,
        asset_class: str,
        model: TradingModel,
        prompt_builder: PromptBuilder,
    ) -> None:
        """Register a pre-loaded model for an asset class.

        Args:
            asset_class: One of "equity", "etf", "futures".
            model: A FlagTraderModel variant.
            prompt_builder: The corresponding prompt builder.
        """
        self._entries[asset_class] = ModelEntry(
            asset_class=asset_class,
            model=model,
            prompt_builder=prompt_builder,
            last_used=time.monotonic(),
            loaded=True,
        )
        logger.info("Registered model for asset_class=%s (pre-loaded)", asset_class)

    def register_factory(
        self,
        asset_class: str,
        factory: Any,
    ) -> None:
        """Register a lazy-loading factory for an asset class.

        The factory is called only when the model is first needed.
        It must return (model, prompt_builder).

        Args:
            asset_class: One of "equity", "etf", "futures".
            factory: Callable that returns (TradingModel, PromptBuilder).
        """
        self._entries[asset_class] = ModelEntry(
            asset_class=asset_class,
            factory=factory,
            loaded=False,
        )
        logger.info("Registered factory for asset_class=%s (lazy)", asset_class)

    # ------------------------------------------------------------------
    # Model access
    # ------------------------------------------------------------------

    def get_model(self, asset_class: str) -> tuple[TradingModel, PromptBuilder]:
        """Get the model and prompt builder for an asset class.

        Loads from factory if not yet loaded. Falls back to "equity"
        if the requested asset class is not registered.

        Args:
            asset_class: One of "equity", "etf", "futures".

        Returns:
            (model, prompt_builder) tuple.

        Raises:
            KeyError: If no model is registered and no fallback available.
        """
        entry = self._entries.get(asset_class)

        # Fallback to equity if not registered
        if entry is None:
            logger.warning(
                "No model for asset_class=%s, falling back to equity", asset_class
            )
            entry = self._entries.get("equity")
            if entry is None:
                raise KeyError(
                    f"No model registered for '{asset_class}' and no equity fallback"
                )

        # Lazy load if needed
        if not entry.loaded and entry.factory is not None:
            logger.info("Lazy-loading model for asset_class=%s", entry.asset_class)
            model, prompt_builder = entry.factory()
            entry.model = model
            entry.prompt_builder = prompt_builder
            entry.loaded = True

        entry.last_used = time.monotonic()

        if entry.model is None or entry.prompt_builder is None:
            raise KeyError(f"Model for '{asset_class}' is registered but not loaded")

        return entry.model, entry.prompt_builder

    def has_model(self, asset_class: str) -> bool:
        """Check if a model is registered (loaded or lazy) for an asset class."""
        return asset_class in self._entries

    @property
    def registered_classes(self) -> list[str]:
        """Return list of registered asset classes."""
        return list(self._entries.keys())

    # ------------------------------------------------------------------
    # Routing and evaluation
    # ------------------------------------------------------------------

    async def route_and_decide(
        self,
        candidates: list[dict[str, Any]],
        candle_cache: dict[str, list[dict[str, float]]],
        portfolio: dict[str, float] | None = None,
    ) -> list[RouteDecision]:
        """Route candidates to appropriate models and collect decisions.

        For each candidate:
        1. Classify asset class (equity/etf/futures)
        2. Route to the correct model
        3. Evaluate and collect actionable decisions

        Args:
            candidates: List of dicts with at least 'symbol' key.
            candle_cache: Dict mapping symbol -> list of candle dicts.
            portfolio: Current portfolio state.

        Returns:
            List of RouteDecision for actionable signals (Buy/Sell above threshold),
            sorted by absolute confidence descending.
        """
        if portfolio is None:
            portfolio = {
                "cash_balance": 0.0,
                "asset_position": 0.0,
                "total_account_value": 0.0,
            }

        decisions: list[RouteDecision] = []

        for candidate in candidates:
            symbol = candidate["symbol"]
            asset_class = classify_asset(symbol)
            candles = candle_cache.get(symbol)

            if not candles or len(candles) < self.candle_window:
                logger.debug(
                    "Insufficient candle data for %s: %d candles",
                    symbol,
                    len(candles) if candles else 0,
                )
                continue

            try:
                model, prompt_builder = self.get_model(asset_class)
            except KeyError as e:
                logger.warning("Cannot route %s: %s", symbol, e)
                continue

            try:
                decision = await self._evaluate_single(
                    symbol=symbol,
                    asset_class=asset_class,
                    candles=candles,
                    model=model,
                    prompt_builder=prompt_builder,
                    portfolio=portfolio,
                    extra_kwargs=candidate,
                )
                if decision is not None:
                    decisions.append(decision)
            except Exception as e:
                logger.warning("Error evaluating %s via %s model: %s", symbol, asset_class, e)

        decisions.sort(key=lambda d: abs(d.confidence), reverse=True)

        logger.info(
            "ModelRouter: evaluated %d candidates, %d actionable decisions",
            len(candidates),
            len(decisions),
        )
        return decisions

    async def _evaluate_single(
        self,
        symbol: str,
        asset_class: str,
        candles: list[dict[str, float]],
        model: TradingModel,
        prompt_builder: PromptBuilder,
        portfolio: dict[str, float],
        extra_kwargs: dict[str, Any],
    ) -> RouteDecision | None:
        """Evaluate a single candidate with the routed model.

        Returns RouteDecision if actionable, else None.
        """
        history: dict[str, list[Any]] = {
            "recent_rewards": [],
            "net_values": [],
            "actions": [],
        }

        # Build kwargs for prompt builder (pass extra fields like sector, session_phase)
        build_kwargs: dict[str, Any] = {}
        if "sector" in extra_kwargs:
            build_kwargs["sector"] = extra_kwargs["sector"]
        if "session_phase" in extra_kwargs:
            build_kwargs["session_phase"] = extra_kwargs["session_phase"]
        if "volume_profile" in extra_kwargs:
            build_kwargs["volume_profile"] = extra_kwargs["volume_profile"]
        if "spy_correlation" in extra_kwargs:
            build_kwargs["spy_correlation"] = extra_kwargs["spy_correlation"]
        if "sector_beta" in extra_kwargs:
            build_kwargs["sector_beta"] = extra_kwargs["sector_beta"]

        prompt = prompt_builder.build_prompt(
            candles=candles,
            portfolio=portfolio,
            history=history,
            **build_kwargs,
        )

        # Model inference (synchronous, run in executor to avoid blocking)
        loop = asyncio.get_running_loop()
        action_id, state_value, log_prob, tp_pct, sl_pct = await loop.run_in_executor(
            None, model.get_action, prompt
        )

        action_name = ACTION_NAMES.get(action_id, "HOLD")

        logger.info(
            "ModelRouter | %s [%s] | action=%s | value=%.4f | tp=%.1f%% | sl=%.1f%%",
            symbol, asset_class, action_name, state_value, tp_pct, sl_pct,
        )

        # Only return actionable decisions (Buy/Sell above threshold)
        if action_id == 1:  # Hold
            return None

        if abs(state_value) < self.confidence_threshold:
            return None

        return RouteDecision(
            symbol=symbol,
            asset_class=asset_class,
            action=action_id,
            action_name=action_name,
            confidence=state_value,
            tp_pct=tp_pct,
            sl_pct=sl_pct,
            log_prob=float(log_prob),
        )

    # ------------------------------------------------------------------
    # Memory management
    # ------------------------------------------------------------------

    def unload_idle_models(self) -> list[str]:
        """Unload models that haven't been used for idle_timeout_seconds.

        Returns:
            List of asset classes that were unloaded.
        """
        now = time.monotonic()
        unloaded: list[str] = []

        for asset_class, entry in self._entries.items():
            if not entry.loaded:
                continue
            if entry.factory is None:
                # Pre-loaded models without factory can't be re-loaded, skip
                continue
            if (now - entry.last_used) > self.idle_timeout_seconds:
                entry.model = None
                entry.prompt_builder = None
                entry.loaded = False
                unloaded.append(asset_class)
                logger.info(
                    "Unloaded idle model for asset_class=%s (idle %.0fs)",
                    asset_class, now - entry.last_used,
                )

        return unloaded

    def load_models(self, config: dict[str, Any]) -> None:
        """Load models from a configuration dict.

        Config format:
            {
                "equity": {"model_name": "...", "checkpoint": "...", "device": "cpu"},
                "etf": {"model_name": "...", "checkpoint": "...", "device": "cpu"},
                "futures": {"model_name": "...", "checkpoint": "...", "device": "cpu"},
            }

        Models are registered as lazy factories and loaded on first use.

        Args:
            config: Dict mapping asset_class -> model config.
        """
        from pathlib import Path

        for asset_class, model_config in config.items():
            model_name = model_config.get("model_name", "Qwen/Qwen2.5-0.5B-Instruct")
            checkpoint = model_config.get("checkpoint")
            device = model_config.get("device", "cpu")

            def _make_factory(
                ac: str, mn: str, cp: str | None, dev: str
            ) -> Any:
                """Create a closure factory for lazy loading."""
                def factory() -> tuple[TradingModel, PromptBuilder]:
                    model: TradingModel
                    prompt_builder: PromptBuilder
                    if ac == "etf":
                        from .etf_model import ETFFlagTraderModel
                        from .etf_prompt import ETFPromptBuilder
                        model = ETFFlagTraderModel(model_name=mn, device=dev)
                        prompt_builder = ETFPromptBuilder()
                    elif ac == "futures":
                        from .futures_model import FuturesFlagTraderModel
                        from .futures_prompt import FuturesPromptBuilder
                        model = FuturesFlagTraderModel(model_name=mn, device=dev)
                        prompt_builder = FuturesPromptBuilder()
                    else:
                        from .equity_model import EquityFlagTraderModel
                        from .equity_prompt import EquityPromptBuilder
                        model = EquityFlagTraderModel(model_name=mn, device=dev)
                        prompt_builder = EquityPromptBuilder()

                    if cp:
                        model.load_trainable(Path(cp))  # type: ignore[attr-defined]
                    return model, prompt_builder
                return factory

            self.register_factory(
                asset_class,
                _make_factory(asset_class, model_name, checkpoint, device),
            )

        logger.info("Loaded model config for %d asset classes: %s", len(config), list(config.keys()))
