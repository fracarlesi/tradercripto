"""
IB Bot Base Service
===================

Abstract base class for all services.
Adapted from crypto_bot pattern for futures trading.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, Optional

from .message_bus import MessageBus, Message
from ..core.enums import Topic

logger = logging.getLogger(__name__)


class BaseService(ABC):
    """Abstract base class for IB bot services."""

    def __init__(
        self,
        name: str,
        bus: Optional[MessageBus] = None,
        loop_interval_seconds: float = 1.0,
    ) -> None:
        self.name = name
        self.bus = bus
        self.loop_interval_seconds = loop_interval_seconds

        self._running = False
        self._start_time: Optional[datetime] = None
        self._main_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
        self._shutdown_event = asyncio.Event()
        self._iteration_count: int = 0
        self._error_count: int = 0
        self._logger = logging.getLogger(f"ib_bot.{name}")

    async def start(self) -> None:
        if self._running:
            return
        self._logger.info("Starting %s...", self.name)
        self._shutdown_event.clear()
        await self._on_start()
        self._main_task = asyncio.create_task(
            self._main_loop(), name=f"{self.name}_main"
        )
        self._running = True
        self._start_time = datetime.now(timezone.utc)
        self._logger.info("%s started", self.name)

    async def stop(self, timeout: float = 10.0) -> None:
        if not self._running:
            return
        self._logger.info("Stopping %s...", self.name)
        self._running = False
        self._shutdown_event.set()
        if self._main_task and not self._main_task.done():
            try:
                await asyncio.wait_for(self._main_task, timeout=timeout)
            except asyncio.TimeoutError:
                self._main_task.cancel()
                try:
                    await self._main_task
                except asyncio.CancelledError:
                    pass
        await self._on_stop()
        self._logger.info("%s stopped", self.name)

    async def _main_loop(self) -> None:
        while not self._shutdown_event.is_set():
            try:
                await self._run_iteration()
                self._iteration_count += 1
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=self.loop_interval_seconds,
                    )
                    break
                except asyncio.TimeoutError:
                    continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._error_count += 1
                self._logger.error("Error in %s: %s", self.name, e, exc_info=True)
                await asyncio.sleep(1.0)

    async def publish(self, topic: Topic, payload: Any) -> None:
        if self.bus:
            await self.bus.publish(topic, payload, source=self.name)

    async def subscribe(self, topic: Topic, handler: Callable[..., Coroutine]) -> None:  # type: ignore[type-arg]
        if self.bus:
            await self.bus.subscribe(topic, handler)

    @abstractmethod
    async def _on_start(self) -> None:
        pass

    @abstractmethod
    async def _on_stop(self) -> None:
        pass

    async def _run_iteration(self) -> None:
        pass

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def stats(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "running": self._running,
            "iterations": self._iteration_count,
            "errors": self._error_count,
        }
