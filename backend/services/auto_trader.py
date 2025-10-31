"""
Auto Trading Service - Main entry point for automated crypto trading
This file maintains backward compatibility while delegating to split services
"""

import logging

# Import from the new split services

logger = logging.getLogger(__name__)

# Constants
AI_TRADE_JOB_ID = "ai_crypto_trade"


# Backward compatibility - re-export main functions
# All the actual implementation is now in the split service files
# Paper trading removed - only AI-driven real trading supported


def place_ai_driven_crypto_order(max_ratio: float = 0.2) -> None:
    """Place AI-driven crypto order (stub for backward compatibility).

    Args:
        max_ratio: Maximum portion of portfolio to use per trade
    """
    logger.info(f"AI-driven crypto order (max_ratio={max_ratio}) - placeholder implementation")
    # TODO: Implement AI-driven trading logic or delegate to appropriate service
