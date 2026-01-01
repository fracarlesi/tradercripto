"""
HLQuantBot v2.0 - Configuration Module

This module provides configuration loading and validation for HLQuantBot.

Usage:
    >>> from simple_bot.config import load_config, Config
    >>> config = load_config("path/to/config.yaml")
    >>> print(config.system.mode)
    testnet
    
    # Or use the default path
    >>> from simple_bot.config import load_config
    >>> config = load_config()
    
    # Hot-reload support
    >>> from simple_bot.config import reload_config, get_config
    >>> config = reload_config()  # Reload from file
    >>> config = get_config()     # Get current config
"""

from simple_bot.config.loader import (
    # Main entry points
    load_config,
    get_config,
    reload_config,
    
    # Loader class for advanced usage
    ConfigLoader,
    
    # Main config
    Config,
    
    # System configs
    SystemConfig,
    HyperliquidConfig,
    DatabaseConfig,
    
    # Service configs
    ServicesConfig,
    MarketScannerConfig,
    OpportunityRankerConfig,
    OpportunityWeights,
    StrategySelectorConfig,
    CapitalAllocatorConfig,
    ExecutionEngineConfig,
    LearningModuleConfig,
    
    # Risk and LLM
    RiskConfig,
    LLMConfig,
    
    # Strategy configs
    StrategiesConfig,
    MomentumStrategyConfig,
    MeanReversionStrategyConfig,
    BreakoutStrategyConfig,
    FundingArbStrategyConfig,
    
    # Optional configs
    TelegramConfig,
    HealthConfig,
    
    # Utility
    resolve_env_vars,
)

__all__ = [
    # Main entry points
    "load_config",
    "get_config",
    "reload_config",
    
    # Loader class
    "ConfigLoader",
    
    # Main config
    "Config",
    
    # System configs
    "SystemConfig",
    "HyperliquidConfig",
    "DatabaseConfig",
    
    # Service configs
    "ServicesConfig",
    "MarketScannerConfig",
    "OpportunityRankerConfig",
    "OpportunityWeights",
    "StrategySelectorConfig",
    "CapitalAllocatorConfig",
    "ExecutionEngineConfig",
    "LearningModuleConfig",
    
    # Risk and LLM
    "RiskConfig",
    "LLMConfig",
    
    # Strategy configs
    "StrategiesConfig",
    "MomentumStrategyConfig",
    "MeanReversionStrategyConfig",
    "BreakoutStrategyConfig",
    "FundingArbStrategyConfig",
    
    # Optional configs
    "TelegramConfig",
    "HealthConfig",
    
    # Utility
    "resolve_env_vars",
]

__version__ = "2.0.0"
