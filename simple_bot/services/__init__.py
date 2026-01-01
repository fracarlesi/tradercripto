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
    - StrategySelectorService: LLM-powered strategy selection
    - Signal: Trading signal dataclass

Example:
    from simple_bot.services import MessageBus, BaseService, Topic, Message

    # Create message bus
    bus = MessageBus()
    await bus.start()

    # Create a custom service
    class MyService(BaseService):
        async def _on_start(self) -> None:
            await self.subscribe(Topic.MARKET_DATA, self.on_data)
        
        async def _on_stop(self) -> None:
            pass
        
        async def on_data(self, msg: Message) -> None:
            print(f"Received: {msg.payload}")

    service = MyService(name="my_service", bus=bus)
    await service.start()
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

from .market_scanner import (
    MarketScannerService,
    CoinData,
    ScanMetrics,
    create_market_scanner,
)

from .opportunity_ranker import (
    OpportunityRankerService,
    OpportunityScore,
    SymbolMetrics,
)

from .capital_allocator import (
    CapitalAllocatorService,
    Position,
    AccountState,
    SizedSignal,
    kelly_size,
    atr_size,
    risk_parity_weight,
    create_capital_allocator,
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

from .strategy_selector import (
    StrategySelectorService,
    Signal,
    StrategyPerformance,
    create_strategy_selector,
)

from .learning_module import (
    LearningModuleService,
    StrategyMetrics,
    OptimizationResult,
    OptimizationCycle,
    create_learning_module,
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

from .kill_switch import (
    KillSwitchService,
    KillSwitchConfig,
    KillSwitchEvent,
    create_kill_switch,
)

from .llm_veto import (
    LLMVetoService,
    LLMVetoConfig,
    create_llm_veto,
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
    # Market Scanner
    "MarketScannerService",
    "CoinData",
    "ScanMetrics",
    "create_market_scanner",
    # Opportunity Ranker
    "OpportunityRankerService",
    "OpportunityScore",
    "SymbolMetrics",
    # Capital Allocator
    "CapitalAllocatorService",
    "Position",
    "AccountState",
    "SizedSignal",
    "kelly_size",
    "atr_size",
    "risk_parity_weight",
    "create_capital_allocator",
    # Execution Engine
    "ExecutionEngineService",
    "Order",
    "ExecutionPosition",
    "OrderStatus",
    "PositionStatus",
    "ExecutionMetrics",
    "create_execution_engine",
    # Strategy Selector
    "StrategySelectorService",
    "Signal",
    "StrategyPerformance",
    "create_strategy_selector",
    # Learning Module
    "LearningModuleService",
    "StrategyMetrics",
    "OptimizationResult",
    "OptimizationCycle",
    "create_learning_module",
    # Conservative System - Market State
    "MarketStateService",
    "MarketStateConfig",
    "create_market_state_service",
    # Conservative System - Risk Manager
    "RiskManagerService",
    "RiskConfig",
    "create_risk_manager",
    # Conservative System - Kill Switch
    "KillSwitchService",
    "KillSwitchConfig",
    "KillSwitchEvent",
    "create_kill_switch",
    # Conservative System - LLM Veto
    "LLMVetoService",
    "LLMVetoConfig",
    "create_llm_veto",
]

__version__ = "2.1.0"  # Conservative system update
