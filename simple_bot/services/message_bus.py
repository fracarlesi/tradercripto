"""
HLQuantBot Message Bus
======================

Async pub/sub message bus for inter-service communication.

Features:
- Topic-based routing with async queues
- Message dataclass with metadata (id, source, timestamp)
- Statistics tracking (message counts, latency)
- Structured logging for all messages

Usage:
    bus = MessageBus()
    
    # Subscribe to topics
    async def on_market_data(msg: Message):
        print(f"Received: {msg.payload}")
    
    await bus.subscribe(Topic.MARKET_DATA, on_market_data)
    
    # Publish messages
    await bus.publish(Topic.MARKET_DATA, {"price": 100.5}, source="market_data_service")
    
    # Get stats
    stats = bus.get_statistics()

Author: Francesco Carlesi
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# =============================================================================
# Topics
# =============================================================================

class Topic(str, Enum):
    """Available message topics for pub/sub routing.

    Conservative refactor topics:
    - MARKET_STATE: OHLCV + indicators per asset (replaces MARKET_DATA for new flow)
    - REGIME: TREND/RANGE/CHAOS detection
    - SETUPS: Trade setup candidates
    - TRADE_INTENT: Sized and approved trades
    - ORDERS: Orders sent to exchange
    - FILLS: Executed orders
    - RISK_ALERTS: Kill-switch, warnings
    - METRICS: Performance data

    Legacy topics (kept for backward compatibility):
    - MARKET_DATA: Raw market data (scanner output)
    - OPPORTUNITIES: Ranked opportunities
    - SIGNALS: Strategy signals
    - SIZED_SIGNALS: Sized signals
    - CONFIG_UPDATES: Configuration updates
    """

    # New topics (conservative refactor)
    MARKET_STATE = "market_state"
    REGIME = "regime"
    SETUPS = "setups"
    TRADE_INTENT = "trade_intent"
    RISK_ALERTS = "risk_alerts"

    # Core topics (both old and new)
    ORDERS = "orders"
    FILLS = "fills"
    METRICS = "metrics"

    # Legacy topics (backward compatibility)
    MARKET_DATA = "market_data"
    OPPORTUNITIES = "opportunities"
    SIGNALS = "signals"
    SIZED_SIGNALS = "sized_signals"
    CONFIG_UPDATES = "config_updates"

    def __str__(self) -> str:
        return self.value


# =============================================================================
# Message
# =============================================================================

@dataclass
class Message:
    """
    Immutable message passed through the bus.
    
    Attributes:
        topic: Message routing topic
        payload: Actual message data (dict, list, or primitive)
        timestamp: When the message was created (UTC)
        source: Service that published the message
        message_id: Unique identifier for tracing
    """
    topic: Topic
    payload: Any
    timestamp: datetime = field(default_factory=datetime.utcnow)
    source: str = "unknown"
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    
    def __post_init__(self) -> None:
        """Ensure topic is a Topic enum."""
        if isinstance(self.topic, str):
            self.topic = Topic(self.topic)
    
    def age_ms(self) -> float:
        """Calculate message age in milliseconds."""
        delta = datetime.utcnow() - self.timestamp
        return delta.total_seconds() * 1000
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize message to dictionary."""
        return {
            "topic": str(self.topic),
            "payload": self.payload,
            "timestamp": self.timestamp.isoformat(),
            "source": self.source,
            "message_id": self.message_id,
        }


# =============================================================================
# Type Aliases
# =============================================================================

MessageHandler = Callable[[Message], Coroutine[Any, Any, None]]


# =============================================================================
# Topic Statistics
# =============================================================================

@dataclass
class TopicStats:
    """Statistics for a single topic."""
    
    message_count: int = 0
    total_latency_ms: float = 0.0
    last_message_time: Optional[datetime] = None
    subscriber_count: int = 0
    
    @property
    def avg_latency_ms(self) -> float:
        """Average processing latency in milliseconds."""
        if self.message_count == 0:
            return 0.0
        return self.total_latency_ms / self.message_count


# =============================================================================
# Message Bus
# =============================================================================

class MessageBus:
    """
    Async pub/sub message bus with topic-based routing.
    
    Thread-safe for concurrent publishers and subscribers.
    Messages are processed asynchronously without blocking publishers.
    
    Example:
        bus = MessageBus()
        
        async def handler(msg: Message):
            print(f"Got {msg.topic}: {msg.payload}")
        
        await bus.subscribe(Topic.SIGNALS, handler)
        await bus.publish(Topic.SIGNALS, {"action": "buy"}, source="strategy")
        
        # Process pending messages
        await bus.process_once()
        
        # Or run continuous processing
        await bus.start()
    """
    
    def __init__(self, max_queue_size: int = 1000) -> None:
        """
        Initialize message bus.
        
        Args:
            max_queue_size: Maximum messages per topic queue before blocking
        """
        self._max_queue_size = max_queue_size
        self._queues: Dict[Topic, asyncio.Queue[Message]] = {}
        self._subscribers: Dict[Topic, Set[MessageHandler]] = {}
        self._stats: Dict[Topic, TopicStats] = {}
        self._running = False
        self._tasks: List[asyncio.Task] = []
        self._lock = asyncio.Lock()
        
        # Initialize queues and stats for all topics
        for topic in Topic:
            self._queues[topic] = asyncio.Queue(maxsize=max_queue_size)
            self._subscribers[topic] = set()
            self._stats[topic] = TopicStats()
        
        logger.info("MessageBus initialized with %d topics", len(Topic))
    
    # =========================================================================
    # Subscription Management
    # =========================================================================
    
    async def subscribe(
        self, 
        topic: Topic, 
        handler: MessageHandler
    ) -> None:
        """
        Subscribe a handler to a topic.
        
        Args:
            topic: Topic to subscribe to
            handler: Async callback function(Message) -> None
        """
        async with self._lock:
            self._subscribers[topic].add(handler)
            self._stats[topic].subscriber_count = len(self._subscribers[topic])
        
        logger.debug(
            "Handler subscribed to %s (total: %d)", 
            topic, 
            self._stats[topic].subscriber_count
        )
    
    async def unsubscribe(
        self, 
        topic: Topic, 
        handler: MessageHandler
    ) -> bool:
        """
        Unsubscribe a handler from a topic.
        
        Args:
            topic: Topic to unsubscribe from
            handler: Handler to remove
            
        Returns:
            True if handler was found and removed
        """
        async with self._lock:
            try:
                self._subscribers[topic].discard(handler)
                self._stats[topic].subscriber_count = len(self._subscribers[topic])
                logger.debug("Handler unsubscribed from %s", topic)
                return True
            except KeyError:
                return False
    
    async def unsubscribe_all(self, topic: Topic) -> int:
        """
        Remove all subscribers from a topic.
        
        Returns:
            Number of handlers removed
        """
        async with self._lock:
            count = len(self._subscribers[topic])
            self._subscribers[topic].clear()
            self._stats[topic].subscriber_count = 0
        
        logger.info("Removed %d subscribers from %s", count, topic)
        return count
    
    # =========================================================================
    # Publishing
    # =========================================================================
    
    async def publish(
        self,
        topic: Topic,
        payload: Any,
        source: str = "unknown",
        message_id: Optional[str] = None
    ) -> Message:
        """
        Publish a message to a topic.
        
        The message is queued and will be delivered to all subscribers
        when process_once() or the processing loop runs.
        
        Args:
            topic: Target topic
            payload: Message data
            source: Publishing service name
            message_id: Optional custom message ID
            
        Returns:
            The created Message object
        """
        message = Message(
            topic=topic,
            payload=payload,
            source=source,
            message_id=message_id or str(uuid.uuid4())
        )
        
        try:
            # Non-blocking put with timeout
            await asyncio.wait_for(
                self._queues[topic].put(message),
                timeout=1.0
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Queue full for topic %s, dropping message %s",
                topic,
                message.message_id
            )
            raise
        
        logger.debug(
            "Published to %s: %s (id=%s)",
            topic,
            type(payload).__name__,
            message.message_id[:8]
        )
        
        return message
    
    # =========================================================================
    # Message Processing
    # =========================================================================
    
    async def _dispatch_message(self, message: Message) -> None:
        """Dispatch a message to all subscribers of its topic."""
        handlers = self._subscribers[message.topic].copy()
        
        if not handlers:
            logger.debug("No subscribers for %s, message discarded", message.topic)
            return
        
        start_time = time.perf_counter()
        
        # Run all handlers concurrently
        results = await asyncio.gather(
            *[self._safe_call(handler, message) for handler in handlers],
            return_exceptions=True
        )
        
        # Track stats
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        stats = self._stats[message.topic]
        stats.message_count += 1
        stats.total_latency_ms += elapsed_ms
        stats.last_message_time = datetime.utcnow()
        
        # Log any exceptions
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(
                    "Handler error for %s: %s",
                    message.topic,
                    result,
                    exc_info=result
                )
    
    async def _safe_call(
        self, 
        handler: MessageHandler, 
        message: Message
    ) -> None:
        """Safely call a handler, catching exceptions."""
        try:
            await handler(message)
        except Exception as e:
            logger.error(
                "Handler %s raised exception: %s",
                handler.__name__,
                e,
                exc_info=True
            )
            raise
    
    async def process_once(self, topic: Optional[Topic] = None) -> int:
        """
        Process one message from each topic (or specified topic).
        
        Non-blocking: returns immediately if queues are empty.
        
        Args:
            topic: Optional specific topic to process
            
        Returns:
            Number of messages processed
        """
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
        """Continuous processing loop for a single topic."""
        logger.info("Started processing loop for %s", topic)
        
        while self._running:
            try:
                # Wait for message with timeout to check _running flag
                message = await asyncio.wait_for(
                    self._queues[topic].get(),
                    timeout=1.0
                )
                await self._dispatch_message(message)
                self._queues[topic].task_done()
                
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                logger.debug("Processing cancelled for %s", topic)
                break
            except Exception as e:
                logger.error("Error in %s processing loop: %s", topic, e)
                await asyncio.sleep(0.1)
        
        logger.info("Stopped processing loop for %s", topic)
    
    # =========================================================================
    # Lifecycle
    # =========================================================================
    
    async def start(self) -> None:
        """
        Start background processing for all topics.
        
        Creates one task per topic for concurrent processing.
        """
        if self._running:
            logger.warning("MessageBus already running")
            return
        
        self._running = True
        
        for topic in Topic:
            task = asyncio.create_task(
                self._process_topic(topic),
                name=f"bus_{topic.value}"
            )
            self._tasks.append(task)
        
        logger.info("MessageBus started with %d processing tasks", len(self._tasks))
    
    async def stop(self, timeout: float = 5.0) -> None:
        """
        Stop all processing and clean up.
        
        Args:
            timeout: Max seconds to wait for tasks to complete
        """
        if not self._running:
            return
        
        logger.info("Stopping MessageBus...")
        self._running = False
        
        # Wait for tasks to complete
        if self._tasks:
            done, pending = await asyncio.wait(
                self._tasks,
                timeout=timeout
            )
            
            for task in pending:
                task.cancel()
            
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        
        self._tasks.clear()
        logger.info("MessageBus stopped")
    
    async def drain(self, timeout: float = 10.0) -> int:
        """
        Process all pending messages before shutdown.
        
        Args:
            timeout: Max seconds to wait
            
        Returns:
            Number of messages drained
        """
        total = 0
        deadline = time.time() + timeout
        
        while time.time() < deadline:
            processed = await self.process_once()
            if processed == 0:
                break
            total += processed
        
        logger.info("Drained %d messages from bus", total)
        return total
    
    # =========================================================================
    # Statistics
    # =========================================================================
    
    def get_statistics(self) -> Dict[str, Any]:
        """
        Get comprehensive bus statistics.
        
        Returns:
            Dict with per-topic and aggregate stats
        """
        topic_stats = {}
        total_messages = 0
        total_latency = 0.0
        
        for topic in Topic:
            stats = self._stats[topic]
            queue_size = self._queues[topic].qsize()
            
            topic_stats[str(topic)] = {
                "message_count": stats.message_count,
                "avg_latency_ms": round(stats.avg_latency_ms, 3),
                "subscriber_count": stats.subscriber_count,
                "queue_size": queue_size,
                "last_message": (
                    stats.last_message_time.isoformat()
                    if stats.last_message_time
                    else None
                ),
            }
            
            total_messages += stats.message_count
            total_latency += stats.total_latency_ms
        
        return {
            "running": self._running,
            "topics": topic_stats,
            "total_messages": total_messages,
            "avg_latency_ms": (
                round(total_latency / total_messages, 3)
                if total_messages > 0
                else 0.0
            ),
        }
    
    def get_queue_sizes(self) -> Dict[str, int]:
        """Get current queue sizes for all topics."""
        return {str(topic): self._queues[topic].qsize() for topic in Topic}
    
    # =========================================================================
    # Properties
    # =========================================================================
    
    @property
    def is_running(self) -> bool:
        """Check if the bus is running."""
        return self._running
    
    @property
    def topics(self) -> List[Topic]:
        """Get list of available topics."""
        return list(Topic)
