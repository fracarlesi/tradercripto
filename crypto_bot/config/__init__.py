"""
HLQuantBot v3.0 - Configuration Module

Production path:
    >>> from crypto_bot.config import BotConfig
    >>> bot_config = BotConfig.from_yaml("crypto_bot/config/trading.yaml")

Phase 4 cleanup: the legacy ``Config`` / ``ConfigLoader`` / ``load_config``
helpers were unused in production and have been removed. The remaining
nested models (SystemConfig, RiskConfig, LLMConfig, ...) are still
exported for callers that import them directly.
"""

from crypto_bot.config.loader import (
    # Production runtime config
    BotConfig,
    BotExecutionConfig,
    BotStopsConfig,
    BotRiskConfig,
    BotMomentumExitConfig,
    BotRegimeConfig,
    BotServicesConfig,

    # Standalone nested models (still used by callers / tests)
    SystemConfig,
    HyperliquidConfig,
    RiskConfig,
    LLMConfig,
    StrategiesConfig,
    MomentumStrategyConfig,
    TelegramConfig,
    HealthConfig,

    # Utility
    resolve_env_vars,
)

__all__ = [
    "BotConfig",
    "BotExecutionConfig",
    "BotStopsConfig",
    "BotRiskConfig",
    "BotMomentumExitConfig",
    "BotRegimeConfig",
    "BotServicesConfig",
    "SystemConfig",
    "HyperliquidConfig",
    "RiskConfig",
    "LLMConfig",
    "StrategiesConfig",
    "MomentumStrategyConfig",
    "TelegramConfig",
    "HealthConfig",
    "resolve_env_vars",
]

__version__ = "3.0.0"
