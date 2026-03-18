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
    else:
        raise ValueError(
            f"Unknown strategy: '{name}'. "
            f"Valid options: 'orb', 'ema_momentum'"
        )
