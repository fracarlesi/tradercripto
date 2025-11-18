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
        Resolve proposals from both agents and return final decisions.

        Conflict Resolution Rules:
        1. Same symbol conflict: Higher confidence wins
        2. Capital conflict: Proportional allocation
        3. Existing position conflict: Can't open opposite on same asset

        Returns:
            List of decisions to execute (0, 1, or 2)
        """
        decisions = []

        # Build current positions map
        positions_map = {}
        for pos in current_positions:
            symbol = pos.get("coin") or pos.get("symbol")
            size = float(pos.get("szi", 0) or pos.get("quantity", 0))
            if size != 0:
                positions_map[symbol] = "LONG" if size > 0 else "SHORT"

        # Check for same-symbol conflict
        if (long_proposal and short_proposal and
            long_proposal.symbol == short_proposal.symbol and
            long_proposal.operation in ["buy"] and
            short_proposal.operation in ["short"]):

            # Conflict! Choose based on confidence/technical score
            if long_proposal.confidence >= short_proposal.confidence:
                winner = long_proposal
                loser_type = "SHORT"
            else:
                winner = short_proposal
                loser_type = "LONG"

            logger.info(
                f"Conflict resolved: {winner.agent_type} wins on {winner.symbol} "
                f"(confidence {winner.confidence:.2f} vs {loser_type})"
            )

            decisions.append(OrchestratorDecision(
                execute=True,
                agent_type=winner.agent_type,
                operation=winner.operation,
                symbol=winner.symbol,
                target_portion=winner.target_portion * (
                    self.long_capital_ratio if winner.agent_type == "LONG"
                    else self.short_capital_ratio
                ),
                leverage=winner.leverage,
                reasoning=winner.reasoning,
                conflict_resolution=f"Won against {loser_type} agent on same symbol"
            ))
            return decisions

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
                    decisions.append(self._create_decision(
                        long_proposal, self.long_capital_ratio
                    ))
            else:
                decisions.append(self._create_decision(
                    long_proposal, self.long_capital_ratio
                ))

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
                    decisions.append(self._create_decision(
                        short_proposal, self.short_capital_ratio
                    ))
            else:
                decisions.append(self._create_decision(
                    short_proposal, self.short_capital_ratio
                ))

        # Log summary
        if not decisions:
            logger.info("Orchestrator: No trades to execute (all HOLD or blocked)")
        else:
            for d in decisions:
                logger.info(
                    f"Orchestrator decision: {d.agent_type} {d.operation} "
                    f"{d.symbol} @ {d.target_portion*100:.1f}% capital, {d.leverage}x"
                )

        return decisions

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
