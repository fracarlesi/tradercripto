"""BacktestResult and output formatting."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import numpy as np

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backtesting.simulator import PortfolioSimulator


@dataclass
class BacktestResult:
    """Aggregated results from a backtest run."""

    label: str
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    initial_equity: float = 86.0

    @property
    def count(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t["net"] > 0)

    @property
    def win_rate(self) -> float:
        return (self.wins / self.count * 100) if self.count > 0 else 0.0

    @property
    def net_pnl(self) -> float:
        return sum(t["net"] for t in self.trades)

    @property
    def total_fees(self) -> float:
        return sum(t["fees"] for t in self.trades)

    @property
    def max_drawdown(self) -> float:
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
    def profit_factor(self) -> float:
        gp = sum(t["net"] for t in self.trades if t["net"] > 0)
        gl = abs(sum(t["net"] for t in self.trades if t["net"] < 0))
        if gl > 0:
            return gp / gl
        return 999.0 if gp > 0 else 0.0

    @property
    def sharpe(self) -> float:
        """Annualized Sharpe ratio (assuming ~96 trades/day at 15m)."""
        if self.count < 2:
            return 0.0
        pnls = [t["net"] for t in self.trades]
        avg = np.mean(pnls)
        std = np.std(pnls)
        if std == 0:
            return 0.0
        return float(avg / std * math.sqrt(96 * 365))

    @property
    def unique_assets(self) -> int:
        return len(set(t.get("symbol", "") for t in self.trades))

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "count": self.count,
            "wins": self.wins,
            "win_rate": round(self.win_rate, 1),
            "net_pnl": round(self.net_pnl, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "total_fees": round(self.total_fees, 4),
            "profit_factor": round(self.profit_factor, 2),
            "sharpe": round(self.sharpe, 2),
            "unique_assets": self.unique_assets,
        }

    @classmethod
    def from_simulator(cls, sim: "PortfolioSimulator", label: str = "") -> BacktestResult:
        """Build from a PortfolioSimulator instance."""
        return cls(
            label=label or sim.label,
            trades=sim.trades,
            equity_curve=sim.equity_curve,
            initial_equity=sim.cfg.account_size,
        )


def print_comparison_table(results: list[BacktestResult]) -> None:
    """Print a formatted comparison table of results."""
    print("=" * 100)
    print(f"{'Config':<30} {'Trades':>6} {'Wins':>5} {'Win%':>6} "
          f"{'Net P&L':>9} {'MaxDD':>8} {'Fees':>8} {'PF':>6} {'Sharpe':>7}")
    print("-" * 100)
    for r in results:
        print(f"{r.label:<30} {r.count:>6} {r.wins:>5} {r.win_rate:>5.1f}% "
              f"${r.net_pnl:>+7.2f} ${r.max_drawdown:>6.2f} "
              f"${r.total_fees:>6.2f} {r.profit_factor:>6.2f} {r.sharpe:>7.2f}")
    print("=" * 100)


def print_results_json(results: list[BacktestResult]) -> None:
    """Print results as JSON."""
    print(json.dumps([r.to_dict() for r in results], indent=2))


def print_top_bottom_trades(trades: list[dict], label: str, n: int = 5) -> None:
    """Print top and bottom trades by net P&L."""
    if not trades:
        return
    print()
    print(f"Detail: {label}")
    print("-" * 80)

    sorted_t = sorted(trades, key=lambda t: t["net"], reverse=True)
    n_show = min(n, len(sorted_t))

    print(f"\nTop {n_show} by P&L:")
    for t in sorted_t[:n_show]:
        d = "LONG" if t["direction"] == 1 else "SHORT"
        print(f"  {t['symbol']:<10} {d:<5} ${t['net']:>+.4f} ({t['reason']})  "
              f"notional=${t['notional']:.1f}")

    print(f"\nBottom {n_show}:")
    for t in sorted_t[-n_show:]:
        d = "LONG" if t["direction"] == 1 else "SHORT"
        print(f"  {t['symbol']:<10} {d:<5} ${t['net']:>+.4f} ({t['reason']})  "
              f"notional=${t['notional']:.1f}")

    winners = [t["net"] for t in trades if t["net"] > 0]
    losers = [t["net"] for t in trades if t["net"] < 0]
    print()
    print(f"  {len(trades)} trades on {len(set(t['symbol'] for t in trades))} assets")
    if winners:
        print(f"  Avg win:  ${np.mean(winners):.4f}")
    if losers:
        print(f"  Avg loss: ${np.mean(losers):.4f}")
