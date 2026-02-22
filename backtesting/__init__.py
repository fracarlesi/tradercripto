"""Backtesting framework for HLQuantBot strategies."""

from backtesting.config import BacktestConfig, load_config
from backtesting.indicators import compute_indicators
from backtesting.simulator import PortfolioSimulator
from backtesting.stats import BacktestResult, print_comparison_table, print_results_json
from backtesting.signals import signal_trend_momentum

__all__ = [
    "BacktestConfig",
    "load_config",
    "compute_indicators",
    "PortfolioSimulator",
    "BacktestResult",
    "print_comparison_table",
    "print_results_json",
    "signal_trend_momentum",
]
