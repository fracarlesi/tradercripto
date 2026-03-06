"""
IB Backtesting - CLI Entry Point
==================================

Usage:
    python -m ib_bot.backtesting replay --days 30 --symbols MES
    python -m ib_bot.backtesting replay --days 60 --symbols MES MNQ --trades --verbose
    python -m ib_bot.backtesting sweep --days 30
    python -m ib_bot.backtesting sweep --days 60 --verbose --trades
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        description="IB ORB Backtest Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m ib_bot.backtesting replay --days 30 --symbols MES\n"
            "  python -m ib_bot.backtesting replay --days 60 --symbols MES MNQ --trades\n"
            "  python -m ib_bot.backtesting sweep --days 30\n"
            "  python -m ib_bot.backtesting sweep --days 60 --verbose --trades\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="mode", help="Backtest mode")

    # ---- Replay mode ----
    replay_parser = subparsers.add_parser("replay", help="Run single replay backtest")
    replay_parser.add_argument(
        "--days", type=int, default=30,
        help="Number of calendar days to look back (default: 30)",
    )
    replay_parser.add_argument(
        "--symbols", nargs="+", default=["MES"],
        help="Futures symbols to backtest (default: MES)",
    )
    replay_parser.add_argument(
        "--account", type=float, default=10_000,
        help="Starting account size in USD (default: 10000)",
    )
    replay_parser.add_argument(
        "--ib-host", default="127.0.0.1",
        help="IB Gateway/TWS host (default: 127.0.0.1)",
    )
    replay_parser.add_argument(
        "--ib-port", type=int, default=4002,
        help="IB Gateway/TWS port (default: 4002 for paper)",
    )
    replay_parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )
    replay_parser.add_argument(
        "--trades", action="store_true",
        help="Print individual trade log after summary",
    )

    # ---- Sweep mode ----
    sweep_parser = subparsers.add_parser(
        "sweep", help="Run parameter sweep across multiple configs",
    )
    sweep_parser.add_argument(
        "--days", type=int, default=30,
        help="Number of calendar days to look back (default: 30)",
    )
    sweep_parser.add_argument(
        "--symbols", nargs="+", default=None,
        help="Extra symbols to include (MES+MNQ always included)",
    )
    sweep_parser.add_argument(
        "--account", type=float, default=10_000,
        help="Starting account size in USD (default: 10000)",
    )
    sweep_parser.add_argument(
        "--ib-host", default="127.0.0.1",
        help="IB Gateway/TWS host (default: 127.0.0.1)",
    )
    sweep_parser.add_argument(
        "--ib-port", type=int, default=4002,
        help="IB Gateway/TWS port (default: 4002 for paper)",
    )
    sweep_parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )
    sweep_parser.add_argument(
        "--trades", action="store_true",
        help="Print individual trade logs for each config",
    )

    # ---- Sweep-sizing mode ----
    sizing_parser = subparsers.add_parser(
        "sweep-sizing", help="Sweep position sizing on Config F (best ORB)",
    )
    sizing_parser.add_argument(
        "--days", type=int, default=90,
        help="Number of calendar days to look back (default: 90)",
    )
    sizing_parser.add_argument(
        "--ib-host", default="127.0.0.1",
        help="IB Gateway/TWS host (default: 127.0.0.1)",
    )
    sizing_parser.add_argument(
        "--ib-port", type=int, default=4002,
        help="IB Gateway/TWS port (default: 4002 for paper)",
    )
    sizing_parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )
    sizing_parser.add_argument(
        "--trades", action="store_true",
        help="Print individual trade logs for each config",
    )

    # ---- Sweep-filtered mode ----
    sf_parser = subparsers.add_parser(
        "sweep-filtered",
        help="Run parameter sweep with regime/volatility filters",
    )
    sf_parser.add_argument(
        "--days", type=int, default=30,
        help="Number of calendar days to look back (default: 30)",
    )
    sf_parser.add_argument(
        "--symbols", nargs="+", default=None,
        help="Extra symbols to include (MES+MNQ always included)",
    )
    sf_parser.add_argument(
        "--account", type=float, default=10_000,
        help="Starting account size in USD (default: 10000)",
    )
    sf_parser.add_argument(
        "--ib-host", default="127.0.0.1",
        help="IB Gateway/TWS host (default: 127.0.0.1)",
    )
    sf_parser.add_argument(
        "--ib-port", type=int, default=4002,
        help="IB Gateway/TWS port (default: 4002 for paper)",
    )
    sf_parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )
    sf_parser.add_argument(
        "--trades", action="store_true",
        help="Print individual trade logs for each config",
    )

    # ---- Sweep-EMA mode ----
    sweep_ema_parser = subparsers.add_parser(
        "sweep-ema", help="Run EMA momentum parameter sweep",
    )
    sweep_ema_parser.add_argument(
        "--days", type=int, default=90,
        help="Number of calendar days to look back (default: 90)",
    )
    sweep_ema_parser.add_argument(
        "--symbols", nargs="+", default=None,
        help="Extra symbols to include (MES+MNQ always included)",
    )
    sweep_ema_parser.add_argument(
        "--account", type=float, default=10_000,
        help="Starting account size in USD (default: 10000)",
    )
    sweep_ema_parser.add_argument(
        "--ib-host", default="127.0.0.1",
        help="IB Gateway/TWS host (default: 127.0.0.1)",
    )
    sweep_ema_parser.add_argument(
        "--ib-port", type=int, default=4002,
        help="IB Gateway/TWS port (default: 4002 for paper)",
    )
    sweep_ema_parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )
    sweep_ema_parser.add_argument(
        "--trades", action="store_true",
        help="Print individual trade logs for each config",
    )

    args = parser.parse_args()

    if not args.mode:
        parser.print_help()
        sys.exit(1)

    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.mode == "replay":
        from .modes.replay import run_replay
        asyncio.run(run_replay(args))
    elif args.mode == "sweep":
        from .modes.sweep import run_sweep
        asyncio.run(run_sweep(args))
    elif args.mode == "sweep-sizing":
        from .modes.sweep_sizing import run_sweep_sizing
        asyncio.run(run_sweep_sizing(args))
    elif args.mode == "sweep-filtered":
        from .modes.sweep_filtered import run_sweep_filtered
        asyncio.run(run_sweep_filtered(args))
    elif args.mode == "sweep-ema":
        from .modes.sweep_ema import run_sweep_ema
        asyncio.run(run_sweep_ema(args))
