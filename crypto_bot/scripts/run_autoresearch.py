"""Autonomous research loop for FLAG-Trader.

Iterates through hyperparameter experiments, keeping improvements.

Usage:
    python -m scripts.run_autoresearch --data-dir data/candles --max-experiments 20 --time-budget 300
"""

import argparse
import logging
from pathlib import Path

from flag_trader.autoresearch import AutoResearcher
from flag_trader.data_collector import HyperliquidDataCollector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%H:%M:%S",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="FLAG-Trader Autoresearch")
    parser.add_argument("--data-dir", default="data/candles")
    parser.add_argument("--max-experiments", type=int, default=20)
    parser.add_argument("--time-budget", type=float, default=300, help="Minutes")
    parser.add_argument("--results", default="experiments.json")
    parser.add_argument("--train-months", type=int, default=4)
    parser.add_argument("--test-months", type=int, default=2)
    args = parser.parse_args()

    # Load candles
    collector = HyperliquidDataCollector(Path(args.data_dir))
    available = collector.list_available()
    if not available:
        print(f"No data in {args.data_dir}. Run download_candles.py first.")
        return

    # Use largest available dataset
    all_candles = []
    for symbol in available:
        df = collector.load_candles(symbol)
        all_candles.append(df[["open", "high", "low", "close", "volume"]].values)

    candles = max(all_candles, key=len)
    print(f"Using {len(candles)} candles for research")

    researcher = AutoResearcher(
        candles=candles,
        results_file=Path(args.results),
        train_months=args.train_months,
        test_months=args.test_months,
    )

    researcher.run(
        max_experiments=args.max_experiments,
        time_budget_minutes=args.time_budget,
    )

    print(f"\n{researcher.get_summary()}")


if __name__ == "__main__":
    main()
