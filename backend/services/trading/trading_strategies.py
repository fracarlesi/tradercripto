"""
Trading Strategies - Dynamic exit rules based on entry type.

This module classifies trading opportunities into different strategies
and applies appropriate exit rules (stop-loss, take-profit, time-based).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
from typing import Dict, Optional, Literal

logger = logging.getLogger(__name__)


StrategyType = Literal["MOMENTUM_BREAKOUT", "TECHNICAL_SPECULATION", "CONTRARIAN_VALUE", "BALANCED"]


@dataclass
class StrategyRules:
    """Exit rules for a specific trading strategy."""

    # Entry criteria
    min_score: float
    min_momentum: Optional[float] = None
    min_support: Optional[float] = None
    sentiment_max: Optional[int] = None
    prophet_trend: Optional[str] = None

    # Exit criteria (percentages as decimals: 0.08 = 8%)
    take_profit_pct: float = 0.05
    stop_loss_pct: float = -0.02
    max_hold_minutes: int = 240  # 4 hours default

    # Position sizing
    leverage: int = 1
    size_pct: float = 0.20  # 20% of portfolio


# Strategy definitions
STRATEGIES: Dict[StrategyType, StrategyRules] = {
    "MOMENTUM_BREAKOUT": StrategyRules(
        # High momentum (>0.85) + Moving fast
        min_score=0.60,
        min_momentum=0.85,
        take_profit_pct=0.02,      # +2% (quick momentum surfing)
        stop_loss_pct=-0.03,        # -3% (tight stop)
        max_hold_minutes=240,       # 4 hours
        leverage=3,
        size_pct=0.25,
    ),

    "TECHNICAL_SPECULATION": StrategyRules(
        # Good score (>0.65) + Good support (>0.70)
        min_score=0.65,
        min_support=0.70,
        take_profit_pct=0.02,      # +2% (quick scalp)
        stop_loss_pct=-0.015,       # -1.5% (very tight)
        max_hold_minutes=120,       # 2 hours
        leverage=2,
        size_pct=0.20,
    ),

    "CONTRARIAN_VALUE": StrategyRules(
        # Sentiment fear (<30) + Prophet bullish
        min_score=0.60,
        sentiment_max=30,           # Fear level
        prophet_trend="bullish",
        take_profit_pct=0.05,      # +5%
        stop_loss_pct=-0.02,        # -2%
        max_hold_minutes=480,       # 8 hours (longer hold)
        leverage=1,
        size_pct=0.15,
    ),

    "BALANCED": StrategyRules(
        # Default fallback strategy
        min_score=0.60,
        take_profit_pct=0.015,     # +1.5% (quick profits)
        stop_loss_pct=-0.02,        # -2%
        max_hold_minutes=180,       # 3 hours
        leverage=2,
        size_pct=0.20,
    ),
}


def classify_opportunity(
    technical_score: float,
    momentum: float,
    support: float,
    sentiment: Optional[int] = None,
    prophet_trend: Optional[str] = None,
) -> StrategyType:
    """
    Classify a trading opportunity into a strategy type.

    Args:
        technical_score: Overall technical analysis score (0-1)
        momentum: Momentum indicator (0-1)
        support: Support level strength (0-1)
        sentiment: Market sentiment (0-100, Fear & Greed Index)
        prophet_trend: Prophet forecast trend ("bullish", "bearish", "neutral")

    Returns:
        Strategy type that best matches the opportunity
    """

    # 1. Check MOMENTUM_BREAKOUT (highest priority)
    if momentum >= 0.85 and technical_score >= 0.60:
        logger.info(
            f"🚀 MOMENTUM_BREAKOUT detected: momentum={momentum:.2f}, score={technical_score:.2f}"
        )
        return "MOMENTUM_BREAKOUT"

    # 2. Check TECHNICAL_SPECULATION
    if technical_score >= 0.65 and support >= 0.70:
        logger.info(
            f"📊 TECHNICAL_SPECULATION detected: score={technical_score:.2f}, support={support:.2f}"
        )
        return "TECHNICAL_SPECULATION"

    # 3. Check CONTRARIAN_VALUE
    if (
        sentiment is not None
        and sentiment <= 30
        and prophet_trend == "bullish"
        and technical_score >= 0.60
    ):
        logger.info(
            f"💎 CONTRARIAN_VALUE detected: sentiment={sentiment}, prophet={prophet_trend}"
        )
        return "CONTRARIAN_VALUE"

    # 4. Default to BALANCED
    logger.info(f"⚖️ BALANCED strategy: score={technical_score:.2f}")
    return "BALANCED"


def get_strategy_rules(strategy_type: StrategyType) -> StrategyRules:
    """Get exit rules for a strategy type."""
    return STRATEGIES[strategy_type]


def should_exit_position(
    entry_price: float,
    current_price: float,
    entry_time: datetime,
    strategy_type: StrategyType,
    current_time: Optional[datetime] = None,
) -> tuple[bool, Optional[str]]:
    """
    Check if a position should be exited based on strategy rules.

    Args:
        entry_price: Price when position was opened
        current_price: Current market price
        entry_time: Timestamp when position was opened
        strategy_type: Strategy type of this position
        current_time: Current timestamp (defaults to now)

    Returns:
        Tuple of (should_exit: bool, reason: str)
    """

    if current_time is None:
        current_time = datetime.now(UTC)

    rules = get_strategy_rules(strategy_type)

    # Calculate P&L percentage
    pnl_pct = (current_price - entry_price) / entry_price

    # Calculate time held (in minutes)
    time_held = (current_time - entry_time).total_seconds() / 60

    # Check take-profit
    if pnl_pct >= rules.take_profit_pct:
        logger.info(
            f"✅ Take-profit hit: {pnl_pct*100:.2f}% >= {rules.take_profit_pct*100:.1f}% "
            f"(strategy: {strategy_type})"
        )
        return True, f"take_profit_{strategy_type}"

    # Check stop-loss
    if pnl_pct <= rules.stop_loss_pct:
        logger.warning(
            f"🛑 Stop-loss hit: {pnl_pct*100:.2f}% <= {rules.stop_loss_pct*100:.1f}% "
            f"(strategy: {strategy_type})"
        )
        return True, f"stop_loss_{strategy_type}"

    # Check time-based exit
    if time_held >= rules.max_hold_minutes:
        logger.info(
            f"⏰ Time-based exit: {time_held:.0f} min >= {rules.max_hold_minutes} min "
            f"(strategy: {strategy_type}, P&L: {pnl_pct*100:+.2f}%)"
        )
        return True, f"time_exit_{strategy_type}"

    # No exit criteria met
    return False, None


def format_strategy_summary(strategy_type: StrategyType) -> str:
    """Format a human-readable summary of a strategy's rules."""
    rules = get_strategy_rules(strategy_type)

    return (
        f"{strategy_type}: "
        f"TP={rules.take_profit_pct*100:+.1f}%, "
        f"SL={rules.stop_loss_pct*100:.1f}%, "
        f"Time={rules.max_hold_minutes}min, "
        f"Lev={rules.leverage}x, "
        f"Size={rules.size_pct*100:.0f}%"
    )


# Log strategy configurations on module load
def _log_strategies():
    """Log all configured strategies for debugging."""
    logger.info("="* 80)
    logger.info("📋 TRADING STRATEGIES CONFIGURED:")
    logger.info("="* 80)

    for strategy_type in STRATEGIES.keys():
        logger.info(f"  {format_strategy_summary(strategy_type)}")

    logger.info("="* 80)


# Log on import
_log_strategies()
