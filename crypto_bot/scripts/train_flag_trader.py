"""Train FLAG-Trader model with PPO on historical candles.

Usage:
    python -m scripts.train_flag_trader --updates 100 --steps 50
    python -m scripts.train_flag_trader --updates 10 --steps 20  # Quick smoke test
"""

import argparse
import logging
from pathlib import Path

import numpy as np

from crypto_bot.flag_trader.data_collector import HyperliquidDataCollector
from crypto_bot.flag_trader.environment import HyperliquidTradingEnv
from crypto_bot.flag_trader.model import FlagTraderModel
from crypto_bot.flag_trader.prompt import PromptBuilder
from crypto_bot.flag_trader.trainer import PPOTrainer  # Created by trainer-builder agent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def load_training_data(
    data_dir: Path, train_pct: float = 0.8
) -> tuple[np.ndarray, np.ndarray]:
    """Load candles from Parquet files, split into train/eval arrays.

    Returns:
        (train_candles, eval_candles) — each shape (N, 5) with columns [O, H, L, C, V].
    """
    collector = HyperliquidDataCollector(data_dir=data_dir)
    available = collector.list_available()
    if not available:
        raise FileNotFoundError(
            f"No candle data in {data_dir}. Run download_candles.py first."
        )

    # Load all assets
    all_candles: list[np.ndarray] = []
    for symbol in available:
        df = collector.load_candles(symbol)
        candles = df[["open", "high", "low", "close", "volume"]].values
        all_candles.append(candles)

    print(f"Loaded {len(all_candles)} assets, {sum(len(c) for c in all_candles)} total candles")

    # Sort by length descending — use longest for training
    all_candles.sort(key=len, reverse=True)

    train_candles = all_candles[0]
    split = int(len(train_candles) * train_pct)

    if len(all_candles) > 1:
        eval_candles = all_candles[1]
    else:
        eval_candles = train_candles[split:]

    train_candles = train_candles[:split]

    return train_candles, eval_candles


def main() -> None:
    parser = argparse.ArgumentParser(description="Train FLAG-Trader with PPO")
    parser.add_argument("--updates", type=int, default=100, help="Number of PPO updates")
    parser.add_argument("--steps", type=int, default=50, help="Steps per rollout")
    parser.add_argument("--lr", type=float, default=1e-5, help="Learning rate")
    parser.add_argument("--data-dir", type=str, default="data/candles", help="Directory with Parquet candle files")
    parser.add_argument("--save-dir", type=str, default="models/flag_trader", help="Directory for model checkpoints")
    parser.add_argument("--eval-every", type=int, default=25, help="Evaluate every N updates")
    parser.add_argument("--save-every", type=int, default=50, help="Save checkpoint every N updates")
    parser.add_argument("--model-name", type=str, default="HuggingFaceTB/SmolLM2-135M-Instruct", help="HuggingFace model ID")
    parser.add_argument("--device", type=str, default="auto", choices=["auto", "cpu", "cuda", "mps"], help="Device (default: auto)")
    args = parser.parse_args()

    # Load data
    data_dir = Path(args.data_dir)
    train_candles, eval_candles = load_training_data(data_dir)

    # Create components
    print(f"Loading model {args.model_name}...")
    model = FlagTraderModel(model_name=args.model_name, device=args.device)
    prompt_builder = PromptBuilder()

    train_env = HyperliquidTradingEnv(candles=train_candles)
    eval_env = HyperliquidTradingEnv(candles=eval_candles)

    trainer = PPOTrainer(model=model, prompt_builder=prompt_builder, lr=args.lr)

    # Train
    print(f"Starting training: {args.updates} updates, {args.steps} steps/rollout")
    save_dir = Path(args.save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)

    trainer.train(
        env=train_env,
        total_updates=args.updates,
        steps_per_rollout=args.steps,
        eval_env=eval_env,
        eval_every=args.eval_every,
        save_dir=save_dir,
        save_every=args.save_every,
    )

    # Final save
    model.save_trainable(save_dir / "final_model.pt")
    print(f"Training complete. Model saved to {save_dir}/final_model.pt")


if __name__ == "__main__":
    main()
