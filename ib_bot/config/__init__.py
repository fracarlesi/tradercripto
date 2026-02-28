"""
IB Trading Bot - Configuration Module

Usage:
    >>> from ib_bot.config import load_config, TradingConfig
    >>> config = load_config()
    >>> print(config.strategy.name)
    orb
"""

from ib_bot.config.loader import (
    # Main entry point
    load_config,

    # Root config
    TradingConfig,

    # Section configs
    IBConnectionConfig,
    ContractConfig,
    OpeningRangeConfig,
    StrategyConfig,
    StopsConfig,
    RiskConfig,
    NotificationsConfig,
    LoggingConfig,

    # Utility
    resolve_env_vars,
)

__all__ = [
    "load_config",
    "TradingConfig",
    "IBConnectionConfig",
    "ContractConfig",
    "OpeningRangeConfig",
    "StrategyConfig",
    "StopsConfig",
    "RiskConfig",
    "NotificationsConfig",
    "LoggingConfig",
    "resolve_env_vars",
]
