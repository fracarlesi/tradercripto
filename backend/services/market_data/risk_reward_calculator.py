"""
Risk/Reward Calculator - Validate trades meet minimum R:R ratio

Based on Mohsen Hassan's strategy:
- Never take trades with R:R < 1:2
- Use pivot points for stop loss and take profit levels
- Calculate potential reward vs risk before entry

Risk = Entry - Stop Loss
Reward = Take Profit - Entry
R:R = Reward / Risk

Example:
- Entry: $100
- Stop Loss: $98 (Risk = $2)
- Take Profit: $106 (Reward = $6)
- R:R = 6/2 = 3:1 (GOOD - exceeds 1:2 minimum)
"""

import logging
from typing import Dict, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class RiskRewardData:
    """Risk/Reward analysis for a trade setup."""
    symbol: str
    direction: str  # "LONG" or "SHORT"
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_amount: float  # Entry - SL (for LONG)
    reward_amount: float  # TP - Entry (for LONG)
    risk_reward_ratio: float  # Reward / Risk
    meets_minimum: bool  # R:R >= minimum_ratio
    risk_pct: float  # Risk as % of entry
    reward_pct: float  # Reward as % of entry


def calculate_risk_reward(
    symbol: str,
    direction: str,
    entry_price: float,
    pivot_points: Dict[str, float],
    min_ratio: float = 2.0,
) -> RiskRewardData:
    """
    Calculate Risk/Reward ratio using pivot points.

    For LONG:
    - Stop Loss = S1 or S2 (below entry)
    - Take Profit = R1 or R2 (above entry)

    For SHORT:
    - Stop Loss = R1 or R2 (above entry)
    - Take Profit = S1 or S2 (below entry)

    Args:
        symbol: Trading symbol
        direction: "LONG" or "SHORT"
        entry_price: Current/entry price
        pivot_points: Dict with PP, S1, S2, S3, R1, R2, R3
        min_ratio: Minimum R:R ratio (default 2.0 = 1:2)

    Returns:
        RiskRewardData with analysis
    """
    PP = pivot_points.get('PP', entry_price)
    S1 = pivot_points.get('S1', entry_price * 0.98)
    S2 = pivot_points.get('S2', entry_price * 0.96)
    S3 = pivot_points.get('S3', entry_price * 0.94)
    R1 = pivot_points.get('R1', entry_price * 1.02)
    R2 = pivot_points.get('R2', entry_price * 1.04)
    R3 = pivot_points.get('R3', entry_price * 1.06)

    if direction == "LONG":
        # LONG: Stop at support, Target at resistance
        # Use S1 for stop loss (closest support)
        stop_loss = S1

        # Calculate target based on risk
        risk = entry_price - stop_loss

        # For R:R 1:2, we need reward = 2 * risk
        min_target = entry_price + (risk * min_ratio)

        # Use R1 or R2 based on which meets minimum
        if R1 >= min_target:
            take_profit = R1
        elif R2 >= min_target:
            take_profit = R2
        else:
            take_profit = R3  # Use R3 even if doesn't meet minimum

        risk_amount = entry_price - stop_loss
        reward_amount = take_profit - entry_price

    else:  # SHORT
        # SHORT: Stop at resistance, Target at support
        # Use R1 for stop loss (closest resistance)
        stop_loss = R1

        # Calculate target based on risk
        risk = stop_loss - entry_price

        # For R:R 1:2, we need reward = 2 * risk
        min_target = entry_price - (risk * min_ratio)

        # Use S1 or S2 based on which meets minimum
        if S1 <= min_target:
            take_profit = S1
        elif S2 <= min_target:
            take_profit = S2
        else:
            take_profit = S3  # Use S3 even if doesn't meet minimum

        risk_amount = stop_loss - entry_price
        reward_amount = entry_price - take_profit

    # Prevent division by zero
    if risk_amount <= 0:
        risk_amount = entry_price * 0.02  # Default 2% risk

    risk_reward_ratio = reward_amount / risk_amount
    meets_minimum = risk_reward_ratio >= min_ratio

    # Calculate percentages
    risk_pct = (risk_amount / entry_price) * 100
    reward_pct = (reward_amount / entry_price) * 100

    return RiskRewardData(
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        risk_amount=risk_amount,
        reward_amount=reward_amount,
        risk_reward_ratio=risk_reward_ratio,
        meets_minimum=meets_minimum,
        risk_pct=risk_pct,
        reward_pct=reward_pct,
    )


def calculate_optimal_entry_for_rr(
    symbol: str,
    direction: str,
    current_price: float,
    pivot_points: Dict[str, float],
    min_ratio: float = 2.0,
) -> Optional[Dict]:
    """
    Calculate optimal entry price to achieve minimum R:R ratio.

    Sometimes current price doesn't offer good R:R.
    This function calculates where to place limit order.

    Args:
        symbol: Trading symbol
        direction: "LONG" or "SHORT"
        current_price: Current market price
        pivot_points: Dict with PP, S1, S2, S3, R1, R2, R3
        min_ratio: Minimum R:R ratio (default 2.0)

    Returns:
        Dict with optimal entry info or None if no good entry exists
    """
    PP = pivot_points.get('PP', current_price)
    S1 = pivot_points.get('S1', current_price * 0.98)
    S2 = pivot_points.get('S2', current_price * 0.96)
    R1 = pivot_points.get('R1', current_price * 1.02)
    R2 = pivot_points.get('R2', current_price * 1.04)

    if direction == "LONG":
        # For LONG, calculate entry that gives R:R >= 2
        # Using R1 as target and S1 as stop
        # Entry needs to be: Entry = (R1 + min_ratio * S1) / (1 + min_ratio)
        optimal_entry = (R1 + min_ratio * S1) / (1 + min_ratio)

        # Entry must be above S1 (stop loss)
        if optimal_entry <= S1:
            # Can't achieve R:R with these levels
            return None

        distance_from_current = ((optimal_entry - current_price) / current_price) * 100

        return {
            "symbol": symbol,
            "direction": direction,
            "optimal_entry": optimal_entry,
            "current_price": current_price,
            "distance_pct": distance_from_current,
            "stop_loss": S1,
            "take_profit": R1,
            "achievable_rr": (R1 - optimal_entry) / (optimal_entry - S1),
        }

    else:  # SHORT
        # For SHORT, calculate entry that gives R:R >= 2
        # Using S1 as target and R1 as stop
        optimal_entry = (S1 + min_ratio * R1) / (1 + min_ratio)

        # Entry must be below R1 (stop loss)
        if optimal_entry >= R1:
            return None

        distance_from_current = ((optimal_entry - current_price) / current_price) * 100

        return {
            "symbol": symbol,
            "direction": direction,
            "optimal_entry": optimal_entry,
            "current_price": current_price,
            "distance_pct": distance_from_current,
            "stop_loss": R1,
            "take_profit": S1,
            "achievable_rr": (optimal_entry - S1) / (R1 - optimal_entry),
        }


def validate_trade_risk_reward(
    symbol: str,
    direction: str,
    entry_price: float,
    pivot_points: Dict[str, float],
    min_ratio: float = 2.0,
) -> Dict:
    """
    Validate if trade meets minimum R:R requirements.

    This is the main function to call before trade execution.

    Args:
        symbol: Trading symbol
        direction: "LONG" or "SHORT"
        entry_price: Entry price
        pivot_points: Pivot point levels
        min_ratio: Minimum R:R ratio (default 2.0)

    Returns:
        Dict with validation result:
        {
            "valid": bool,
            "rr_ratio": float,
            "stop_loss": float,
            "take_profit": float,
            "risk_pct": float,
            "reward_pct": float,
            "message": str,
        }
    """
    rr_data = calculate_risk_reward(
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        pivot_points=pivot_points,
        min_ratio=min_ratio,
    )

    if rr_data.meets_minimum:
        message = (
            f"APPROVED: R:R {rr_data.risk_reward_ratio:.1f}:1 "
            f"(risk={rr_data.risk_pct:.1f}%, reward={rr_data.reward_pct:.1f}%)"
        )
        logger.info(f"R:R VALID {symbol} {direction}: {message}")
    else:
        message = (
            f"REJECTED: R:R {rr_data.risk_reward_ratio:.1f}:1 < {min_ratio}:1 minimum "
            f"(risk={rr_data.risk_pct:.1f}%, reward={rr_data.reward_pct:.1f}%)"
        )
        logger.warning(f"R:R INVALID {symbol} {direction}: {message}")

    return {
        "valid": rr_data.meets_minimum,
        "rr_ratio": rr_data.risk_reward_ratio,
        "stop_loss": rr_data.stop_loss,
        "take_profit": rr_data.take_profit,
        "risk_pct": rr_data.risk_pct,
        "reward_pct": rr_data.reward_pct,
        "message": message,
        "risk_amount": rr_data.risk_amount,
        "reward_amount": rr_data.reward_amount,
    }


def get_rr_score_for_decision(
    symbol: str,
    direction: str,
    entry_price: float,
    pivot_points: Dict[str, float],
) -> float:
    """
    Get R:R score for AI decision weighting.

    Returns score 0.0-1.0 based on R:R quality:
    - R:R >= 3:1 = 1.0 (excellent)
    - R:R >= 2:1 = 0.8 (good)
    - R:R >= 1.5:1 = 0.5 (marginal)
    - R:R < 1.5:1 = 0.2 (poor)
    - R:R < 1:1 = 0.0 (unacceptable)

    Args:
        symbol: Trading symbol
        direction: "LONG" or "SHORT"
        entry_price: Entry price
        pivot_points: Pivot point levels

    Returns:
        Score 0.0-1.0
    """
    rr_data = calculate_risk_reward(
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        pivot_points=pivot_points,
        min_ratio=2.0,
    )

    ratio = rr_data.risk_reward_ratio

    if ratio >= 3.0:
        return 1.0
    elif ratio >= 2.5:
        return 0.9
    elif ratio >= 2.0:
        return 0.8
    elif ratio >= 1.5:
        return 0.5
    elif ratio >= 1.0:
        return 0.2
    else:
        return 0.0


def format_rr_for_prompt(
    symbol: str,
    direction: str,
    entry_price: float,
    pivot_points: Dict[str, float],
) -> str:
    """
    Format R:R data for inclusion in DeepSeek prompt.

    Args:
        symbol: Trading symbol
        direction: "LONG" or "SHORT"
        entry_price: Entry price
        pivot_points: Pivot point levels

    Returns:
        Formatted string for prompt
    """
    rr_data = calculate_risk_reward(
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        pivot_points=pivot_points,
        min_ratio=2.0,
    )

    status = "APPROVED" if rr_data.meets_minimum else "REJECTED"

    return (
        f"Risk/Reward Analysis ({symbol} {direction}):\n"
        f"  - Entry: ${entry_price:.2f}\n"
        f"  - Stop Loss: ${rr_data.stop_loss:.2f} (risk: {rr_data.risk_pct:.1f}%)\n"
        f"  - Take Profit: ${rr_data.take_profit:.2f} (reward: {rr_data.reward_pct:.1f}%)\n"
        f"  - R:R Ratio: {rr_data.risk_reward_ratio:.1f}:1 [{status}]\n"
    )
