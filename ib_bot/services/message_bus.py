"""
IB Bot Message Bus
==================

Async pub/sub message bus for inter-service communication.
Adapted from crypto_bot pattern for futures trading.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

from ..core.enums import Topic

logger = logging.getLogger(__name__)

MessageHandler = Callable[["Message"], Coroutine[Any, Any, None]]


@dataclass
class Message:
    """Immutable message passed through the bus."""

    topic: Topic
    payload: Any
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "unknown"
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self) -> None:
        if isinstance(self.topic, str):
            self.topic = Topic(self.topic)

    def age_ms(self) -> float:
        delta = datetime.now(timezone.utc) - self.timestamp
        return delta.total_seconds() * 1000


@dataclass
class TopicStats:
    """Statistics for a single topic."""

    message_count: int = 0
    total_latency_ms: float = 0.0
    last_message_time: Optional[datetime] = None
    subscriber_count: int = 0

    @property
    def avg_latency_ms(self) -> float:
        if self.message_count == 0:
            return 0.0
        return self.total_latency_ms / self.message_count


class MessageBus:
    """Async pub/sub message bus with topic-based routing."""

    def __init__(self, max_queue_size: int = 1000) -> None:
        self._max_queue_size = max_queue_size
        self._queues: Dict[Topic, asyncio.Queue[Message]] = {}
        self._subscribers: Dict[Topic, Set[MessageHandler]] = {}
        self._stats: Dict[Topic, TopicStats] = {}
        self._running = False
        self._tasks: List[asyncio.Task] = []  # type: ignore[type-arg]
        self._lock = asyncio.Lock()

        for topic in Topic:
            self._queues[topic] = asyncio.Queue(maxsize=max_queue_size)
            self._subscribers[topic] = set()
            self._stats[topic] = TopicStats()

        logger.info("MessageBus initialized with %d topics", len(Topic))

    async def subscribe(self, topic: Topic, handler: MessageHandler) -> None:
        async with self._lock:
            self._subscribers[topic].add(handler)
            self._stats[topic].subscriber_count = len(self._subscribers[topic])

    async def unsubscribe(self, topic: Topic, handler: MessageHandler) -> bool:
        async with self._lock:
            self._subscribers[topic].discard(handler)
            self._stats[topic].subscriber_count = len(self._subscribers[topic])
            return True

    async def publish(
        self,
        topic: Topic,
        payload: Any,
        source: str = "unknown",
        message_id: Optional[str] = None,
    ) -> Message:
        message = Message(
            topic=topic,
            payload=payload,
            source=source,
            message_id=message_id or str(uuid.uuid4()),
        )

        try:
            await asyncio.wait_for(
                self._queues[topic].put(message),
                timeout=1.0,
            )
        except asyncio.TimeoutError:
            logger.warning("Queue full for topic %s, dropping message", topic)
            raise

        logger.debug("Published to %s from %s", topic, source)
        return message

    async def _dispatch_message(self, message: Message) -> None:
        handlers = self._subscribers[message.topic].copy()
        if not handlers:
            return

        start_time = time.perf_counter()
        results = await asyncio.gather(
            *[self._safe_call(handler, message) for handler in handlers],
            return_exceptions=True,
        )

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        stats = self._stats[message.topic]
        stats.message_count += 1
        stats.total_latency_ms += elapsed_ms
        stats.last_message_time = datetime.now(timezone.utc)

        for result in results:
            if isinstance(result, Exception):
                logger.error("Handler error for %s: %s", message.topic, result)

    async def _safe_call(self, handler: MessageHandler, message: Message) -> None:
        try:
            await handler(message)
        except Exception as e:
            logger.error("Handler raised exception: %s", e, exc_info=True)
            raise

    async def process_once(self, topic: Optional[Topic] = None) -> int:
        processed = 0
        topics = [topic] if topic else list(Topic)

        for t in topics:
            try:
                message = self._queues[t].get_nowait()
                await self._dispatch_message(message)
                self._queues[t].task_done()
                processed += 1
            except asyncio.QueueEmpty:
                continue

        return processed

    async def _process_topic(self, topic: Topic) -> None:
        while self._running:
            try:
                message = await asyncio.wait_for(
                    self._queues[topic].get(), timeout=1.0
                )
                await self._dispatch_message(message)
                self._queues[topic].task_done()
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in %s processing loop: %s", topic, e)
                await asyncio.sleep(0.1)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        for topic in Topic:
            task = asyncio.create_task(
                self._process_topic(topic), name=f"bus_{topic.value}"
            )
            self._tasks.append(task)
        logger.info("MessageBus started with %d processing tasks", len(self._tasks))

    async def stop(self, timeout: float = 5.0) -> None:
        if not self._running:
            return
        self._running = False
        if self._tasks:
            done, pending = await asyncio.wait(self._tasks, timeout=timeout)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        logger.info("MessageBus stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    def get_statistics(self) -> Dict[str, Any]:
        topic_stats = {}
        total_messages = 0
        for topic in Topic:
            stats = self._stats[topic]
            topic_stats[str(topic)] = {
                "message_count": stats.message_count,
                "avg_latency_ms": round(stats.avg_latency_ms, 3),
                "subscriber_count": stats.subscriber_count,
                "queue_size": self._queues[topic].qsize(),
            }
            total_messages += stats.message_count
        return {"running": self._running, "topics": topic_stats, "total_messages": total_messages}
