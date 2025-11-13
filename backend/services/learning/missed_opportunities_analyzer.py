"""
Missed Opportunities Analyzer - Hourly post-mortem analysis.

Every hour, this service:
1. Fetches top market movers (gainers/losers) from Hyperliquid
2. Checks if AI considered them and why they weren't chosen
3. Logs detailed report with AI reasoning
4. Identifies recurring patterns of missed opportunities
"""

import logging
from datetime import datetime, timedelta, UTC
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import async_session_factory
from database.models import Account, DecisionSnapshot, MissedOpportunitiesReport
from services.trading.hyperliquid_trading_service import hyperliquid_trading_service

logger = logging.getLogger(__name__)


async def analyze_missed_opportunities(
    lookback_hours: int = 1,
    min_move_pct: float = 10.0,
) -> Dict:
    """
    Analyze top market movers and check why AI didn't trade them.
    
    Args:
        lookback_hours: How many hours back to analyze
        min_move_pct: Minimum price movement % to consider (default 10%)
    
    Returns:
        Dictionary with analysis results
    """
    try:
        logger.info(f"🔍 Starting missed opportunities analysis (last {lookback_hours}h, min move: {min_move_pct}%)")
        
        # 1. Get top market movers from Hyperliquid
        top_movers = await _get_top_market_movers(lookback_hours, min_move_pct)
        
        if not top_movers:
            logger.info("No significant market movers found")
            return {"status": "no_movers", "movers": []}
        
        logger.info(f"📊 Found {len(top_movers)} significant movers:")
        for mover in top_movers[:10]:
            logger.info(f"   {mover['symbol']:10s}: {mover['change_pct']:+6.2f}%  (price: ${mover['current_price']:.4f})")
        
        # 2. Get AI decisions from last hour
        async with async_session_factory() as db:
            cutoff_time = datetime.now(UTC) - timedelta(hours=lookback_hours)
            
            result = await db.execute(
                select(DecisionSnapshot)
                .where(DecisionSnapshot.timestamp >= cutoff_time)
                .order_by(DecisionSnapshot.timestamp.desc())
            )
            recent_decisions = list(result.scalars().all())
        
        logger.info(f"🤖 Found {len(recent_decisions)} AI decisions in last {lookback_hours}h")
        
        # 3. Analyze each top mover
        missed_opportunities = []
        for mover in top_movers[:10]:  # Analyze top 10
            analysis = await _analyze_single_mover(mover, recent_decisions)
            if analysis:
                missed_opportunities.append(analysis)
        
        # 4. Generate summary report
        report_text, patterns, recommendations = _generate_report(missed_opportunities, lookback_hours)

        logger.info(f"\n{'='*80}\n{report_text}\n{'='*80}")

        # 5. Save report to database
        async with async_session_factory() as db:
            report_entry = MissedOpportunitiesReport(
                analyzed_at=datetime.now(UTC),
                lookback_hours=lookback_hours,
                min_move_pct=min_move_pct,
                total_movers=len(top_movers),
                analyzed_movers=len(missed_opportunities),
                gainers_missed=len([m for m in missed_opportunities if m['direction'] == 'UP']),
                losers_missed=len([m for m in missed_opportunities if m['direction'] == 'DOWN']),
                missed_opportunities=missed_opportunities,
                patterns_identified=patterns,
                recommendations=recommendations,
                report_text=report_text,
                status="completed",
            )
            db.add(report_entry)
            await db.commit()
            logger.info(f"✅ Report saved to database (ID: {report_entry.id})")

        return {
            "status": "completed",
            "report_id": report_entry.id,
            "lookback_hours": lookback_hours,
            "total_movers": len(top_movers),
            "analyzed_movers": len(missed_opportunities),
            "missed_opportunities": missed_opportunities,
            "patterns": patterns,
            "recommendations": recommendations,
            "report": report_text,
        }
        
    except Exception as e:
        logger.error(f"Failed to analyze missed opportunities: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}


async def _get_top_market_movers(
    lookback_hours: int,
    min_move_pct: float,
) -> List[Dict]:
    """
    Fetch top market movers from Hyperliquid in the last N hours.
    
    Returns list sorted by absolute % change (gainers and losers).
    """
    try:
        # Get all market prices
        all_mids = await hyperliquid_trading_service.get_all_mids_async()
        
        # Get 24h data for comparison (Hyperliquid provides 24h change)
        # For hourly analysis, we use recent snapshots
        from services.market_data.hyperliquid_market_data import hyperliquid_market_data
        
        movers = []
        for symbol, current_price in all_mids.items():
            try:
                # Get recent candles for the symbol
                candles = await hyperliquid_market_data.get_historical_klines(
                    symbol=symbol,
                    interval="1h",
                    limit=lookback_hours + 1,
                )
                
                if len(candles) < 2:
                    continue
                
                # Calculate % change from N hours ago
                old_price = float(candles[0]['close'])
                new_price = float(current_price)
                change_pct = ((new_price - old_price) / old_price) * 100
                
                if abs(change_pct) >= min_move_pct:
                    movers.append({
                        "symbol": symbol,
                        "old_price": old_price,
                        "current_price": new_price,
                        "change_pct": change_pct,
                        "direction": "UP" if change_pct > 0 else "DOWN",
                    })
            except Exception as e:
                logger.debug(f"Skipping {symbol}: {e}")
                continue
        
        # Sort by absolute % change (descending)
        movers.sort(key=lambda x: abs(x['change_pct']), reverse=True)
        
        return movers
        
    except Exception as e:
        logger.error(f"Failed to get market movers: {e}", exc_info=True)
        return []


async def _analyze_single_mover(
    mover: Dict,
    recent_decisions: List[DecisionSnapshot],
) -> Optional[Dict]:
    """
    Analyze why AI didn't trade a specific mover.
    
    Returns dict with analysis or None if AI did trade it.
    """
    import json
    
    symbol = mover['symbol']
    
    # Check if AI traded this symbol
    traded = any(d.symbol == symbol and d.actual_decision != 'HOLD' for d in recent_decisions)
    
    if traded:
        logger.debug(f"✅ {symbol} was traded, skipping analysis")
        return None
    
    # Find most recent decision that considered this symbol
    for decision in recent_decisions:
        try:
            indicators = json.loads(decision.indicators_snapshot)
            tech_analysis = indicators.get('technical_analysis', {})
            opportunities = tech_analysis.get('top_opportunities', [])
            
            # Find this symbol in opportunities
            symbol_data = next((o for o in opportunities if o.get('symbol') == symbol), None)
            
            if symbol_data:
                return {
                    "symbol": symbol,
                    "price_move": mover['change_pct'],
                    "direction": mover['direction'],
                    "ai_decision": decision.actual_decision,
                    "ai_chosen_symbol": decision.symbol,
                    "technical_score": symbol_data.get('technical_score', 0),
                    "pivot_signal": symbol_data.get('pivot_signal'),
                    "prophet_trend": symbol_data.get('prophet_forecast', {}).get('trend'),
                    "prophet_confidence": symbol_data.get('prophet_forecast', {}).get('confidence', 0),
                    "rsi": symbol_data.get('rsi'),
                    "macd_signal": symbol_data.get('macd_signal'),
                    "ai_reasoning": decision.deepseek_reasoning[:300] + "...",
                    "decision_time": decision.timestamp.isoformat(),
                }
        except Exception as e:
            logger.debug(f"Error parsing decision for {symbol}: {e}")
            continue
    
    # Symbol wasn't in top opportunities at all
    return {
        "symbol": symbol,
        "price_move": mover['change_pct'],
        "direction": mover['direction'],
        "ai_decision": "NOT_ANALYZED",
        "technical_score": None,
        "reason": "Not in top opportunities list",
    }


def _generate_report(missed_opportunities: List[Dict], lookback_hours: int) -> tuple[str, Dict, List[str]]:
    """
    Generate human-readable report of missed opportunities.

    Returns:
        Tuple of (report_text, patterns_dict, recommendations_list)
    """

    report = f"""
🔍 MISSED OPPORTUNITIES ANALYSIS - Last {lookback_hours} hour(s)
{'='*80}

📊 Summary:
   Total missed opportunities: {len(missed_opportunities)}
   Gainers missed: {len([m for m in missed_opportunities if m['direction'] == 'UP'])}
   Losers missed: {len([m for m in missed_opportunities if m['direction'] == 'DOWN'])}

"""

    if not missed_opportunities:
        report += "✅ No significant missed opportunities detected!\n"
        return report, {}, []
    
    report += "📈 TOP MISSED OPPORTUNITIES:\n\n"
    
    for i, opp in enumerate(missed_opportunities[:5], 1):
        report += f"{i}. {opp['symbol']} ({opp['direction']}): {opp['price_move']:+.2f}%\n"
        
        if opp.get('technical_score') is not None:
            report += f"   Technical Score: {opp['technical_score']:.3f}\n"
            report += f"   Pivot Signal: {opp.get('pivot_signal', 'N/A')}\n"
            report += f"   Prophet: {opp.get('prophet_trend', 'N/A')} (conf: {opp.get('prophet_confidence', 0):.2f})\n"
            report += f"   RSI: {opp.get('rsi', 'N/A')}\n"
            report += f"   MACD: {opp.get('macd_signal', 'N/A')}\n"
            
            if opp.get('ai_reasoning'):
                report += f"   AI Reasoning: {opp['ai_reasoning']}\n"
        else:
            report += f"   Reason: {opp.get('reason', 'Unknown')}\n"
        
        report += "\n"
    
    # Identify patterns
    report += "🔍 PATTERNS IDENTIFIED:\n\n"
    
    # Pattern 1: Strong Prophet bearish blocking buys
    prophet_blocks = [
        o for o in missed_opportunities 
        if o.get('prophet_trend') == 'bearish' 
        and o.get('prophet_confidence', 0) > 0.6
        and o['direction'] == 'UP'
    ]
    if prophet_blocks:
        report += f"⚠️  Prophet bearish blocked {len(prophet_blocks)} upward moves:\n"
        for o in prophet_blocks[:3]:
            report += f"     - {o['symbol']} (+{o['price_move']:.1f}%) - tech score {o.get('technical_score', 0):.2f}\n"
        report += "\n"
    
    # Pattern 2: Low technical score but strong move
    low_score_movers = [
        o for o in missed_opportunities
        if o.get('technical_score') is not None
        and o['technical_score'] < 0.6
        and abs(o['price_move']) > 15
    ]
    if low_score_movers:
        report += f"⚠️  Low tech score despite strong moves: {len(low_score_movers)} cases\n"
        for o in low_score_movers[:3]:
            report += f"     - {o['symbol']} ({o['price_move']:+.1f}%) - score only {o['technical_score']:.2f}\n"
        report += "\n"
    
    # Pattern 3: Not analyzed at all
    not_analyzed = [o for o in missed_opportunities if o.get('ai_decision') == 'NOT_ANALYZED']
    if not_analyzed:
        report += f"⚠️  Not in top opportunities: {len(not_analyzed)} symbols\n"
        report += "     → Consider increasing top_opportunities limit in technical analysis\n\n"
    
    report += "💡 RECOMMENDATIONS:\n\n"

    recommendations = []
    if prophet_blocks:
        report += "   1. Reduce prophet weight (currently blocking momentum trades)\n"
        recommendations.append("Reduce prophet weight (currently blocking momentum trades)")

    if low_score_movers:
        report += "   2. Review technical_score calculation (missing momentum signals)\n"
        recommendations.append("Review technical_score calculation (missing momentum signals)")

    if not_analyzed:
        report += "   3. Increase top_opportunities limit to analyze more symbols\n"
        recommendations.append("Increase top_opportunities limit to analyze more symbols")

    # Build patterns dict
    patterns = {
        "prophet_blocks": {
            "count": len(prophet_blocks),
            "examples": [
                {"symbol": o["symbol"], "move": o["price_move"], "tech_score": o.get("technical_score", 0)}
                for o in prophet_blocks[:3]
            ],
        } if prophet_blocks else None,
        "low_score_movers": {
            "count": len(low_score_movers),
            "examples": [
                {"symbol": o["symbol"], "move": o["price_move"], "tech_score": o.get("technical_score", 0)}
                for o in low_score_movers[:3]
            ],
        } if low_score_movers else None,
        "not_analyzed": {
            "count": len(not_analyzed),
            "symbols": [o["symbol"] for o in not_analyzed[:5]],
        } if not_analyzed else None,
    }

    return report, patterns, recommendations


# Wrapper for scheduler
def analyze_missed_opportunities_sync():
    """Synchronous wrapper for scheduler."""
    import asyncio
    return asyncio.run(analyze_missed_opportunities())
