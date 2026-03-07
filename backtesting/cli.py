"""CLI entry point for the backtesting framework."""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backtest",
        description="HLQuantBot backtesting framework",
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    # Common args added to each subparser
    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--days", type=int, default=None,
                        help="Lookback days (default varies by mode)")
        sp.add_argument("--timeframe", type=str, default=None,
                        help="Candle timeframe (default: 15m)")
        sp.add_argument("--account", type=float, default=None,
                        help="Account size in USD (default: from config)")
        sp.add_argument("--json", action="store_true",
                        help="Output results as JSON")

    sp_sizing = sub.add_parser("sizing",
                               help="Compare position sizing configs")
    add_common(sp_sizing)

    sp_strat = sub.add_parser("strategies",
                              help="Compare alternative strategies")
    add_common(sp_strat)

    sp_regime = sub.add_parser("regime",
                               help="Grid-search regime parameters")
    add_common(sp_regime)

    sp_tf = sub.add_parser("timeframes",
                           help="Compare 5m/15m/1h timeframes")
    add_common(sp_tf)

    sp_thresh = sub.add_parser("threshold",
                               help="Compare P&L at each ML threshold level")
    add_common(sp_thresh)

    sp_replay = sub.add_parser("replay",
                               help="Replay historical data through full live-bot logic")
    add_common(sp_replay)
    sp_replay.add_argument("--threshold", type=float, default=None,
                           help="ML probability threshold (default: from trading.yaml)")
    sp_replay.add_argument("--tp", type=float, default=None,
                           help="Take-profit %% (e.g. 3.5 for 3.5%%)")
    sp_replay.add_argument("--sl", type=float, default=None,
                           help="Stop-loss %% (e.g. 1.0 for 1.0%%)")
    sp_replay.add_argument("--kelly", action="store_true",
                           help="Use Kelly criterion for position sizing")
    sp_replay.add_argument("--no-ml", action="store_true", dest="no_ml",
                           help="Disable ML model (accept all signals)")
    sp_replay.add_argument("--verbose", action="store_true",
                           help="Print each open/close event")
    sp_replay.add_argument("--bar-log", type=str, default=None, dest="bar_log",
                           help="Write bar-level CSV log to FILE")
    sp_replay.add_argument("--breakeven", type=float, default=None,
                           help="Breakeven threshold %% (e.g. 1.0 for 1.0%%, 99 to disable)")
    sp_replay.add_argument("--trailing", type=float, default=None,
                           help="Trailing ATR multiplier (0 to disable)")

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.mode == "sizing":
        from backtesting.modes.sizing import run
    elif args.mode == "strategies":
        from backtesting.modes.strategies import run
    elif args.mode == "regime":
        from backtesting.modes.regime import run
    elif args.mode == "timeframes":
        from backtesting.modes.timeframes import run
    elif args.mode == "threshold":
        from backtesting.modes.threshold import run
    elif args.mode == "replay":
        from backtesting.modes.replay import run
    else:
        parser.print_help()
        sys.exit(1)

    run(args)


if __name__ == "__main__":
    main()
