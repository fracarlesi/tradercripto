"""Trading strategies for HLQuantBot Phase C (HFT)."""

from .base import BaseStrategy

# Legacy strategies (disabled in Phase C, kept for backward compatibility)
from .funding_bias import FundingBiasStrategy
from .liquidation_cluster import LiquidationClusterStrategy
from .volatility_expansion import VolatilityExpansionStrategy

# HFT Strategies (Phase C - Active)
from .hft import (
    HFTBaseStrategy,
    MMRHFTStrategy,
    MicroBreakoutStrategy,
    PairTradingStrategy,
    LiquidationSnipingStrategy,
)

__all__ = [
    # Base
    "BaseStrategy",
    # Legacy (deprecated)
    "FundingBiasStrategy",
    "LiquidationClusterStrategy",
    "VolatilityExpansionStrategy",
    # HFT (Phase C)
    "HFTBaseStrategy",
    "MMRHFTStrategy",
    "MicroBreakoutStrategy",
    "PairTradingStrategy",
    "LiquidationSnipingStrategy",
]


# Strategy registry for easy lookup
STRATEGY_REGISTRY = {
    # Legacy
    "funding_bias": FundingBiasStrategy,
    "liquidation_cluster": LiquidationClusterStrategy,
    "volatility_expansion": VolatilityExpansionStrategy,
    # HFT
    "mmr_hft": MMRHFTStrategy,
    "micro_breakout": MicroBreakoutStrategy,
    "pair_trading": PairTradingStrategy,
    "liquidation_sniping": LiquidationSnipingStrategy,
}


def get_strategy_class(strategy_id: str):
    """Get strategy class by ID."""
    return STRATEGY_REGISTRY.get(strategy_id)
