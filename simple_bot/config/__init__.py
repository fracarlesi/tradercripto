"""
HLQuantBot v3.0 - Configuration Module

Usage:
    >>> from simple_bot.config import load_config, Config
    >>> config = load_config("path/to/config.yaml")
    >>> print(config.system.mode)
    testnet
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
    ExecutionEngineConfig,

    # Risk and LLM
    RiskConfig,
    LLMConfig,

    # Strategy configs
    StrategiesConfig,
    MomentumStrategyConfig,

    # Optional configs
    TelegramConfig,
    HealthConfig,

    # Utility
    resolve_env_vars,
)

__all__ = [
    "load_config",
    "get_config",
    "reload_config",
    "ConfigLoader",
    "Config",
    "SystemConfig",
    "HyperliquidConfig",
    "DatabaseConfig",
    "ServicesConfig",
    "ExecutionEngineConfig",
    "RiskConfig",
    "LLMConfig",
    "StrategiesConfig",
    "MomentumStrategyConfig",
    "TelegramConfig",
    "HealthConfig",
    "resolve_env_vars",
]

__version__ = "3.0.0"
