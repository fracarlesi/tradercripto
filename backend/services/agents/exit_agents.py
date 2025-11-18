"""
Exit Agents - AI-powered Take Profit and Stop Loss decision making.

These agents analyze positions intelligently instead of using fixed percentages.
They consider:
- Current P&L and momentum
- Technical indicators
- Market conditions
- Entry strategy context
"""

import logging
from dataclasses import dataclass
from typing import Any

from database.models import Account
from services.ai.deepseek_client import DeepSeekClient

logger = logging.getLogger(__name__)


@dataclass
class ExitDecision:
    """Decision from an exit agent."""
    agent_type: str  # "TAKE_PROFIT" or "STOP_LOSS"
    symbol: str
    should_exit: bool
    confidence: float  # 0.0 - 1.0
    reasoning: str
    pnl_pct: float


class ExitAgentService:
    """Service for AI-powered exit decisions."""

    def __init__(self):
        pass  # Client created per-call with account parameter

    async def analyze_position_for_exit(
        self,
        account: Account,
        position_data: dict[str, Any],
        technical_factors: dict[str, Any],
        agent_type: str  # "TAKE_PROFIT" or "STOP_LOSS"
    ) -> ExitDecision | None:
        """
        Analyze a position and decide whether to exit.

        Args:
            account: Trading account
            position_data: Position info from Hyperliquid (coin, szi, entryPx, pnl, etc.)
            technical_factors: Current technical indicators for the symbol
            agent_type: "TAKE_PROFIT" or "STOP_LOSS"

        Returns:
            ExitDecision or None if analysis fails
        """
        coin = position_data.get('coin', '')
        szi = float(position_data.get('szi', 0))
        entry_px = float(position_data.get('entryPx', 0))
        position_value = float(position_data.get('positionValue', 0))
        unrealized_pnl = float(position_data.get('unrealizedPnl', 0))

        # Calculate P&L percentage
        if position_value > 0:
            pnl_pct = unrealized_pnl / position_value
        else:
            return None

        # Get technical data for this symbol
        recommendations = technical_factors.get('recommendations', [])
        symbol_tech = next((r for r in recommendations if r['symbol'] == coin), None)

        # Build prompt based on agent type
        if agent_type == "TAKE_PROFIT":
            prompt = self._build_take_profit_prompt(
                coin, szi, entry_px, pnl_pct, unrealized_pnl, symbol_tech
            )
        else:  # STOP_LOSS
            prompt = self._build_stop_loss_prompt(
                coin, szi, entry_px, pnl_pct, unrealized_pnl, symbol_tech
            )

        try:
            # Create DeepSeek client for this account
            deepseek_client = DeepSeekClient(account)

            # Call DeepSeek for decision
            response = await deepseek_client.chat_async(
                model=account.model or "deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,  # Low temperature for consistent decisions
                max_tokens=500,
            )

            # Parse response
            decision = self._parse_exit_response(
                response, agent_type, coin, pnl_pct
            )

            return decision

        except Exception as e:
            logger.error(f"Exit agent {agent_type} failed for {coin}: {e}", exc_info=True)
            return None

    def _build_take_profit_prompt(
        self,
        coin: str,
        szi: float,
        entry_px: float,
        pnl_pct: float,
        unrealized_pnl: float,
        symbol_tech: dict | None
    ) -> str:
        """Build prompt for Take Profit agent."""
        direction = "LONG" if szi > 0 else "SHORT"

        tech_info = ""
        if symbol_tech:
            tech_info = f"""
Technical Indicators:
- Score: {symbol_tech.get('score', 0):.3f}
- Momentum: {symbol_tech.get('momentum', 0):.3f}
- Support Level: {symbol_tech.get('support', 0):.2f}
"""

        return f"""You are an expert TAKE PROFIT agent for crypto trading.

CURRENT POSITION:
- Symbol: {coin}
- Direction: {direction}
- Entry Price: ${entry_px:.2f}
- Current P&L: {pnl_pct:.2%} (${unrealized_pnl:.2f})
{tech_info}

TASK: Decide if NOW is the optimal time to take profit.

Consider:
1. Is momentum slowing down? (exit before reversal)
2. Is P&L sufficient given entry risk?
3. Are technical indicators showing overbought/oversold?
4. Is there resistance/support nearby that could reverse price?

CRITICAL: For momentum surfing strategy, don't be greedy.
- Lock in profits when momentum shows signs of weakening
- +3-5% is good for quick trades
- +5-10% is excellent, consider partial exit
- >10% watch closely for reversal signs

Respond in this EXACT format:
DECISION: EXIT or HOLD
CONFIDENCE: 0.0 to 1.0
REASONING: Brief explanation (1-2 sentences)
"""

    def _build_stop_loss_prompt(
        self,
        coin: str,
        szi: float,
        entry_px: float,
        pnl_pct: float,
        unrealized_pnl: float,
        symbol_tech: dict | None
    ) -> str:
        """Build prompt for Stop Loss agent."""
        direction = "LONG" if szi > 0 else "SHORT"

        tech_info = ""
        if symbol_tech:
            tech_info = f"""
Technical Indicators:
- Score: {symbol_tech.get('score', 0):.3f}
- Momentum: {symbol_tech.get('momentum', 0):.3f}
- Support Level: {symbol_tech.get('support', 0):.2f}
"""

        return f"""You are an expert STOP LOSS agent for crypto trading.

CURRENT POSITION:
- Symbol: {coin}
- Direction: {direction}
- Entry Price: ${entry_px:.2f}
- Current P&L: {pnl_pct:.2%} (${unrealized_pnl:.2f})
{tech_info}

TASK: Decide if this position should be closed to prevent further losses.

Consider:
1. Is this a temporary dip or trend reversal?
2. Is momentum against the position direction?
3. Are key support/resistance levels broken?
4. Could holding lead to larger losses?

CRITICAL RULES:
- IGNORE losses under -1% (normal bid/ask spread and commissions)
- Losses -1% to -3% are NORMAL and acceptable - DO NOT EXIT for these
- Only consider exit at -3% to -5% if momentum is clearly against position
- -5% to -7% requires serious analysis - exit only if no reversal signs
- >-7% exit unless very strong reversal signals
- HARD STOP at -10% regardless of conditions

IMPORTANT: Small losses (-0.1% to -1%) are just commission costs and spread.
These are NOT real losses - HOLD through them and wait for the trade to develop.
Closing immediately after entry guarantees losses from spread + commissions.

Respond in this EXACT format:
DECISION: EXIT or HOLD
CONFIDENCE: 0.0 to 1.0
REASONING: Brief explanation (1-2 sentences)
"""

    def _parse_exit_response(
        self,
        response: str,
        agent_type: str,
        symbol: str,
        pnl_pct: float
    ) -> ExitDecision:
        """Parse DeepSeek response into ExitDecision."""
        lines = response.strip().split('\n')

        should_exit = False
        confidence = 0.5
        reasoning = "Unable to parse response"

        for line in lines:
            line = line.strip()
            if line.startswith('DECISION:'):
                decision_text = line.replace('DECISION:', '').strip().upper()
                should_exit = decision_text == 'EXIT'
            elif line.startswith('CONFIDENCE:'):
                try:
                    confidence = float(line.replace('CONFIDENCE:', '').strip())
                    confidence = max(0.0, min(1.0, confidence))
                except ValueError:
                    confidence = 0.5
            elif line.startswith('REASONING:'):
                reasoning = line.replace('REASONING:', '').strip()

        return ExitDecision(
            agent_type=agent_type,
            symbol=symbol,
            should_exit=should_exit,
            confidence=confidence,
            reasoning=reasoning,
            pnl_pct=pnl_pct
        )


# Global instance
_exit_agent_service = None

def get_exit_agent_service() -> ExitAgentService:
    """Get singleton exit agent service."""
    global _exit_agent_service
    if _exit_agent_service is None:
        _exit_agent_service = ExitAgentService()
    return _exit_agent_service


async def call_exit_agent(
    account: Account,
    position_data: dict[str, Any],
    technical_factors: dict[str, Any],
    agent_type: str
) -> ExitDecision | None:
    """
    Convenience function to call an exit agent.

    Args:
        account: Trading account
        position_data: Position info (coin, szi, entryPx, pnl, positionValue)
        technical_factors: Current technical analysis
        agent_type: "TAKE_PROFIT" or "STOP_LOSS"

    Returns:
        ExitDecision or None
    """
    service = get_exit_agent_service()
    return await service.analyze_position_for_exit(
        account, position_data, technical_factors, agent_type
    )
