"""
Realtime Monitor Service
========================

Monitors market prices and open positions in real-time,
triggering LLM evaluation only when meaningful events occur:
- Scheduled scan every 5 minutes (fallback)
- Large price move (>2%) on top assets in 5 minutes
- Position PnL exceeds threshold
- New fill detected

This replaces the bar-aligned sleep with event-driven triggering,
reducing latency for important market moves while avoiding
unnecessary LLM calls during quiet periods.
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
    priority: int = 0  # Higher = more urgent


class RealtimeMonitorService:
    """Monitors market and positions, triggers LLM when needed.

    Runs a lightweight poll loop (every 10s) checking for
    significant events. The main bot loop calls should_trigger_llm()
    to decide whether to run a full evaluation cycle.
    """

    def __init__(self, exchange, config) -> None:
        self.exchange = exchange
        self.config = config

        # Trigger thresholds
        self.poll_interval: float = 10.0          # seconds between polls
        self.price_move_threshold: float = 2.0    # % move in 5 min
        self.pnl_threshold: float = 3.0           # % PnL on position
        self.scheduled_interval: float = 300.0    # 5 min fallback

        # Internal state
        self._price_history: dict[str, list[tuple[float, float]]] = defaultdict(list)
        self._last_fill_id: str = ""
        self._last_scheduled: float = 0.0
        self._last_trigger: float = 0.0
        self._min_trigger_cooldown: float = 60.0  # min 60s between triggers
        self._pending_trigger: Optional[TriggerEvent] = None

        # Lifecycle
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start the monitor poll loop in background."""
        if self._running:
            return
        self._running = True
        self._last_scheduled = time.time()
        self._task = asyncio.create_task(self._poll_loop(), name="realtime_monitor")
        logger.info("RealtimeMonitorService started (poll=%ds, cooldown=%ds)",
                     int(self.poll_interval), int(self._min_trigger_cooldown))

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
        """Trigger a scan every 5 minutes as fallback."""
        now = time.time()
        if now - self._last_scheduled >= self.scheduled_interval:
            self._last_scheduled = now
            self._set_trigger(TriggerEvent(
                "scheduled_scan", "5min periodic scan", priority=1,
            ))

    async def _check_price_moves(self) -> None:
        """Check if a top asset moved >2% in 5 minutes."""
        top_assets = ["BTC", "ETH", "SOL", "DOGE", "XRP"]
        for symbol in top_assets:
            try:
                summary = await self.exchange.get_market_summary(symbol)
                price = float(summary.get("markPx", 0))
                if price <= 0:
                    continue

                now = time.time()
                self._price_history[symbol].append((now, price))
                # Keep only last 5 minutes
                self._price_history[symbol] = [
                    (t, p) for t, p in self._price_history[symbol]
                    if now - t < 300
                ]

                if len(self._price_history[symbol]) >= 2:
                    oldest_price = self._price_history[symbol][0][1]
                    pct_move = abs(price - oldest_price) / oldest_price * 100
                    if pct_move >= self.price_move_threshold:
                        direction = "+" if price > oldest_price else "-"
                        self._set_trigger(TriggerEvent(
                            "price_move",
                            f"{symbol} {direction}{pct_move:.1f}% in 5min",
                            priority=3,
                        ))
            except Exception:
                pass  # Don't let one symbol failure block others

    async def _check_position_pnl(self) -> None:
        """Check unrealized PnL on open positions."""
        try:
            positions = await self.exchange.get_positions()
            for pos in positions:
                size = float(pos.get("size", 0))
                if size == 0:
                    continue

                entry_price = float(pos.get("entryPrice", 0))
                mark_price = float(pos.get("markPrice", 0))
                if entry_price <= 0 or mark_price <= 0:
                    continue

                # Calculate PnL % from entry
                side = pos.get("side", "long")
                if side == "long":
                    pnl_pct = (mark_price - entry_price) / entry_price * 100
                else:
                    pnl_pct = (entry_price - mark_price) / entry_price * 100

                symbol = pos.get("symbol", "")
                if abs(pnl_pct) >= self.pnl_threshold:
                    self._set_trigger(TriggerEvent(
                        "position_pnl",
                        f"{symbol} position at {pnl_pct:+.1f}% PnL",
                        priority=2,
                    ))
        except Exception:
            pass

    async def _check_new_fills(self) -> None:
        """Detect new fills since last check."""
        try:
            fills = await self.exchange.get_fills(limit=5)
            if fills and fills[0].get("fillId") != self._last_fill_id:
                new_fill_id = fills[0].get("fillId", "")
                if self._last_fill_id:  # Don't trigger on first poll
                    self._set_trigger(TriggerEvent(
                        "new_fill",
                        f"New fill: {fills[0].get('symbol', '?')}",
                        priority=2,
                    ))
                self._last_fill_id = new_fill_id
        except Exception:
            pass

    # =========================================================================
    # Trigger management
    # =========================================================================

    def _set_trigger(self, event: TriggerEvent) -> None:
        """Set pending trigger, keeping highest priority."""
        if self._pending_trigger is None or event.priority > self._pending_trigger.priority:
            self._pending_trigger = event

    def should_trigger_llm(self) -> tuple[bool, str]:
        """Check if LLM evaluation should run. Called from main loop."""
        if self._pending_trigger is None:
            return False, ""

        now = time.time()
        if now - self._last_trigger < self._min_trigger_cooldown:
            return False, ""

        event = self._pending_trigger
        self._pending_trigger = None
        self._last_trigger = now
        return True, f"{event.reason}: {event.details}"
