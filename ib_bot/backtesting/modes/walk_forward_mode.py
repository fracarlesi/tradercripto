"""
IB Backtesting - Walk-Forward Mode
=====================================

Fetches data from IB, runs walk-forward validation engine,
and prints results.

Usage:
    python -m ib_bot.backtesting walk-forward --days 90 --strategy orb
"""

from __future__ import annotations

import logging
from argparse import Namespace
from datetime import date, timedelta
from typing import Any

from ..data import IBDataFetcher
from ..walk_forward import (
    WalkForwardConfig,
    WalkForwardEngine,
    print_walk_forward_summary,
)

logger = logging.getLogger(__name__)


async def run_walk_forward(args: Namespace) -> None:
    """Run walk-forward validation.

    Steps:
        1. Fetch historical bars from IB (cached)
        2. Configure walk-forward windows
        3. Run engine (sweep train, validate OOS per window)
        4. Print summary with verdict
    """
    symbols = args.symbols
    end_date = date.today()
    start_date = end_date - timedelta(days=args.days)

    logger.info(
        "Fetching %d days of data for %s (%s -> %s)...",
        args.days, symbols, start_date, end_date,
    )

    # Fetch data once (per-symbol, then merge)
    fetcher = IBDataFetcher(
        host=args.ib_host,
        port=args.ib_port,
        client_id=2,
    )

    await fetcher.connect()

    try:
        bars_by_day: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for symbol in symbols:
            symbol_bars = await fetcher.fetch_days(symbol, start_date, end_date)
            for day_str, bars in symbol_bars.items():
                bars_by_day.setdefault(day_str, {})[symbol] = bars
    finally:
        await fetcher.disconnect()

    logger.info("Data ready: %d trading days", len(bars_by_day))

    if not bars_by_day:
        logger.error("No data fetched. Check IB connection and symbol availability.")
        return

    # Configure walk-forward
    wf_config = WalkForwardConfig(
        total_days=args.days,
        train_days=args.train,
        validate_days=args.validate,
        step_days=args.step,
        symbols=symbols,
        strategy=args.strategy,
        account_size=args.account,
    )

    # Run
    engine = WalkForwardEngine()
    engine.set_data(bars_by_day)
    result = engine.run(wf_config)

    # Print results
    print_walk_forward_summary(result)
