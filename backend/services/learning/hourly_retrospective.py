"""
Hourly Market Retrospective - Continuous Real-Time Learning

Every hour:
1. Analyzes top 5 gainers and top 5 losers in the last hour
2. Checks if AI spotted these opportunities
3. Identifies root causes for missed opportunities
4. Auto-adjusts weights/thresholds for next decisions

This provides IMMEDIATE feedback (1h vs 24h) and proactive corrections.
"""

import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any
import asyncio

from services.market_data.websocket_candle_service import get_websocket_candle_service
from services.learning.weight_adjustments import (
    get_active_adjustments,
    apply_adjustment,
    clear_expired_adjustments
)
from database.connection import get_async_session_factory
from database.models import DecisionSnapshot, PendingStrategySuggestion

logger = logging.getLogger(__name__)


async def analyze_hourly_market() -> Dict[str, Any]:
    """
    Main hourly retrospective analysis.

    Returns:
        Analysis results with missed opportunities and applied corrections
    """
    logger.info("=" * 60)
    logger.info("🔍 HOURLY MARKET RETROSPECTIVE")
    logger.info("=" * 60)

    try:
        # 1. Get top movers in last hour
        winners = await get_top_movers(direction="up", limit=5)
        losers = await get_top_movers(direction="down", limit=5)

        winner_str = ', '.join([f"{w['symbol']} (+{w['return_pct']:.1f}%)" for w in winners])
        loser_str = ', '.join([f"{l['symbol']} ({l['return_pct']:.1f}%)" for l in losers])
        logger.info(f"📈 Top 5 winners: {winner_str}")
        logger.info(f"📉 Top 5 losers: {loser_str}")

        # 2. Get AI decision from 1 hour ago
        ai_decision = await get_decision_from_1h_ago()

        if not ai_decision:
            logger.warning("No AI decision from 1h ago - system might have just started")
            return {"status": "no_data"}

        # 3. Analyze missed opportunities (winners AI didn't trade)
        missed_winners = []
        for winner in winners:
            if not was_traded_by_ai(winner['symbol'], ai_decision):
                analysis = analyze_missed_opportunity(
                    symbol=winner['symbol'],
                    return_pct=winner['return_pct'],
                    technical_data=winner.get('technical', {}),
                    ai_decision=ai_decision
                )
                missed_winners.append(analysis)

        # 4. Analyze avoided losses (losers AI didn't trade or shorted)
        avoided_losses = []
        for loser in losers:
            if not was_traded_by_ai(loser['symbol'], ai_decision):
                avoided_losses.append({
                    'symbol': loser['symbol'],
                    'return_pct': loser['return_pct'],
                    'avoided_loss': abs(loser['return_pct'])
                })

        # 5. Save suggestions for manual review (NO auto-apply)
        suggestions_saved = await save_suggestions_for_review(missed_winners)

        # 6. Clear expired adjustments (older than 6 hours) - keep for cleanup
        clear_expired_adjustments(max_age_hours=6)

        # Log summary
        logger.info("=" * 60)
        logger.info(f"📊 SUMMARY:")
        logger.info(f"  • Missed opportunities: {len(missed_winners)}")
        logger.info(f"  • Avoided losses: {len(avoided_losses)}")
        logger.info(f"  • Suggestions saved for review: {len(suggestions_saved)}")
        logger.info("=" * 60)

        return {
            "status": "success",
            "missed_winners": missed_winners,
            "avoided_losses": avoided_losses,
            "suggestions_saved": suggestions_saved
        }

    except Exception as e:
        logger.error(f"Hourly retrospective failed: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}


async def get_top_movers(direction: str = "up", limit: int = 5) -> List[Dict[str, Any]]:
    """
    Get top gainers or losers in the last hour using WebSocket cache.

    Args:
        direction: "up" for gainers, "down" for losers
        limit: Number of top movers to return

    Returns:
        List of dicts with symbol, return_pct, technical data
    """
    ws_service = get_websocket_candle_service()
    all_symbols = ws_service.subscribed_symbols

    movers = []

    for symbol in all_symbols:
        try:
            # Get last 2 candles (current hour + previous hour)
            candles = ws_service.get_candles(symbol, limit=2)

            if len(candles) < 2:
                continue

            # Calculate 1-hour return
            current_close = float(candles[0]['c'])
            hour_ago_open = float(candles[1]['o'])

            if hour_ago_open == 0:
                continue

            return_pct = ((current_close - hour_ago_open) / hour_ago_open) * 100

            # Filter by direction
            if direction == "up" and return_pct <= 0:
                continue
            if direction == "down" and return_pct >= 0:
                continue

            movers.append({
                'symbol': symbol,
                'return_pct': return_pct,
                'current_price': current_close,
                'hour_ago_price': hour_ago_open,
                'volume': float(candles[0]['v'])
            })

        except Exception as e:
            logger.debug(f"Error calculating return for {symbol}: {e}")
            continue

    # Sort by absolute return
    movers.sort(key=lambda x: abs(x['return_pct']), reverse=True)

    return movers[:limit]


async def get_decision_from_1h_ago() -> Dict[str, Any] | None:
    """
    Get the AI decision made 1 hour ago from decision_snapshots table.

    Returns:
        Dict with decision data or None if not found
    """
    one_hour_ago = datetime.utcnow() - timedelta(hours=1)
    # Allow 15-minute window (AI runs every 3 min, so we should find one)
    window_start = one_hour_ago - timedelta(minutes=15)
    window_end = one_hour_ago + timedelta(minutes=15)

    async with get_async_session_factory()() as session:
        from sqlalchemy import select

        stmt = select(DecisionSnapshot).where(
            DecisionSnapshot.timestamp >= window_start,
            DecisionSnapshot.timestamp <= window_end
        ).order_by(DecisionSnapshot.timestamp.desc()).limit(1)

        result = await session.execute(stmt)
        snapshot = result.scalar_one_or_none()

        if not snapshot:
            return None

        return {
            'timestamp': snapshot.timestamp,
            'decision': snapshot.actual_decision,
            'symbol': snapshot.symbol,
            'operation': snapshot.actual_decision.lower() if snapshot.actual_decision else 'hold',
            'indicators': snapshot.indicators_snapshot
        }


def was_traded_by_ai(symbol: str, ai_decision: Dict[str, Any]) -> bool:
    """Check if symbol was traded in the AI decision."""
    if not ai_decision:
        return False

    return ai_decision.get('symbol') == symbol and ai_decision.get('operation') in ['long', 'short']


def analyze_missed_opportunity(
    symbol: str,
    return_pct: float,
    technical_data: Dict[str, Any],
    ai_decision: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Analyze why AI missed this opportunity.

    Returns:
        Dict with root cause analysis and suggested corrections
    """
    import json

    # Parse indicators from AI decision
    indicators = {}
    if ai_decision.get('indicators'):
        try:
            indicators = json.loads(ai_decision['indicators']) if isinstance(ai_decision['indicators'], str) else ai_decision['indicators']
        except:
            pass

    # Get technical factors for this symbol
    tech = indicators.get('technical_factors', {})
    recommendations = tech.get('recommendations', [])

    symbol_tech = None
    for rec in recommendations:
        if rec.get('symbol') == symbol:
            symbol_tech = rec
            break

    if not symbol_tech:
        return {
            'symbol': symbol,
            'return_pct': return_pct,
            'root_cause': 'NOT_IN_TOP_ANALYSIS',
            'potential_profit': 0
        }

    score = symbol_tech.get('score', 0)
    momentum = symbol_tech.get('momentum', 0)
    support = symbol_tech.get('support', 0)

    # Calculate potential profit (50% position size)
    # Assuming account value ~$187
    potential_profit = (187 * 0.5) * (return_pct / 100)

    # Determine root cause
    root_cause = "UNKNOWN"
    correction = None

    if score < 0.75:
        root_cause = "SCORE_TOO_LOW"
        if momentum > 0.90:
            correction = {
                'type': 'LOWER_THRESHOLD_HIGH_MOMENTUM',
                'reason': f'Score {score:.2f} < 0.75 but momentum {momentum:.2f} > 0.90',
                'action': 'Lower threshold to 0.70 when momentum > 0.90'
            }
        elif momentum > 0.85 and support > 0.70:
            correction = {
                'type': 'BOOST_SCORE_MOMENTUM_SUPPORT',
                'reason': f'Score {score:.2f} close to threshold, momentum {momentum:.2f}, support {support:.2f}',
                'action': 'Boost score by +0.05 when momentum > 0.85 AND support > 0.70'
            }

    logger.info(
        f"❌ MISSED: {symbol} +{return_pct:.1f}% "
        f"(score: {score:.2f}, momentum: {momentum:.2f}, support: {support:.2f}) "
        f"→ Lost ${potential_profit:.2f}"
    )

    if correction:
        logger.info(f"   💡 CORRECTION: {correction['action']}")

    return {
        'symbol': symbol,
        'return_pct': return_pct,
        'score': score,
        'momentum': momentum,
        'support': support,
        'root_cause': root_cause,
        'potential_profit': potential_profit,
        'correction': correction
    }


async def save_suggestions_for_review(missed_opportunities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Save strategy suggestions to database for manual review.

    Instead of auto-applying corrections, we save them as pending suggestions
    that can be reviewed and applied manually.

    Returns:
        List of suggestions saved
    """
    suggestions_saved = []

    for miss in missed_opportunities:
        correction = miss.get('correction')
        if not correction:
            continue

        # Prepare suggestion data based on correction type
        if correction['type'] == 'LOWER_THRESHOLD_HIGH_MOMENTUM':
            suggestion_data = {
                'type': 'LOWER_THRESHOLD',
                'from': 0.75,
                'to': 0.70,
                'condition': {'momentum_min': 0.90},
                'duration_hours': 3
            }
            suggestion_type = 'threshold_adjustment'

        elif correction['type'] == 'BOOST_SCORE_MOMENTUM_SUPPORT':
            suggestion_data = {
                'type': 'SCORE_BOOST',
                'boost_amount': 0.05,
                'condition': {'momentum_min': 0.85, 'support_min': 0.70},
                'duration_hours': 3
            }
            suggestion_type = 'score_boost'
        else:
            continue

        # Save to database
        try:
            async with get_async_session_factory()() as session:
                suggestion = PendingStrategySuggestion(
                    source='hourly_retrospective',
                    suggestion_type=suggestion_type,
                    symbol=miss.get('symbol'),
                    suggestion_data=suggestion_data,
                    reason=correction['reason'],
                    evidence={
                        'missed_profit': miss.get('potential_profit', 0),
                        'return_pct': miss.get('return_pct', 0),
                        'score': miss.get('score', 0),
                        'momentum': miss.get('momentum', 0),
                        'support': miss.get('support', 0)
                    },
                    status='pending'
                )
                session.add(suggestion)
                await session.commit()

                suggestions_saved.append({
                    'id': suggestion.id,
                    'type': suggestion_type,
                    'symbol': miss.get('symbol'),
                    'reason': correction['reason']
                })

                logger.info(
                    f"💾 SAVED SUGGESTION: {suggestion_type} for {miss.get('symbol')} "
                    f"(reason: {correction['reason'][:50]}...)"
                )

        except Exception as e:
            logger.error(f"Failed to save suggestion for {miss.get('symbol')}: {e}", exc_info=True)

    if suggestions_saved:
        logger.info(f"📋 {len(suggestions_saved)} suggestions saved for manual review")

    return suggestions_saved


# Sync wrapper for scheduler
def analyze_hourly_market_sync():
    """Synchronous wrapper for APScheduler."""
    try:
        result = asyncio.run(analyze_hourly_market())
        if result.get('status') == 'success':
            logger.info(f"✅ Hourly retrospective completed successfully")
        else:
            logger.warning(f"Hourly retrospective finished with status: {result.get('status')}")
    except Exception as e:
        logger.error(f"Hourly retrospective (sync wrapper) failed: {e}", exc_info=True)
