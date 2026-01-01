"""
HLQuantBot v2.0 - Intelligent Trading Bot for Hyperliquid DEX
=============================================================

A microservices-based trading bot with:
- Market scanning and opportunity detection
- LLM-powered strategy selection (DeepSeek)
- Dynamic capital allocation with Kelly criterion
- Automated execution with slippage control
- Continuous learning and optimization

Quick Start:
    from simple_bot import run_bot
    
    # Run with default config
    asyncio.run(run_bot())
    
    # Or with custom config path
    asyncio.run(run_bot("path/to/config.yaml"))

Services:
    - MarketScannerService: Scans market for trading opportunities
    - OpportunityRankerService: Ranks opportunities by multiple factors
    - StrategySelectorService: LLM-based strategy selection
    - CapitalAllocatorService: Position sizing and risk management
    - ExecutionEngineService: Order execution with TP/SL
    - LearningModuleService: Performance tracking and optimization

Architecture:
    +-----------------+
    | MarketScanner   |
    +-------+---------+
            |
            v
    +-----------------+
    | OpportunityRank |
    +-------+---------+
            |
            v
    +-----------------+
    | StrategySelect  |<---> DeepSeek LLM
    +-------+---------+
            |
            v
    +-----------------+
    | CapitalAlloc    |
    +-------+---------+
            |
            v
    +-----------------+
    | ExecutionEngine |<---> Hyperliquid API
    +-------+---------+
            |
            v
    +-----------------+
    | LearningModule  |
    +-----------------+

Author: Francesco Carlesi
License: MIT
"""

__version__ = "2.0.0"
__author__ = "Francesco Carlesi"

# =============================================================================
# Package Exports
# =============================================================================

# Services
from .services import (
    # Message Bus
    MessageBus,
    Message,
    Topic,
    TopicStats,
    # Base Service
    BaseService,
    ServiceStatus,
    HealthStatus,
    RetryConfig,
    # Market Scanner
    MarketScannerService,
    CoinData,
    ScanMetrics,
    create_market_scanner,
    # Opportunity Ranker
    OpportunityRankerService,
    OpportunityScore,
    SymbolMetrics,
    # Capital Allocator
    CapitalAllocatorService,
    Position,
    AccountState,
    SizedSignal,
    kelly_size,
    atr_size,
    risk_parity_weight,
    create_capital_allocator,
    # Execution Engine
    ExecutionEngineService,
    Order,
    ExecutionPosition,
    OrderStatus,
    PositionStatus,
    ExecutionMetrics,
    create_execution_engine,
    # Strategy Selector
    StrategySelectorService,
    Signal,
    StrategyPerformance,
    create_strategy_selector,
    # Learning Module
    LearningModuleService,
    StrategyMetrics,
    OptimizationResult,
    OptimizationCycle,
    create_learning_module,
)

# Config
from .config.loader import (
    Config,
    load_config,
    get_config,
    reload_config,
    ConfigLoader,
)

# LLM Client
from .llm.client import (
    DeepSeekClient,
    StrategyDecision,
    MarketAnalysis,
    StrategyType,
    DirectionType,
    create_deepseek_client,
)

# API Client
from .api.hyperliquid import (
    HyperliquidClient,
    create_client as create_hyperliquid_client,
)


# =============================================================================
# Convenience Functions
# =============================================================================

async def run_bot(config_path: str = "simple_bot/config/intelligent_bot.yaml") -> None:
    """
    Run the HLQuantBot with the specified configuration.
    
    This is the main entry point for running the bot.
    Handles initialization, running, and graceful shutdown.
    
    Args:
        config_path: Path to YAML configuration file
        
    Example:
        import asyncio
        from simple_bot import run_bot
        
        asyncio.run(run_bot())
    """
    from .main import HLQuantBot
    
    bot = HLQuantBot(config_path=config_path)
    await bot.run()


def get_version() -> str:
    """Get the package version."""
    return __version__


__all__ = [
    # Version
    "__version__",
    "__author__",
    "get_version",
    # Main entry point
    "run_bot",
    # Services
    "MessageBus",
    "Message",
    "Topic",
    "TopicStats",
    "BaseService",
    "ServiceStatus",
    "HealthStatus",
    "RetryConfig",
    "MarketScannerService",
    "CoinData",
    "ScanMetrics",
    "create_market_scanner",
    "OpportunityRankerService",
    "OpportunityScore",
    "SymbolMetrics",
    "CapitalAllocatorService",
    "Position",
    "AccountState",
    "SizedSignal",
    "kelly_size",
    "atr_size",
    "risk_parity_weight",
    "create_capital_allocator",
    "ExecutionEngineService",
    "Order",
    "ExecutionPosition",
    "OrderStatus",
    "PositionStatus",
    "ExecutionMetrics",
    "create_execution_engine",
    "StrategySelectorService",
    "Signal",
    "StrategyPerformance",
    "create_strategy_selector",
    "LearningModuleService",
    "StrategyMetrics",
    "OptimizationResult",
    "OptimizationCycle",
    "create_learning_module",
    # Config
    "Config",
    "load_config",
    "get_config",
    "reload_config",
    "ConfigLoader",
    # LLM
    "DeepSeekClient",
    "StrategyDecision",
    "MarketAnalysis",
    "StrategyType",
    "DirectionType",
    "create_deepseek_client",
    # API
    "HyperliquidClient",
    "create_hyperliquid_client",
]
