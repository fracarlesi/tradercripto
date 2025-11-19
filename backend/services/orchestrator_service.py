"""
Multi-Agent Orchestrator Service

Coordinates LONG and SHORT specialized agents, resolves conflicts,
and manages capital allocation on a single Hyperliquid account.
"""

import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class AgentProposal:
    """Proposal from a specialized agent"""
    agent_type: str  # "LONG" or "SHORT"
    operation: str   # "buy", "short", "sell", "hold"
    symbol: str
    confidence: float  # 0.0-1.0
    target_portion: float
    leverage: int
    reasoning: str
    technical_score: float


@dataclass
class OrchestratorDecision:
    """Final decision from orchestrator"""
    execute: bool
    agent_type: str
    operation: str
    symbol: str
    target_portion: float
    leverage: int
    reasoning: str
    conflict_resolution: Optional[str] = None


class OrchestratorService:
    """
    Central orchestrator that:
    1. Collects proposals from LONG and SHORT agents
    2. Resolves conflicts (same symbol, capital allocation)
    3. Makes final execution decision
    4. Tracks global state
    """

    def __init__(self, long_capital_ratio: float = 0.5, short_capital_ratio: float = 0.5):
        """
        Args:
            long_capital_ratio: Fraction of capital for LONG agent (0.0-1.0)
            short_capital_ratio: Fraction of capital for SHORT agent (0.0-1.0)
        """
        self.long_capital_ratio = long_capital_ratio
        self.short_capital_ratio = short_capital_ratio

    def resolve_proposals(
        self,
        long_proposal: Optional[AgentProposal],
        short_proposal: Optional[AgentProposal],
        current_positions: List[Dict[str, Any]]
    ) -> List[OrchestratorDecision]:
        """
        Resolve proposals from both agents and return the SINGLE best decision.

        Selection Strategy (momentum surfing optimization):
        - Only execute ONE trade per cycle (highest technical score wins)
        - This ensures DeepSeek makes decisions with accurate portfolio state
        - Avoids conflicting market bets that hedge momentum strategy

        Blocking Rules:
        - Cannot open opposite position on same asset

        Returns:
            List with 0 or 1 decision (never multiple)
        """
        candidates = []

        # Build current positions map
        positions_map = {}
        for pos in current_positions:
            symbol = pos.get("coin") or pos.get("symbol")
            size = float(pos.get("szi", 0) or pos.get("quantity", 0))
            if size != 0:
                positions_map[symbol] = "LONG" if size > 0 else "SHORT"

        # Process LONG proposal
        if long_proposal and long_proposal.operation != "hold":
            # Check for existing opposite position
            if long_proposal.symbol in positions_map:
                existing = positions_map[long_proposal.symbol]
                if existing == "SHORT" and long_proposal.operation == "buy":
                    logger.warning(
                        f"LONG agent blocked: Cannot BUY {long_proposal.symbol} "
                        f"while SHORT position exists"
                    )
                else:
                    candidates.append(("LONG", long_proposal))
            else:
                candidates.append(("LONG", long_proposal))

        # Process SHORT proposal
        if short_proposal and short_proposal.operation != "hold":
            # Check for existing opposite position
            if short_proposal.symbol in positions_map:
                existing = positions_map[short_proposal.symbol]
                if existing == "LONG" and short_proposal.operation == "short":
                    logger.warning(
                        f"SHORT agent blocked: Cannot SHORT {short_proposal.symbol} "
                        f"while LONG position exists"
                    )
                else:
                    candidates.append(("SHORT", short_proposal))
            else:
                candidates.append(("SHORT", short_proposal))

        # No valid candidates
        if not candidates:
            logger.info("Orchestrator: No trades to execute (all HOLD or blocked)")
            return []

        # Select BEST candidate by technical score (momentum surfing strategy)
        if len(candidates) == 1:
            winner_type, winner = candidates[0]
            logger.info(
                f"Single proposal: {winner_type} {winner.operation} {winner.symbol} "
                f"(score: {winner.technical_score:.4f})"
            )
        else:
            # Compare by technical score
            long_type, long_prop = candidates[0] if candidates[0][0] == "LONG" else candidates[1]
            short_type, short_prop = candidates[1] if candidates[1][0] == "SHORT" else candidates[0]

            if long_prop.technical_score >= short_prop.technical_score:
                winner_type, winner = "LONG", long_prop
                loser_type, loser = "SHORT", short_prop
            else:
                winner_type, winner = "SHORT", short_prop
                loser_type, loser = "LONG", long_prop

            logger.info(
                f"Best trade selected: {winner_type} {winner.operation} {winner.symbol} "
                f"(score: {winner.technical_score:.4f}) beats {loser_type} {loser.symbol} "
                f"(score: {loser.technical_score:.4f})"
            )

        # Create single decision with FULL capital allocation (no 50/50 split)
        decision = OrchestratorDecision(
            execute=True,
            agent_type=winner_type,
            operation=winner.operation,
            symbol=winner.symbol,
            target_portion=winner.target_portion,  # Use full requested portion
            leverage=winner.leverage,
            reasoning=winner.reasoning,
            conflict_resolution=f"Selected as best trade (score: {winner.technical_score:.4f})" if len(candidates) > 1 else None
        )

        logger.info(
            f"Orchestrator decision: {decision.agent_type} {decision.operation} "
            f"{decision.symbol} @ {decision.target_portion*100:.1f}% capital, {decision.leverage}x"
        )

        return [decision]

    def _create_decision(
        self,
        proposal: AgentProposal,
        capital_ratio: float
    ) -> OrchestratorDecision:
        """Create decision from proposal with capital allocation"""
        return OrchestratorDecision(
            execute=True,
            agent_type=proposal.agent_type,
            operation=proposal.operation,
            symbol=proposal.symbol,
            target_portion=proposal.target_portion * capital_ratio,
            leverage=proposal.leverage,
            reasoning=proposal.reasoning
        )


# Singleton instance
orchestrator_service = OrchestratorService()
