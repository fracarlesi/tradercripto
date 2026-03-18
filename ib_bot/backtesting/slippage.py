"""
IB Backtesting - Slippage Scenario Testing
============================================

Applies post-hoc slippage adjustments to trade results to test
strategy robustness under different execution quality assumptions.

Scenarios range from "ideal" (perfect fills) to "hostile" (worst case).
Each scenario adjusts entry, exit (TP), and stop (SL) fills by a
configurable number of ticks in the adverse direction.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Dict, List

from ..core.contracts import CONTRACTS


@dataclass
class SlippageScenario:
    """Defines slippage parameters for a scenario."""

    name: str
    entry_slippage_ticks: float
    exit_slippage_ticks: float    # applies to both TP and SL exits
    stop_slippage_ticks: float    # additional adverse slippage on SL fills
    description: str


SCENARIOS: Dict[str, SlippageScenario] = {
    "ideal": SlippageScenario(
        "ideal", 0, 0, 0,
        "Perfect fills - no slippage",
    ),
    "normal": SlippageScenario(
        "normal", 0.5, 0.25, 0.5,
        "Typical execution quality",
    ),
    "adverse": SlippageScenario(
        "adverse", 1.0, 0.5, 1.0,
        "Poor execution (fast markets, low liquidity)",
    ),
    "hostile": SlippageScenario(
        "hostile", 2.0, 1.0, 2.0,
        "Worst-case execution (news events, gaps)",
    ),
}


def apply_slippage(
    trades: List[Dict[str, Any]],
    scenario: SlippageScenario,
    contract_specs: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    """Adjust trade PnLs for slippage.

    For each trade:
    - Entry is worsened by entry_slippage_ticks (LONG: higher, SHORT: lower)
    - TP exit is worsened by exit_slippage_ticks
    - SL exit is worsened by (exit_slippage_ticks + stop_slippage_ticks)

    Note: This adjusts the NET PnL by the tick-value cost of slippage.
    It does NOT re-simulate whether the trade would still have triggered.

    Args:
        trades: List of trade dicts (as produced by simulators).
        scenario: SlippageScenario to apply.
        contract_specs: Optional mapping of symbol -> FuturesSpec.
                       If None, uses the global CONTRACTS dict.

    Returns:
        New list of trade dicts with adjusted PnLs (originals not modified).
    """
    if contract_specs is None:
        contract_specs = CONTRACTS

    adjusted: List[Dict[str, Any]] = []

    for trade in trades:
        t = copy.deepcopy(trade)
        symbol = t.get("symbol", "MES")
        spec = contract_specs.get(symbol)
        if spec is None:
            adjusted.append(t)
            continue

        tick_value = float(spec.tick_value)
        contracts = t.get("contracts", 1)
        direction = t.get("direction", "LONG")
        reason = t.get("reason", "")

        # Entry slippage: always adverse
        entry_cost = scenario.entry_slippage_ticks * tick_value * contracts

        # Exit slippage: base + additional for stops
        if reason == "SL":
            exit_cost = (
                scenario.exit_slippage_ticks + scenario.stop_slippage_ticks
            ) * tick_value * contracts
        else:
            exit_cost = scenario.exit_slippage_ticks * tick_value * contracts

        total_slippage = entry_cost + exit_cost

        t["net_pnl"] = t["net_pnl"] - total_slippage
        t["gross_pnl"] = t["gross_pnl"] - total_slippage
        t["slippage_cost"] = total_slippage
        t["slippage_scenario"] = scenario.name

        adjusted.append(t)

    return adjusted


def run_slippage_scenarios(
    trades: List[Dict[str, Any]],
    scenarios: List[str] | None = None,
) -> Dict[str, Dict[str, Any]]:
    """Run multiple slippage scenarios and return summary stats for each.

    Args:
        trades: Original trade list from simulator.
        scenarios: List of scenario names to test.
                  If None or ["all"], runs all scenarios.

    Returns:
        Dict mapping scenario name to summary stats dict with:
        - net_pnl, trade_count, profit_factor, win_rate, slippage_name
    """
    if scenarios is None or scenarios == ["all"] or "all" in (scenarios or []):
        scenario_list = list(SCENARIOS.values())
    else:
        scenario_list = [
            SCENARIOS[name] for name in scenarios if name in SCENARIOS
        ]

    results: Dict[str, Dict[str, Any]] = {}

    for scenario in scenario_list:
        adjusted = apply_slippage(trades, scenario)

        net_pnl = sum(t["net_pnl"] for t in adjusted)
        wins = sum(1 for t in adjusted if t["net_pnl"] > 0)
        losses = sum(1 for t in adjusted if t["net_pnl"] <= 0)
        total = len(adjusted)

        gross_profit = sum(t["net_pnl"] for t in adjusted if t["net_pnl"] > 0)
        gross_loss = abs(sum(t["net_pnl"] for t in adjusted if t["net_pnl"] < 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else 999.0

        total_slippage = sum(t.get("slippage_cost", 0.0) for t in adjusted)

        results[scenario.name] = {
            "scenario": scenario.name,
            "description": scenario.description,
            "net_pnl": net_pnl,
            "trade_count": total,
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / total * 100) if total > 0 else 0.0,
            "profit_factor": pf,
            "total_slippage_cost": total_slippage,
            "trades": adjusted,
        }

    return results


def print_slippage_table(
    scenario_results: Dict[str, Dict[str, Any]],
) -> None:
    """Print a comparison table of slippage scenario results."""
    print()
    print("=" * 90)
    print("  SLIPPAGE SCENARIO ANALYSIS")
    print("=" * 90)
    print()

    header = (
        f"  {'Scenario':<12s} | {'Description':<30s} | "
        f"{'Net P&L':>10s} | {'PF':>6s} | {'Win%':>6s} | {'Slip$':>8s}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    for name in ["ideal", "normal", "adverse", "hostile"]:
        if name not in scenario_results:
            continue
        r = scenario_results[name]
        line = (
            f"  {r['scenario']:<12s} | {r['description']:<30s} | "
            f"${r['net_pnl']:>+9,.2f} | {r['profit_factor']:>6.2f} | "
            f"{r['win_rate']:>5.1f}% | ${r['total_slippage_cost']:>7,.2f}"
        )
        print(line)

    print()

    # Survival check
    profitable_scenarios = [
        name for name, r in scenario_results.items() if r["net_pnl"] > 0
    ]
    total_scenarios = len(scenario_results)
    print(
        f"  Profitable in {len(profitable_scenarios)}/{total_scenarios} "
        f"scenarios: {', '.join(profitable_scenarios) if profitable_scenarios else 'NONE'}"
    )

    if "normal" in scenario_results and scenario_results["normal"]["net_pnl"] <= 0:
        print("  WARNING: Strategy is NOT profitable under normal slippage!")

    print()
    print("=" * 90)
