"""
Realtime Monitor Service
========================

Monitors market in real-time using BB-KC squeeze detection.
When a squeeze fires on a universe asset, it triggers LLM evaluation
for that specific symbol.

Purely event-driven: NO scheduled fallback scan.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TriggerEvent:
    """An event that should trigger LLM evaluation."""

    reason: str       # "squeeze_fire"
    details: str      # "BTC squeeze_bars=3 bb_w=0.0012 kc_w=0.0015"
    symbols: list[str] = field(default_factory=list)  # which assets to evaluate
    priority: int = 0  # Higher = more urgent


class RealtimeMonitorService:
    """Monitors market via squeeze indicator, triggers LLM when needed.

    Runs a lightweight poll loop checking for BB-KC squeeze fire events.

    The main bot loop calls should_trigger_llm() which returns
    both the trigger reason AND which symbols to evaluate.
    """

    def __init__(
        self,
        exchange,
        config,
        universe_assets: list[str] | None = None,
        squeeze_config: dict | None = None,
    ) -> None:
        self.exchange = exchange
        self.config = config
        self._universe: list[str] = universe_assets or []

        # Poll settings
        self.poll_interval: float = 10.0          # seconds between polls

        # Trigger management
        self._last_trigger: float = 0.0
        self._min_trigger_cooldown: float = 60.0  # min 60s between triggers
        self._pending_trigger: Optional[TriggerEvent] = None

        # Squeeze trigger state
        sq = squeeze_config or {}
        self._squeeze_enabled: bool = sq.get("enabled", False)
        self._squeeze_candle_interval: str = sq.get("candle_interval", "15m")
        self._squeeze_candle_limit: int = sq.get("candle_limit", 50)
        self._squeeze_candle_ttl: float = sq.get("candle_ttl_seconds", 300.0)
        self._squeeze_lookback: int = sq.get("lookback_bars", 3)
        self._squeeze_bb_period: int = sq.get("bb_period", 20)
        self._squeeze_bb_std_mult: float = sq.get("bb_std_mult", 2.0)
        self._squeeze_kc_ema_period: int = sq.get("kc_ema_period", 20)
        self._squeeze_kc_atr_period: int = sq.get("kc_atr_period", 14)
        self._squeeze_kc_atr_mult: float = sq.get("kc_atr_mult", 1.5)

        # Candle cache: symbol -> (fetch_timestamp, candles_list)
        self._candle_cache: dict[str, tuple[float, list[dict]]] = {}
        # Squeeze state: symbol -> SqueezeResult
        from .squeeze_indicator import SqueezeResult
        self._squeeze_states: dict[str, SqueezeResult] = {}

        # Lifecycle
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None

    def set_universe(self, assets: list[str]) -> None:
        """Update the monitored asset universe (called after dynamic loading)."""
        self._universe = assets
        logger.info("Monitor universe updated: %d assets", len(assets))

    async def start(self) -> None:
        """Start the monitor poll loop in background."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="realtime_monitor")
        logger.info(
            "RealtimeMonitorService started (poll=%ds, cooldown=%ds, universe=%d assets, squeeze=%s)",
            int(self.poll_interval), int(self._min_trigger_cooldown), len(self._universe),
            "ON" if self._squeeze_enabled else "OFF",
        )

    async def stop(self) -> None:
        """Stop the monitor."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("RealtimeMonitorService stopped")

    # =========================================================================
    # Main poll loop
    # =========================================================================

    async def _poll_loop(self) -> None:
        """Poll for squeeze trigger conditions."""
        while self._running:
            try:
                if self._squeeze_enabled:
                    await self._check_squeeze_fire()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Monitor poll error: %s", e)
            await asyncio.sleep(self.poll_interval)

    # =========================================================================
    # Squeeze check
    # =========================================================================

    async def _check_squeeze_fire(self) -> None:
        """Check universe assets for BB-KC squeeze -> fire transitions."""
        if not self._universe:
            return

        import numpy as np
        from .squeeze_indicator import detect_squeeze_state

        now = time.time()
        triggered_symbols: list[str] = []
        triggered_details: list[str] = []

        for symbol in self._universe:
            try:
                # Check candle cache TTL
                cached = self._candle_cache.get(symbol)
                if cached and (now - cached[0]) < self._squeeze_candle_ttl:
                    candles = cached[1]
                else:
                    candles = await self.exchange.get_candles(
                        symbol,
                        interval=self._squeeze_candle_interval,
                        limit=self._squeeze_candle_limit,
                    )
                    if not candles:
                        continue
                    self._candle_cache[symbol] = (now, candles)

                # Extract OHLCV arrays (keys: c, h, l from hyperliquid API)
                close = np.array([float(c["c"]) for c in candles])
                high = np.array([float(c["h"]) for c in candles])
                low = np.array([float(c["l"]) for c in candles])

                if len(close) < 2:
                    continue

                result = detect_squeeze_state(
                    symbol=symbol,
                    close=close,
                    high=high,
                    low=low,
                    bb_period=self._squeeze_bb_period,
                    bb_std_mult=self._squeeze_bb_std_mult,
                    kc_ema_period=self._squeeze_kc_ema_period,
                    kc_atr_period=self._squeeze_kc_atr_period,
                    kc_atr_mult=self._squeeze_kc_atr_mult,
                    lookback=self._squeeze_lookback,
                )
                self._squeeze_states[symbol] = result

                if result.fired:
                    triggered_symbols.append(symbol)
                    triggered_details.append(
                        f"{symbol} squeeze_bars={result.squeeze_bars} bb_w={result.bb_width:.4f} kc_w={result.kc_width:.4f}"
                    )
                    logger.info(
                        "SQUEEZE FIRE | %s | squeeze_bars=%d | bb_width=%.4f | kc_width=%.4f",
                        symbol, result.squeeze_bars, result.bb_width, result.kc_width,
                    )
                elif result.in_squeeze_now:
                    logger.debug(
                        "SQUEEZE ACTIVE | %s | bars=%d | bb_w=%.4f | kc_w=%.4f",
                        symbol, result.squeeze_bars, result.bb_width, result.kc_width,
                    )

            except Exception as e:
                logger.debug("Squeeze check failed for %s: %s", symbol, e)
                continue

        if triggered_symbols:
            self._set_trigger(TriggerEvent(
                reason="squeeze_fire",
                details=", ".join(triggered_details),
                symbols=triggered_symbols,
                priority=4,
            ))

    # =========================================================================
    # Trigger management
    # =========================================================================

    def _set_trigger(self, event: TriggerEvent) -> None:
        """Set pending trigger, merging symbols from multiple events."""
        if self._pending_trigger is None:
            self._pending_trigger = event
        elif event.priority > self._pending_trigger.priority:
            # Higher priority replaces, but keep symbols from both
            merged_symbols = list(dict.fromkeys(
                self._pending_trigger.symbols + event.symbols
            ))
            event.symbols = merged_symbols
            self._pending_trigger = event
        else:
            # Lower/equal priority: merge symbols into existing trigger
            merged = list(dict.fromkeys(
                self._pending_trigger.symbols + event.symbols
            ))
            self._pending_trigger.symbols = merged

    def should_trigger_llm(self) -> tuple[bool, str, list[str]]:
        """Check if LLM evaluation should run. Called from main loop.

        Returns:
            (should_trigger, reason_string, symbols_to_evaluate)
            If symbols is empty, caller should use default scan (top N).
        """
        if self._pending_trigger is None:
            return False, "", []

        now = time.time()
        if now - self._last_trigger < self._min_trigger_cooldown:
            return False, "", []

        event = self._pending_trigger
        self._pending_trigger = None
        self._last_trigger = now

        logger.info(
            "TRIGGER | %s | symbols=%s | priority=%d",
            event.reason, ",".join(event.symbols) if event.symbols else "all", event.priority,
        )

        return True, f"{event.reason}: {event.details}", event.symbols
