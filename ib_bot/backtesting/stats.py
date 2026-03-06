"""
IB Backtesting - Results Statistics & Reporting
=================================================

Aggregates trade results into summary statistics:
- Win rate, profit factor, Sharpe ratio
- Max drawdown, equity curve
- Daily P&L breakdown
- Individual trade log
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass
class IBBacktestResult:
    """Aggregated backtest result with computed statistics."""

    label: str
    trades: List[Dict[str, Any]]
    equity_curve: List[float]
    daily_results: List[Dict[str, Any]]
    initial_equity: float

    # ---- Core metrics (computed as properties) ----

    @property
    def count(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t["net_pnl"] > 0)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t["net_pnl"] <= 0)

    @property
    def win_rate(self) -> float:
        return (self.wins / self.count * 100) if self.count else 0.0

    @property
    def net_pnl(self) -> float:
        return sum(t["net_pnl"] for t in self.trades)

    @property
    def gross_pnl(self) -> float:
        return sum(t["gross_pnl"] for t in self.trades)

    @property
    def total_commission(self) -> float:
        return sum(t["commission"] for t in self.trades)

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t["net_pnl"] for t in self.trades if t["net_pnl"] > 0)
        gross_loss = abs(sum(t["net_pnl"] for t in self.trades if t["net_pnl"] < 0))
        return gross_profit / gross_loss if gross_loss > 0 else 999.0

    @property
    def max_drawdown(self) -> float:
        """Maximum peak-to-trough drawdown in dollars."""
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0]
        max_dd = 0.0
        for eq in self.equity_curve:
            if eq > peak:
                peak = eq
            dd = peak - eq
            if dd > max_dd:
                max_dd = dd
        return max_dd

    @property
    def max_drawdown_pct(self) -> float:
        """Maximum drawdown as percentage of peak equity."""
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0]
        max_dd_pct = 0.0
        for eq in self.equity_curve:
            if eq > peak:
                peak = eq
            if peak > 0:
                dd_pct = (peak - eq) / peak * 100
                if dd_pct > max_dd_pct:
                    max_dd_pct = dd_pct
        return max_dd_pct

    @property
    def sharpe(self) -> float:
        """Annualized Sharpe ratio (252 trading days)."""
        if len(self.daily_results) < 2:
            return 0.0
        daily_pnls = [d["pnl"] for d in self.daily_results]
        n = len(daily_pnls)
        avg = sum(daily_pnls) / n
        variance = sum((p - avg) ** 2 for p in daily_pnls) / n
        std = math.sqrt(variance) if variance > 0 else 0.0
        return float(avg / std * math.sqrt(252)) if std > 0 else 0.0

    @property
    def avg_win(self) -> float:
        """Average winning trade P&L in dollars."""
        wins = [t["net_pnl"] for t in self.trades if t["net_pnl"] > 0]
        return sum(wins) / len(wins) if wins else 0.0

    @property
    def avg_loss(self) -> float:
        """Average losing trade P&L in dollars (returned as positive)."""
        losses = [abs(t["net_pnl"]) for t in self.trades if t["net_pnl"] < 0]
        return sum(losses) / len(losses) if losses else 0.0

    @property
    def avg_win_ticks(self) -> float:
        wins = [t["ticks"] for t in self.trades if t["net_pnl"] > 0]
        return sum(wins) / len(wins) if wins else 0.0

    @property
    def avg_loss_ticks(self) -> float:
        losses = [abs(t["ticks"]) for t in self.trades if t["net_pnl"] < 0]
        return sum(losses) / len(losses) if losses else 0.0

    @property
    def return_pct(self) -> float:
        """Total return as percentage of initial equity."""
        return (self.net_pnl / self.initial_equity * 100) if self.initial_equity > 0 else 0.0


# =========================================================================
# Reporting functions
# =========================================================================

def print_summary(result: IBBacktestResult) -> None:
    """Print formatted backtest summary to stdout."""
    print()
    print("=" * 60)
    print(f"  IB ORB Backtest: {result.label}")
    print("=" * 60)
    print()
    print(f"  Trades:          {result.count}")
    print(f"  Wins / Losses:   {result.wins} / {result.losses}")
    print(f"  Win Rate:        {result.win_rate:.1f}%")
    print()
    print(f"  Net P&L:         ${result.net_pnl:,.2f}")
    print(f"  Gross P&L:       ${result.gross_pnl:,.2f}")
    print(f"  Commissions:     ${result.total_commission:,.2f}")
    print(f"  Return:          {result.return_pct:+.2f}%")
    print()
    print(f"  Profit Factor:   {result.profit_factor:.2f}")
    print(f"  Sharpe Ratio:    {result.sharpe:.2f}")
    print(f"  Max Drawdown:    ${result.max_drawdown:,.2f} ({result.max_drawdown_pct:.1f}%)")
    print()
    print(f"  Avg Win:         ${result.avg_win:,.2f} ({result.avg_win_ticks:.1f} ticks)")
    print(f"  Avg Loss:        ${result.avg_loss:,.2f} ({result.avg_loss_ticks:.1f} ticks)")
    print(f"  Final Equity:    ${result.equity_curve[-1]:,.2f}")
    print()

    # Daily breakdown (only days with trades)
    active_days = [d for d in result.daily_results if d["trades"] > 0]
    if active_days:
        print("  --- Daily P&L ---")
        for d in active_days:
            print(
                f"  {d['date']}: {d['trades']} trade{'s' if d['trades'] != 1 else ''}, "
                f"${d['pnl']:+.2f}  (eq: ${d['equity']:,.2f})"
            )
        print()

    print("=" * 60)


def print_trade_log(result: IBBacktestResult) -> None:
    """Print individual trades line-by-line."""
    if not result.trades:
        print("  No trades.")
        return

    print()
    print("  --- Trade Log ---")
    for i, t in enumerate(result.trades, 1):
        entry_str = (
            t["entry_time"].strftime("%Y-%m-%d %H:%M")
            if hasattr(t["entry_time"], "strftime")
            else str(t["entry_time"])
        )
        print(
            f"  {i:3d}. {entry_str} | {t['direction']:5s} {t['symbol']} "
            f"x{t['contracts']} | {t['entry']:.2f} -> {t['exit']:.2f} | "
            f"{t['ticks']:+.1f} ticks | ${t['net_pnl']:+.2f} | {t['reason']}"
        )
    print()
