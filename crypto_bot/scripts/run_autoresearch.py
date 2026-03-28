"""Autonomous research loop for FLAG-Trader (DeepSeek / model-driven).

Trains models with different hyperparameters and validates with the replay engine.
Designed to run on RunPod (GPU) -- training AND replay on the same machine.

Usage:
    python -m scripts.run_autoresearch --data-dir data/candles --max-experiments 20 --time-budget 300
    python -m scripts.run_autoresearch --device cuda --time-budget 600  # RunPod with GPU
"""

import argparse
import logging
import sys
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from crypto_bot.flag_trader.autoresearch import AutoResearcher
from crypto_bot.flag_trader.data_collector import HyperliquidDataCollector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)


def load_candles_by_symbol(
    data_dir: Path,
    assets: list[str] | None = None,
) -> dict[str, list[dict]]:
    """Load candle data from parquet files.

    Args:
        data_dir: Directory containing parquet files.
        assets: Optional list of symbols to load. If None, loads all available.

    Returns:
        Dict mapping symbol -> list of candle dicts.
    """
    collector = HyperliquidDataCollector(data_dir=data_dir)
    available = collector.list_available()
    if not available:
        raise FileNotFoundError(f"No candle data in {data_dir}. Run download_candles.py first.")

    if assets:
        symbols = [s for s in assets if s in available]
        missing = [s for s in assets if s not in available]
        if missing:
            logging.warning("Requested assets not found: %s", ", ".join(missing))
    else:
        symbols = available

    result: dict[str, list[dict]] = {}
    for symbol in symbols:
        df = collector.load_candles(symbol)
        candles = [
            {
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            }
            for _, row in df.iterrows()
        ]
        result[symbol] = candles

    total_bars = sum(len(c) for c in result.values())
    logging.info("Loaded %d assets, %d total candles", len(result), total_bars)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="FLAG-Trader Autoresearch (DeepSeek)")
    parser.add_argument("--data-dir", default="data/candles", help="Candle parquet directory")
    parser.add_argument("--assets", nargs="+", default=None, help="Symbols to use (default: all)")
    parser.add_argument("--max-experiments", type=int, default=6, help="Max experiments to run")
    parser.add_argument("--time-budget", type=float, default=600, help="Time budget in minutes (default: 10h)")
    parser.add_argument("--results", default="experiments.json", help="Results JSON file")
    parser.add_argument(
        "--device", default="auto", choices=["auto", "cpu", "cuda", "mps"],
        help="Device for model (default: auto-detect)",
    )
    parser.add_argument(
        "--model-name", default=None,
        help="Override model name (default: DeepSeek-R1-Distill-Qwen-1.5B)",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir

    # Load candles
    candles_by_symbol = load_candles_by_symbol(data_dir, args.assets)

    # Build baseline config override
    baseline_config: dict = {}
    if args.model_name:
        baseline_config["model_name"] = args.model_name

    researcher = AutoResearcher(
        candles_by_symbol=candles_by_symbol,
        baseline_config=baseline_config,
        results_file=Path(args.results),
        device=args.device,
    )

    researcher.run(
        max_experiments=args.max_experiments,
        time_budget_minutes=args.time_budget,
    )

    print(f"\n{researcher.get_summary()}")


if __name__ == "__main__":
    main()
