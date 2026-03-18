"""
Strategy Registry
==================

Factory for creating strategy instances from config.
Maps strategy names to their implementation classes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import BaseStrategy

if TYPE_CHECKING:
    from ..config.loader import TradingConfig

logger = logging.getLogger(__name__)


def create_strategy(config: TradingConfig) -> BaseStrategy:
    """Create a strategy instance based on config.

    Args:
        config: Full trading configuration.

    Returns:
        Configured BaseStrategy instance.

    Raises:
        ValueError: If strategy name is unknown.
    """
    name = config.strategy.name

    if name == "orb":
        from .orb import ORBStrategy
        return ORBStrategy(
            strategy_config=config.strategy,
            stops_config=config.stops,
        )
    elif name == "ema_momentum":
        from .ema_momentum import EMAMomentumStrategy
        return EMAMomentumStrategy(
            ema_config=config.ema_strategy,
            stops_config=config.stops,
        )
    elif name == "rsi2_connors":
        from .rsi2_connors import RSI2ConnorsStrategy
        symbol = config.enabled_contracts[0].symbol if config.enabled_contracts else "MES"
        return RSI2ConnorsStrategy(
            rsi2_config=config.rsi2_connors,
            stops_config=config.stops,
            symbol=symbol,
        )
    else:
        raise ValueError(
            f"Unknown strategy: '{name}'. "
            f"Valid options: 'orb', 'ema_momentum', 'rsi2_connors'"
        )


def create_rsi_mean_reversion(config: TradingConfig):
    """Create RSI Mean Reversion strategy if enabled in config.

    Args:
        config: Full trading configuration.

    Returns:
        RSIMeanReversionStrategy instance or None if disabled.
    """
    if not config.rsi_mean_reversion.enabled:
        return None

    from .rsi_mean_reversion import RSIMeanReversionStrategy
    return RSIMeanReversionStrategy(
        rsi_mr_config=config.rsi_mean_reversion,
    )


def create_rsi2_connors(config: TradingConfig):
    """Create RSI(2) Connors daily strategy if enabled in config.

    Args:
        config: Full trading configuration.

    Returns:
        RSI2ConnorsStrategy instance or None if disabled.
    """
    if not config.rsi2_connors.enabled:
        return None

    from .rsi2_connors import RSI2ConnorsStrategy
    symbol = config.enabled_contracts[0].symbol if config.enabled_contracts else "MES"
    return RSI2ConnorsStrategy(
        rsi2_config=config.rsi2_connors,
        stops_config=config.stops,
        symbol=symbol,
    )
