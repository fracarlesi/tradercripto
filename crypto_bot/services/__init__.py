"""
HLQuantBot Services Module
==========================

Core infrastructure for the microservices architecture.

Exports:
    - MessageBus: Async pub/sub message bus
    - Message: Message dataclass with metadata
    - Topic: Enum of available message topics
    - BaseService: Abstract base class for services
    - ServiceStatus: Enum of service states
    - HealthStatus: Health check result dataclass
    - RetryConfig: Configuration for exponential backoff
"""

from .message_bus import (
    Message,
    MessageBus,
    Topic,
    TopicStats,
)

from .base import (
    BaseService,
    HealthStatus,
    RetryConfig,
    ServiceStatus,
)

from .execution_engine import (
    ExecutionEngineService,
    Order,
    ExecutionPosition,
    OrderStatus,
    PositionStatus,
    ExecutionMetrics,
    create_execution_engine,
)

# Conservative System Services
from .market_state import (
    MarketStateService,
    MarketStateConfig,
    create_market_state_service,
)

from .risk_manager import (
    RiskManagerService,
    RiskConfig,
    create_risk_manager,
)

__all__ = [
    # Message Bus
    "MessageBus",
    "Message",
    "Topic",
    "TopicStats",
    # Base Service
    "BaseService",
    "ServiceStatus",
    "HealthStatus",
    "RetryConfig",
    # Execution Engine
    "ExecutionEngineService",
    "Order",
    "ExecutionPosition",
    "OrderStatus",
    "PositionStatus",
    "ExecutionMetrics",
    "create_execution_engine",
    # Market State
    "MarketStateService",
    "MarketStateConfig",
    "create_market_state_service",
    # Risk Manager
    "RiskManagerService",
    "RiskConfig",
    "create_risk_manager",
]

__version__ = "3.0.0"  # Momentum scalper update
