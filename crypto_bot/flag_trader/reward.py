"""
Reward Function — Sharpe Ratio Delta
=====================================

Implements the reward signal from FLAG-Trader paper (Eq. 1):
    reward_t = SR_t - SR_{t-1}

where SR_t is the Sharpe ratio computed over PnL history up to step t.
"""

from __future__ import annotations

import math
from typing import Callable


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


def compute_sortino_delta(
    pnl_history: list[float],
    risk_free_rate: float = 0.0,
) -> float:
    """Sortino ratio delta — penalises only downside volatility.

    Args:
        pnl_history: List of per-step PnL values.
        risk_free_rate: Risk-free rate (default 0).

    Returns:
        Change in Sortino ratio after the latest step.
    """
    if len(pnl_history) < 2:
        return 0.0

    def _sortino(pnls: list[float]) -> float:
        if len(pnls) < 2:
            return 0.0
        mean_return = sum(pnls) / len(pnls) - risk_free_rate
        downside = [min(0.0, p) for p in pnls]
        downside_std = (sum(d**2 for d in downside) / len(downside)) ** 0.5
        if downside_std < 1e-10:
            return 0.0
        return mean_return / downside_std

    current = _sortino(pnl_history)
    previous = _sortino(pnl_history[:-1])
    return current - previous


def compute_calmar_delta(pnl_history: list[float]) -> float:
    """Calmar ratio delta — return / max drawdown.

    Args:
        pnl_history: List of per-step PnL values.

    Returns:
        Change in Calmar ratio after the latest step.
    """
    if len(pnl_history) < 2:
        return 0.0

    def _calmar(pnls: list[float]) -> float:
        if len(pnls) < 2:
            return 0.0
        cumsum = [sum(pnls[:i + 1]) for i in range(len(pnls))]
        peak = cumsum[0]
        max_dd = 0.0
        for v in cumsum:
            peak = max(peak, v)
            dd = peak - v
            max_dd = max(max_dd, dd)
        if max_dd < 1e-10:
            return 0.0
        total_return = cumsum[-1]
        return total_return / max_dd

    current = _calmar(pnl_history)
    previous = _calmar(pnl_history[:-1])
    return current - previous



def compute_enhanced_sharpe(
    pnl_history: list[float],
    risk_free_rate: float = 0.0,
    trade_opened: bool = False,
    trade_closed_profit: bool | None = None,
) -> float:
    """Enhanced Sharpe delta con bonus/penalità per segnale più chiaro.

    Componenti:
    1. Base: sharpe_delta (esistente)
    2. PnL bonus: +0.1 se trade chiuso in profitto, -0.1 se chiuso in loss
    3. Fee penalty: -0.01 se trade aperto (scoraggia overtrading)

    I parametri extra (trade_opened, trade_closed_profit) sono opzionali.
    Se non forniti, si comporta come sharpe_delta normale.

    Args:
        pnl_history: List of per-step PnL values (at least 2 for a delta).
        risk_free_rate: Annualized risk-free rate (default 0).
        trade_opened: Whether a new trade was opened this step.
        trade_closed_profit: True if trade closed in profit, False if in loss,
            None if no trade was closed this step.

    Returns:
        Enhanced reward combining Sharpe delta with trade-level signals.
    """
    # 1. Base component: standard Sharpe delta
    reward = compute_sharpe_delta(pnl_history, risk_free_rate)

    # 2. PnL bonus/penalty on trade close
    if trade_closed_profit is True:
        reward += 0.1
    elif trade_closed_profit is False:
        reward -= 0.1

    # 3. Fee penalty on trade open (discourages overtrading)
    if trade_opened:
        reward -= 0.01

    return reward


REWARD_FUNCTIONS: dict[str, Callable[..., float]] = {
    "sharpe_delta": compute_sharpe_delta,
    "sortino_delta": compute_sortino_delta,
    "calmar_delta": compute_calmar_delta,
    "enhanced_sharpe": compute_enhanced_sharpe,
}
