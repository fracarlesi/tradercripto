"""Walk-forward validation for FLAG-Trader.

Usage:
    cd crypto_bot
    python -m scripts.walk_forward_validate --data-dir data/candles --train-months 4 --test-months 2
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import numpy as np

from flag_trader.data_collector import HyperliquidDataCollector
from flag_trader.walk_forward import WalkForwardValidator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-forward validation for FLAG-Trader")
    parser.add_argument("--data-dir", default="data/candles", help="Directory with cached parquet candles")
    parser.add_argument("--train-months", type=int, default=4)
    parser.add_argument("--test-months", type=int, default=2)
    parser.add_argument("--step-months", type=int, default=1)
    parser.add_argument("--ppo-updates", type=int, default=100)
    parser.add_argument("--steps", type=int, default=50)
    parser.add_argument("--reward-fn", default="sharpe_delta", choices=["sharpe_delta", "sortino_delta", "calmar_delta"])
    parser.add_argument("--save-results", default="wf_results.json")
    args = parser.parse_args()

    collector = HyperliquidDataCollector(Path(args.data_dir))
    available = collector.list_available()
    if not available:
        print(f"No data in {args.data_dir}. Run download_candles.py first.")
        return

    symbol = available[0]
    print(f"Using {symbol} candle data for walk-forward validation")

    df = collector.load_candles(symbol)
    candles = df[["open", "high", "low", "close", "volume"]].values.astype(np.float64)

    validator = WalkForwardValidator(
        candles,
        train_months=args.train_months,
        test_months=args.test_months,
        step_months=args.step_months,
    )
    result = validator.run(
        ppo_updates=args.ppo_updates,
        steps_per_rollout=args.steps,
        reward_fn=args.reward_fn,
    )

    validator.save_results(result, Path(args.save_results))

    print(f"\nPASSED: {result.passed}")
    print(f"Avg Sharpe: {result.avg_sharpe:.3f} | Avg PF: {result.avg_pf:.3f} | Avg MaxDD: {result.avg_max_dd:.1f}%")
    print(f"Profitable windows: {result.windows_profitable}/{result.total_windows}")

    for w in result.windows:
        print(
            f"  Window {w.window_id + 1}: Sharpe={w.sharpe:.3f} PF={w.profit_factor:.3f} "
            f"MDD={w.max_drawdown_pct:.1f}% Trades={w.total_trades} WR={w.win_rate:.1%} "
            f"Return={w.net_return_pct:.2f}%"
        )


if __name__ == "__main__":
    main()
