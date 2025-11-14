"""
Dynamic Weight Adjustments Storage and Management

Manages temporary adjustments to trading parameters based on
hourly retrospective analysis. Adjustments expire after N hours.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List
import json

logger = logging.getLogger(__name__)

# In-memory storage for active adjustments
# In production, this could be Redis or database table
_active_adjustments: List[Dict[str, Any]] = []


def apply_adjustment(
    adjustment_type: str,
    value: float,
    duration_hours: int,
    condition: Dict[str, Any],
    reason: str
) -> None:
    """
    Apply a dynamic adjustment that will be active for N hours.

    Args:
        adjustment_type: Type of adjustment (e.g., 'threshold_momentum', 'score_boost')
        value: The adjusted value (e.g., 0.70 for lowered threshold, 0.05 for boost)
        duration_hours: How long this adjustment remains active
        condition: Conditions for when to apply this adjustment
        reason: Human-readable reason for this adjustment
    """
    expires_at = datetime.utcnow() + timedelta(hours=duration_hours)

    adjustment = {
        'type': adjustment_type,
        'value': value,
        'condition': condition,
        'reason': reason,
        'created_at': datetime.utcnow(),
        'expires_at': expires_at,
        'applied_count': 0  # Track how many times this was applied
    }

    _active_adjustments.append(adjustment)

    logger.info(
        f"✅ Applied adjustment: {adjustment_type} = {value} "
        f"(expires in {duration_hours}h, condition: {condition})"
    )


def get_active_adjustments() -> List[Dict[str, Any]]:
    """
    Get all active (non-expired) adjustments.

    Returns:
        List of active adjustment dicts
    """
    now = datetime.utcnow()
    return [adj for adj in _active_adjustments if adj['expires_at'] > now]


def check_and_apply_adjustments(
    symbol: str,
    technical_data: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Check if any active adjustments should be applied to this symbol's technical data.

    Args:
        symbol: Symbol being analyzed
        technical_data: Dict with 'score', 'momentum', 'support', etc.

    Returns:
        Modified technical_data dict with adjustments applied
    """
    active = get_active_adjustments()

    if not active:
        return technical_data

    # Make a copy to avoid modifying original
    adjusted_data = technical_data.copy()
    adjustments_applied = []

    for adj in active:
        adj_type = adj['type']
        condition = adj['condition']

        # Check if conditions are met
        if not _check_condition(condition, technical_data):
            continue

        # Apply adjustment based on type
        if adj_type == 'threshold_momentum':
            # Lower threshold when momentum is high
            adjusted_data['threshold_override'] = adj['value']
            adj['applied_count'] += 1
            adjustments_applied.append(f"Threshold → {adj['value']}")

        elif adj_type == 'score_boost':
            # Boost score when specific conditions met
            original_score = adjusted_data.get('score', 0)
            adjusted_data['score'] = min(1.0, original_score + adj['value'])
            adj['applied_count'] += 1
            adjustments_applied.append(f"Score +{adj['value']} ({original_score:.2f} → {adjusted_data['score']:.2f})")

    if adjustments_applied:
        logger.info(
            f"⚡ {symbol}: Applied {len(adjustments_applied)} adjustments: "
            f"{', '.join(adjustments_applied)}"
        )

    return adjusted_data


def _check_condition(condition: Dict[str, Any], technical_data: Dict[str, Any]) -> bool:
    """
    Check if technical data meets the condition requirements.

    Args:
        condition: Dict with min/max requirements (e.g., {'momentum_min': 0.90})
        technical_data: Current technical data

    Returns:
        True if all conditions are met
    """
    for key, threshold in condition.items():
        if key.endswith('_min'):
            field = key.replace('_min', '')
            if technical_data.get(field, 0) < threshold:
                return False
        elif key.endswith('_max'):
            field = key.replace('_max', '')
            if technical_data.get(field, 0) > threshold:
                return False

    return True


def clear_expired_adjustments(max_age_hours: int = 6) -> int:
    """
    Remove expired adjustments from active list.

    Args:
        max_age_hours: Remove adjustments older than this (default: 6 hours)

    Returns:
        Number of adjustments removed
    """
    global _active_adjustments

    now = datetime.utcnow()
    cutoff = now - timedelta(hours=max_age_hours)

    # Keep only non-expired adjustments
    before_count = len(_active_adjustments)
    _active_adjustments = [
        adj for adj in _active_adjustments
        if adj['expires_at'] > now and adj['created_at'] > cutoff
    ]
    removed_count = before_count - len(_active_adjustments)

    if removed_count > 0:
        logger.info(f"🗑️ Cleared {removed_count} expired adjustments")

    return removed_count


def get_adjustment_summary() -> Dict[str, Any]:
    """
    Get summary of current adjustments for debugging/logging.

    Returns:
        Dict with active adjustments summary
    """
    active = get_active_adjustments()

    summary = {
        'total_active': len(active),
        'by_type': {},
        'adjustments': []
    }

    for adj in active:
        adj_type = adj['type']
        summary['by_type'][adj_type] = summary['by_type'].get(adj_type, 0) + 1

        time_left = adj['expires_at'] - datetime.utcnow()
        hours_left = time_left.total_seconds() / 3600

        summary['adjustments'].append({
            'type': adj_type,
            'value': adj['value'],
            'condition': adj['condition'],
            'hours_left': round(hours_left, 1),
            'applied_count': adj['applied_count'],
            'reason': adj['reason']
        })

    return summary


def log_adjustment_status():
    """Log current adjustment status for debugging."""
    summary = get_adjustment_summary()

    if summary['total_active'] == 0:
        logger.info("📊 No active dynamic adjustments")
        return

    logger.info(f"📊 {summary['total_active']} active dynamic adjustments:")
    for adj in summary['adjustments']:
        logger.info(
            f"  • {adj['type']}: {adj['value']} "
            f"({adj['hours_left']:.1f}h left, applied {adj['applied_count']} times)"
        )
