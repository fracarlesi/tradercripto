"""
IB Backtesting - Replay Mode
===============================

Run a single-configuration replay backtest.
Fetches historical 1-min bars from IB (with caching), then
simulates the ORB strategy day-by-day using the live bot's logic.

Usage:
    python -m ib_bot.backtesting replay --days 30 --symbols MES
"""

from __future__ import annotations

import logging
from argparse import Namespace
from datetime import date, timedelta

from ..config import load_backtest_config
from ..data import IBDataFetcher
from ..orb_detector import detect_opening_range
from ..simulator import ORBSimulator
from ..stats import IBBacktestResult, print_summary, print_trade_log

logger = logging.getLogger(__name__)


async def run_replay(args: Namespace) -> None:
    """Run a single-config replay backtest.

    Steps:
        1. Load backtest config (from trading.yaml + CLI overrides)
        2. Fetch historical bars from IB (cached after first fetch)
        3. Run ORBSimulator day-by-day
        4. Print summary statistics and optional trade log
    """
    cfg = load_backtest_config(
        symbols=args.symbols,
        account_size=args.account,
        lookback_days=args.days,
    )

    end_date = date.today()
    start_date = end_date - timedelta(days=cfg.lookback_days)

    logger.info(
        "Fetching %d days of data for %s (%s -> %s)...",
        cfg.lookback_days, cfg.symbols, start_date, end_date,
    )

    # Connect to IB and fetch data
    fetcher = IBDataFetcher(
        host=args.ib_host,
        port=args.ib_port,
        client_id=2,
        cache_dir=cfg.cache_dir,
    )

    await fetcher.connect()

    try:
        bars_by_day: dict[str, dict[str, list]] = {}
        for symbol in cfg.symbols:
            symbol_bars = await fetcher.fetch_days(symbol, start_date, end_date)
            for day_str, bars in symbol_bars.items():
                bars_by_day.setdefault(day_str, {})[symbol] = bars
    finally:
        await fetcher.disconnect()

    logger.info("Data ready: %d trading days", len(bars_by_day))

    if not bars_by_day:
        logger.error("No data fetched. Check IB connection and symbol availability.")
        return

    # Run simulation
    sim = ORBSimulator(cfg)
    sim.run(bars_by_day, detect_opening_range)

    # Build and display results
    result = IBBacktestResult(
        label=f"ORB {', '.join(cfg.symbols)} ({cfg.lookback_days}d)",
        trades=sim.trades,
        equity_curve=sim.equity_curve,
        daily_results=sim.daily_results,
        initial_equity=cfg.account_size,
    )

    print_summary(result)

    if args.trades:
        print_trade_log(result)
