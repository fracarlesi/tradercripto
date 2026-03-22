"""
Reward Function — Sharpe Ratio Delta
=====================================

Implements the reward signal from FLAG-Trader paper (Eq. 1):
    reward_t = SR_t - SR_{t-1}

where SR_t is the Sharpe ratio computed over PnL history up to step t.
"""

from __future__ import annotations

import math


def compute_sharpe_delta(
    pnl_history: list[float],
    risk_free_rate: float = 0.0,
) -> float:
    """Compute incremental Sharpe ratio change: SR_t - SR_{t-1}.

    Args:
        pnl_history: List of per-step PnL values (at least 2 for a delta).
        risk_free_rate: Annualized risk-free rate (default 0).

    Returns:
        The change in Sharpe ratio after the latest step.
        Returns 0.0 if history is too short or std is zero.
    """
    if len(pnl_history) < 2:
        return 0.0

    def _sharpe(values: list[float]) -> float:
        n = len(values)
        if n < 2:
            return 0.0
        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / (n - 1)
        std = math.sqrt(variance)
        if std < 1e-12:
            return 0.0
        return (mean - risk_free_rate) / std

    sr_prev = _sharpe(pnl_history[:-1])
    sr_curr = _sharpe(pnl_history)
    return sr_curr - sr_prev
