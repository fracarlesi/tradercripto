"""
HLQuantBot v3.0 - BTC Momentum Scalper for Hyperliquid DEX
=============================================================

A focused trading bot with:
- EMA9/EMA21 momentum crossover strategy on 15m timeframe
- LLM-powered trade veto (DeepSeek)
- Strict risk management with kill switch
- Automated execution with TP/SL

Quick Start:
    from simple_bot import run_bot

    asyncio.run(run_bot())

Author: Francesco Carlesi
License: MIT
"""

__version__ = "3.0.0"
__author__ = "Francesco Carlesi"

# Config
from .config.loader import (
    Config,
    load_config,
    get_config,
    reload_config,
    ConfigLoader,
)

# API Client
from .api.hyperliquid import (
    HyperliquidClient,
    create_client as create_hyperliquid_client,
)


async def run_bot(config_path: str = "simple_bot/config/trading.yaml") -> None:
    """
    Run the HLQuantBot with the specified configuration.

    Args:
        config_path: Path to YAML configuration file
    """
    from .main import ConservativeBot

    bot = ConservativeBot(config_path=config_path)
    await bot.start()

    # Wait for shutdown
    import asyncio
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        await bot.stop()


def get_version() -> str:
    """Get the package version."""
    return __version__


__all__ = [
    "__version__",
    "__author__",
    "get_version",
    "run_bot",
    "Config",
    "load_config",
    "get_config",
    "reload_config",
    "ConfigLoader",
    "HyperliquidClient",
    "create_hyperliquid_client",
]
