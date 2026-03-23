"""
Realtime Monitor Service
========================

Monitors market prices and open positions in real-time,
triggering LLM evaluation only when meaningful events occur:
- Scheduled scan every 5 minutes (top N by volume, fallback)
- Large price move (>2%) on ANY universe asset in 5 minutes
- Position PnL exceeds threshold
- New fill detected

Key design: uses a single `get_all_mids()` call per poll (1 API call)
to monitor ALL assets in the universe, not a per-asset loop.

When a trigger fires, it includes which specific symbols caused it,
so the LLM evaluates ONLY those assets instead of scanning everything.
"""

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TriggerEvent:
    """An event that should trigger LLM evaluation."""

    reason: str       # "scheduled_scan", "price_move", "position_pnl", "new_fill"
    details: str      # "BTC +2.3% in 5min", "ETH position -5% PnL"
    symbols: list[str] = field(default_factory=list)  # which assets to evaluate
    priority: int = 0  # Higher = more urgent


class RealtimeMonitorService:
    """Monitors market and positions, triggers LLM when needed.

    Runs a lightweight poll loop (every 10s) checking for
    significant events. Uses batch price fetch (1 API call for all assets).

    The main bot loop calls should_trigger_llm() which returns
    both the trigger reason AND which symbols to evaluate.
    """

    def __init__(self, exchange, config, universe_assets: list[str] | None = None) -> None:
        self.exchange = exchange
        self.config = config
        self._universe: list[str] = universe_assets or []

        # Trigger thresholds
        self.poll_interval: float = 10.0          # seconds between polls
        self.price_move_threshold: float = 2.0    # % move in 5 min
        self.pnl_threshold: float = 3.0           # % PnL on position
        self.scheduled_interval: float = 300.0    # 5 min fallback

        # Internal state
        self._price_snapshots: dict[str, list[tuple[float, float]]] = defaultdict(list)
        self._last_fill_id: str = ""
        self._last_scheduled: float = 0.0
        self._last_trigger: float = 0.0
        self._min_trigger_cooldown: float = 60.0  # min 60s between triggers
        self._pending_trigger: Optional[TriggerEvent] = None

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
        self._last_scheduled = time.time()
        self._task = asyncio.create_task(self._poll_loop(), name="realtime_monitor")
        logger.info(
            "RealtimeMonitorService started (poll=%ds, cooldown=%ds, universe=%d assets)",
            int(self.poll_interval), int(self._min_trigger_cooldown), len(self._universe),
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
        """Poll every 10 seconds for trigger conditions."""
        while self._running:
            try:
                await self._check_scheduled_scan()
                await self._check_price_moves()
                await self._check_position_pnl()
                await self._check_new_fills()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Monitor poll error: %s", e)
            await asyncio.sleep(self.poll_interval)

    # =========================================================================
    # Trigger checks
    # =========================================================================

    async def _check_scheduled_scan(self) -> None:
        """Trigger a full scan every 5 minutes as fallback.

        Scheduled scans pass NO specific symbols — the main loop
        will use its default top-N selection.
        """
        now = time.time()
        if now - self._last_scheduled >= self.scheduled_interval:
            self._last_scheduled = now
            self._set_trigger(TriggerEvent(
                reason="scheduled_scan",
                details="5min periodic scan",
                symbols=[],  # empty = scan top N (default behavior)
                priority=1,
            ))

    async def _check_price_moves(self) -> None:
        """Check ALL universe assets for >2% moves in 5 minutes.

        Uses a single batch API call (get_all_mids) instead of
        per-asset calls. Only assets that moved trigger the LLM.
        """
        if not self._universe:
            return

        try:
            all_mids = await self.exchange.get_all_mids()
        except Exception as e:
            logger.debug("Failed to fetch all_mids: %s", e)
            return

        now = time.time()
        triggered_symbols: list[str] = []
        triggered_details: list[str] = []

        universe_set = set(self._universe)
        for symbol, price in all_mids.items():
            if symbol not in universe_set or price <= 0:
                continue

            self._price_snapshots[symbol].append((now, price))
            # Keep only last 5 minutes
            self._price_snapshots[symbol] = [
                (t, p) for t, p in self._price_snapshots[symbol]
                if now - t < 300
            ]

            if len(self._price_snapshots[symbol]) >= 2:
                oldest_price = self._price_snapshots[symbol][0][1]
                pct_move = abs(price - oldest_price) / oldest_price * 100
                if pct_move >= self.price_move_threshold:
                    direction = "+" if price > oldest_price else "-"
                    triggered_symbols.append(symbol)
                    triggered_details.append(f"{symbol} {direction}{pct_move:.1f}%")

        if triggered_symbols:
            self._set_trigger(TriggerEvent(
                reason="price_move",
                details=", ".join(triggered_details),
                symbols=triggered_symbols,
                priority=3,
            ))

    async def _check_position_pnl(self) -> None:
        """Check unrealized PnL on open positions."""
        try:
            positions = await self.exchange.get_positions()
        except Exception:
            return

        triggered_symbols: list[str] = []

        for pos in positions:
            size = float(pos.get("size", 0))
            if size == 0:
                continue

            entry_price = float(pos.get("entryPrice", 0))
            mark_price = float(pos.get("markPrice", 0))
            if entry_price <= 0 or mark_price <= 0:
                continue

            side = pos.get("side", "long")
            if side == "long":
                pnl_pct = (mark_price - entry_price) / entry_price * 100
            else:
                pnl_pct = (entry_price - mark_price) / entry_price * 100

            symbol = pos.get("symbol", "")
            if abs(pnl_pct) >= self.pnl_threshold:
                triggered_symbols.append(symbol)
                self._set_trigger(TriggerEvent(
                    reason="position_pnl",
                    details=f"{symbol} at {pnl_pct:+.1f}% PnL",
                    symbols=triggered_symbols,
                    priority=2,
                ))

    async def _check_new_fills(self) -> None:
        """Detect new fills since last check."""
        try:
            fills = await self.exchange.get_fills(limit=5)
            if fills and fills[0].get("fillId") != self._last_fill_id:
                new_fill_id = fills[0].get("fillId", "")
                fill_symbol = fills[0].get("symbol", "")
                if self._last_fill_id:  # Don't trigger on first poll
                    self._set_trigger(TriggerEvent(
                        reason="new_fill",
                        details=f"New fill: {fill_symbol}",
                        symbols=[fill_symbol] if fill_symbol else [],
                        priority=2,
                    ))
                self._last_fill_id = new_fill_id
        except Exception:
            pass

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
        return True, f"{event.reason}: {event.details}", event.symbols
