"""Core models, enums and exceptions for HLQuantBot."""

from .enums import (
    Environment,
    Side,
    OrderType,
    OrderStatus,
    PositionStatus,
    StrategyId,
    MarketRegime,
    AlertSeverity,
)
from .models import (
    Tick,
    Bar,
    ProposedTrade,
    ApprovedOrder,
    Position,
    AccountState,
    StrategyMetrics,
    RiskLimits,
)
from .exceptions import (
    HLQuantBotError,
    ConfigurationError,
    InsufficientBalanceError,
    RiskLimitExceededError,
    ExecutionError,
    DataFeedError,
    CircuitBreakerTriggeredError,
)

__all__ = [
    # Enums
    "Environment",
    "Side",
    "OrderType",
    "OrderStatus",
    "PositionStatus",
    "StrategyId",
    "MarketRegime",
    "AlertSeverity",
    # Models
    "Tick",
    "Bar",
    "ProposedTrade",
    "ApprovedOrder",
    "Position",
    "AccountState",
    "StrategyMetrics",
    "RiskLimits",
    # Exceptions
    "HLQuantBotError",
    "ConfigurationError",
    "InsufficientBalanceError",
    "RiskLimitExceededError",
    "ExecutionError",
    "DataFeedError",
    "CircuitBreakerTriggeredError",
]
