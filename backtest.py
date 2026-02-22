#!/usr/bin/env python3
"""Backtest shim: python backtest.py sizing [--days 1] [--timeframe 5m] [--json]"""

from backtesting.cli import main

if __name__ == "__main__":
    main()
