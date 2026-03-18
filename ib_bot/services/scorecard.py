"""
Paper-Trading Scorecard
========================

Rolling-window metrics + promotion state machine.
Evaluates paper-trading performance to determine readiness
for live micro-contract deployment.

States:
  FAIL:              20-session PnL < 0 AND PF < 0.8
  HALT:              DD > $400 OR 5-session loss > $150
  HOLD_PAPER:        < 30 trades in 20 sessions OR positive but inconclusive
  CANDIDATE_LIVE_MICRO: PF > 1.2 (20s), PF > 1.0 (10s), 30+ trades,
                        DD < $300, WR > 35%
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import List

from .trade_journal import SessionRecord

logger = logging.getLogger(__name__)


class PromotionState(str, Enum):
    """Promotion state machine states."""

    FAIL = "fail"
    HALT = "halt"
    HOLD_PAPER = "hold_paper"
    CANDIDATE_LIVE_MICRO = "candidate_live_micro"


@dataclass
class ScorecardMetrics:
    """Computed metrics for a rolling window."""

    window_sessions: int
    total_trades: int
    total_pnl: Decimal
    profit_factor: Decimal
    win_rate: float
    max_drawdown: Decimal
    recent_5s_pnl: Decimal
    promotion_state: PromotionState


class Scorecard:
    """Paper-trading scorecard with promotion state machine."""

    def __init__(
        self,
        halt_dd_usd: Decimal = Decimal("400"),
        halt_5s_loss_usd: Decimal = Decimal("150"),
        candidate_pf_20s: Decimal = Decimal("1.2"),
        candidate_pf_10s: Decimal = Decimal("1.0"),
        candidate_min_trades: int = 30,
        candidate_max_dd: Decimal = Decimal("300"),
        candidate_min_wr: float = 35.0,
    ) -> None:
        self._halt_dd = halt_dd_usd
        self._halt_5s_loss = halt_5s_loss_usd
        self._cand_pf_20 = candidate_pf_20s
        self._cand_pf_10 = candidate_pf_10s
        self._cand_min_trades = candidate_min_trades
        self._cand_max_dd = candidate_max_dd
        self._cand_min_wr = candidate_min_wr

    def evaluate(self, sessions: List[SessionRecord]) -> ScorecardMetrics:
        """Evaluate sessions and determine promotion state.

        Args:
            sessions: List of SessionRecords (most recent 20 sessions).

        Returns:
            ScorecardMetrics with computed stats and promotion state.
        """
        if not sessions:
            return ScorecardMetrics(
                window_sessions=0,
                total_trades=0,
                total_pnl=Decimal("0"),
                profit_factor=Decimal("0"),
                win_rate=0.0,
                max_drawdown=Decimal("0"),
                recent_5s_pnl=Decimal("0"),
                promotion_state=PromotionState.HOLD_PAPER,
            )

        # Use last 20 sessions max
        recent_20 = sessions[-20:]
        recent_10 = sessions[-10:]
        recent_5 = sessions[-5:]

        # Collect all completed trades
        all_trades: list[Decimal] = []
        for s in recent_20:
            for t in s.trades:
                if t.pnl is not None:
                    all_trades.append(Decimal(t.pnl))

        total_trades = len(all_trades)
        total_pnl = sum(all_trades, Decimal("0"))

        # Profit factor (20-session)
        gross_profit = sum((p for p in all_trades if p > 0), Decimal("0"))
        gross_loss = abs(sum((p for p in all_trades if p < 0), Decimal("0")))
        pf_20 = gross_profit / gross_loss if gross_loss > 0 else Decimal("999")

        # Profit factor (10-session)
        trades_10: list[Decimal] = []
        for s in recent_10:
            for t in s.trades:
                if t.pnl is not None:
                    trades_10.append(Decimal(t.pnl))
        gp_10 = sum((p for p in trades_10 if p > 0), Decimal("0"))
        gl_10 = abs(sum((p for p in trades_10 if p < 0), Decimal("0")))
        pf_10 = gp_10 / gl_10 if gl_10 > 0 else Decimal("999")

        # Win rate
        wins = sum(1 for p in all_trades if p > 0)
        win_rate = (wins / total_trades * 100.0) if total_trades > 0 else 0.0

        # Max drawdown (session-level equity curve)
        equity = Decimal("0")
        peak = Decimal("0")
        max_dd = Decimal("0")
        for s in recent_20:
            equity += Decimal(s.total_pnl)
            if equity > peak:
                peak = equity
            dd = peak - equity
            if dd > max_dd:
                max_dd = dd

        # Recent 5-session P&L
        pnl_5 = sum((Decimal(s.total_pnl) for s in recent_5), Decimal("0"))

        # --- State machine ---
        state = self._determine_state(
            total_pnl=total_pnl,
            pf_20=pf_20,
            pf_10=pf_10,
            total_trades=total_trades,
            max_dd=max_dd,
            win_rate=win_rate,
            pnl_5=pnl_5,
            num_sessions=len(recent_20),
        )

        return ScorecardMetrics(
            window_sessions=len(recent_20),
            total_trades=total_trades,
            total_pnl=total_pnl,
            profit_factor=pf_20,
            win_rate=win_rate,
            max_drawdown=max_dd,
            recent_5s_pnl=pnl_5,
            promotion_state=state,
        )

    def _determine_state(
        self,
        total_pnl: Decimal,
        pf_20: Decimal,
        pf_10: Decimal,
        total_trades: int,
        max_dd: Decimal,
        win_rate: float,
        pnl_5: Decimal,
        num_sessions: int,
    ) -> PromotionState:
        """Apply promotion rules in priority order."""
        # HALT: drawdown or recent losses exceed threshold
        if max_dd > self._halt_dd:
            return PromotionState.HALT
        if pnl_5 < -self._halt_5s_loss:
            return PromotionState.HALT

        # FAIL: negative performance with low PF
        if num_sessions >= 20 and total_pnl < 0 and pf_20 < Decimal("0.8"):
            return PromotionState.FAIL

        # CANDIDATE: all criteria must pass
        if (
            pf_20 >= self._cand_pf_20
            and pf_10 >= self._cand_pf_10
            and total_trades >= self._cand_min_trades
            and max_dd <= self._cand_max_dd
            and win_rate >= self._cand_min_wr
        ):
            return PromotionState.CANDIDATE_LIVE_MICRO

        return PromotionState.HOLD_PAPER

    @staticmethod
    def format_report(metrics: ScorecardMetrics) -> str:
        """Format scorecard metrics as a human-readable report."""
        lines = [
            "--- SCORECARD REPORT ---",
            f"Sessions: {metrics.window_sessions}",
            f"Trades: {metrics.total_trades}",
            f"Total P&L: ${metrics.total_pnl:.2f}",
            f"Profit Factor: {metrics.profit_factor:.2f}",
            f"Win Rate: {metrics.win_rate:.1f}%",
            f"Max DD: ${metrics.max_drawdown:.2f}",
            f"Last 5 sessions: ${metrics.recent_5s_pnl:.2f}",
            f"Status: {metrics.promotion_state.value.upper()}",
            "------------------------",
        ]
        return "\n".join(lines)
