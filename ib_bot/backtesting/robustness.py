"""
IB Backtesting - Robustness Scoring
=====================================

Multi-dimensional scoring that penalizes fragile configurations.
Combines PnL, profit factor, drawdown, trade count, Sharpe, and
win rate into a single 0-100 score.

Used by walk-forward validation to select the best in-sample config
instead of naive PnL ranking.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List


def calculate_robustness_score(stats: Dict[str, Any]) -> float:
    """Calculate a multi-dimensional robustness score (0-100).

    Combines:
    - net_pnl (normalized)           25%
    - profit_factor (capped at 3.0)  20%
    - max_drawdown_pct (penalty)     20%
    - trade_count adequacy           15%
    - sharpe_ratio                   10%
    - win_rate                       10%

    Penalizes configs with < 10 trades severely (multiplied by
    trade_count / 10 if under 10).

    Args:
        stats: Dictionary with keys: net_pnl, profit_factor,
               max_drawdown_pct, trade_count, sharpe, win_rate.
               All values should be floats.

    Returns:
        Score between 0.0 and 100.0, higher = more robust.
    """
    net_pnl = float(stats.get("net_pnl", 0.0))
    profit_factor = float(stats.get("profit_factor", 0.0))
    max_dd_pct = float(stats.get("max_drawdown_pct", 0.0))
    trade_count = int(stats.get("trade_count", 0))
    sharpe = float(stats.get("sharpe", 0.0))
    win_rate = float(stats.get("win_rate", 0.0))

    # --- PnL component (25%) ---
    # Normalize: sigmoid-like mapping, $0 -> 50, positive -> higher
    # Scale: $500 ~ 75, $1000 ~ 90, -$500 ~ 25
    pnl_norm = _sigmoid(net_pnl / 500.0) * 100.0
    pnl_score = pnl_norm * 0.25

    # --- Profit Factor component (20%) ---
    # Cap at 3.0 to avoid outlier bias, map [0, 3] -> [0, 100]
    pf_capped = min(max(profit_factor, 0.0), 3.0)
    pf_score = (pf_capped / 3.0) * 100.0 * 0.20

    # --- Drawdown penalty (20%) ---
    # 0% DD -> 100, 5% DD -> 50, 10%+ DD -> 0
    dd_score = max(0.0, 100.0 - max_dd_pct * 10.0) * 0.20

    # --- Trade count adequacy (15%) ---
    # 0 trades -> 0, 10 trades -> 75, 20+ trades -> 100
    if trade_count >= 20:
        tc_raw = 100.0
    elif trade_count >= 10:
        tc_raw = 50.0 + (trade_count - 10) * 5.0
    elif trade_count > 0:
        tc_raw = trade_count * 5.0
    else:
        tc_raw = 0.0
    tc_score = tc_raw * 0.15

    # --- Sharpe component (10%) ---
    # Map: 0 -> 0, 1.0 -> 50, 2.0+ -> 100
    sharpe_capped = min(max(sharpe, 0.0), 2.0)
    sharpe_score = (sharpe_capped / 2.0) * 100.0 * 0.10

    # --- Win rate component (10%) ---
    # Direct percentage mapping, capped at 70% for max score
    wr_capped = min(win_rate, 70.0)
    wr_score = (wr_capped / 70.0) * 100.0 * 0.10

    total = pnl_score + pf_score + dd_score + tc_score + sharpe_score + wr_score

    # --- Low trade count penalty ---
    # If fewer than 10 trades, multiply total by trade_count/10
    if trade_count < 10 and trade_count > 0:
        total *= trade_count / 10.0
    elif trade_count == 0:
        total = 0.0

    return round(min(max(total, 0.0), 100.0), 2)


def _sigmoid(x: float) -> float:
    """Sigmoid function mapping (-inf, inf) -> (0, 1)."""
    return 1.0 / (1.0 + math.exp(-x))


def classify_robustness(score: float) -> str:
    """Classify a robustness score into a category.

    Args:
        score: Robustness score (0-100).

    Returns:
        "fragile" (< 30), "acceptable" (30-60), or "robust" (> 60).
    """
    if score < 30.0:
        return "fragile"
    elif score <= 60.0:
        return "acceptable"
    else:
        return "robust"


def rank_configs(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sort configurations by robustness score (descending).

    Each dict in results must contain the keys needed by
    calculate_robustness_score(). A "robustness_score" and
    "robustness_class" key will be added to each entry.

    Args:
        results: List of config result dicts with stats fields.

    Returns:
        Same list sorted by robustness_score (highest first),
        with robustness_score and robustness_class added.
    """
    for r in results:
        score = calculate_robustness_score(r)
        r["robustness_score"] = score
        r["robustness_class"] = classify_robustness(score)

    return sorted(results, key=lambda x: x["robustness_score"], reverse=True)


def stats_from_backtest_result(result: Any) -> Dict[str, Any]:
    """Extract stats dict from an IBBacktestResult for robustness scoring.

    Args:
        result: An IBBacktestResult instance (from stats.py).

    Returns:
        Dictionary compatible with calculate_robustness_score().
    """
    return {
        "net_pnl": result.net_pnl,
        "profit_factor": result.profit_factor,
        "max_drawdown_pct": result.max_drawdown_pct,
        "trade_count": result.count,
        "sharpe": result.sharpe,
        "win_rate": result.win_rate,
    }
