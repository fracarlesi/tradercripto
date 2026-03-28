"""Train a single FLAG-Trader model with specified hyperparameters.

Standalone training script that bypasses the autoresearch loop.
Loads candles from parquet, runs supervised warm-start, then PPO training.

Usage:
    python -m crypto_bot.scripts.train_single_model \
        --model-name Qwen/Qwen2.5-0.5B-Instruct \
        --device cuda \
        --ppo-updates 1000 \
        --output-dir models/flag_trader_qwen
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np

from crypto_bot.flag_trader.data_collector import HyperliquidDataCollector
from crypto_bot.flag_trader.environment import HyperliquidTradingEnv
from crypto_bot.flag_trader.model import FlagTraderModel
from crypto_bot.flag_trader.prompt import PromptBuilder
from crypto_bot.flag_trader.reward import REWARD_FUNCTIONS
from crypto_bot.flag_trader.supervised_warmstart import SupervisedWarmStart
from crypto_bot.flag_trader.trainer import PPOTrainer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def load_candles_by_symbol(
    data_dir: Path,
    min_volume: float = 0.0,
) -> dict[str, list[dict[str, float]]]:
    """Load candle data from parquet files, optionally filtering by volume.

    Args:
        data_dir: Directory containing parquet files.
        min_volume: Minimum average 24h volume to include an asset.

    Returns:
        Dict mapping symbol -> list of candle dicts.
    """
    collector = HyperliquidDataCollector(data_dir=data_dir)
    available = collector.list_available()
    if not available:
        raise FileNotFoundError(
            f"No candle data in {data_dir}. Run download_candles.py first."
        )

    result: dict[str, list[dict[str, float]]] = {}
    skipped_volume: int = 0

    for symbol in available:
        df = collector.load_candles(symbol)
        candles: list[dict[str, float]] = [
            {
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            }
            for _, row in df.iterrows()
        ]

        # Filter by average daily volume (96 bars = 1 day at 15m)
        if min_volume > 0 and candles:
            avg_bar_vol = sum(c["volume"] for c in candles) / len(candles)
            avg_daily_vol = avg_bar_vol * 96  # 15m bars per day
            if avg_daily_vol < min_volume:
                skipped_volume += 1
                continue

        result[symbol] = candles

    total_bars = sum(len(c) for c in result.values())
    logger.info(
        "Loaded %d assets (%d total candles), skipped %d below min_volume=$%.0f",
        len(result),
        total_bars,
        skipped_volume,
        min_volume,
    )
    return result


def build_envs(
    candles_by_symbol: dict[str, list[dict[str, float]]],
    max_assets: int,
    reward_fn_name: str = "enhanced_sharpe",
    window_size: int = 20,
) -> dict[str, HyperliquidTradingEnv]:
    """Create trading environments for the top assets by data length.

    Args:
        candles_by_symbol: Mapping of symbol -> candle list.
        max_assets: Maximum number of assets to create envs for.
        reward_fn_name: Name of reward function from REWARD_FUNCTIONS.
        window_size: Observation window size.

    Returns:
        Dict mapping symbol -> HyperliquidTradingEnv.
    """
    reward_fn = REWARD_FUNCTIONS[reward_fn_name]

    # Sort by data length (descending) and take top N
    sorted_symbols = sorted(
        candles_by_symbol.keys(),
        key=lambda s: len(candles_by_symbol[s]),
        reverse=True,
    )
    selected = sorted_symbols[:max_assets]

    envs: dict[str, HyperliquidTradingEnv] = {}
    min_candles = window_size + 10  # need at least window + some steps

    for symbol in selected:
        candles = candles_by_symbol[symbol]
        if len(candles) < min_candles:
            logger.warning(
                "Skipping %s: only %d candles (need %d)",
                symbol,
                len(candles),
                min_candles,
            )
            continue

        candle_array = np.array(
            [[c["open"], c["high"], c["low"], c["close"], c["volume"]] for c in candles],
            dtype=np.float32,
        )
        env = HyperliquidTradingEnv(
            candles=candle_array,
            reward_fn=reward_fn,
            window_size=window_size,
        )
        # Store symbol for market context in trainer
        env.symbol = symbol  # type: ignore[attr-defined]
        envs[symbol] = env

    logger.info("Created %d trading environments", len(envs))
    return envs


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train a single FLAG-Trader model",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--model-name",
        default="Qwen/Qwen2.5-0.5B-Instruct",
        help="HuggingFace model identifier",
    )
    parser.add_argument(
        "--data-dir",
        default="data/candles",
        help="Candle parquet directory",
    )
    parser.add_argument(
        "--output-dir",
        default="models/flag_trader_best",
        help="Directory to save checkpoints",
    )
    parser.add_argument(
        "--device",
        default="auto",
        choices=["auto", "cpu", "cuda", "mps"],
        help="Device for model training",
    )
    parser.add_argument(
        "--warmstart-steps",
        type=int,
        default=500,
        help="Number of supervised warm-start gradient steps",
    )
    parser.add_argument(
        "--ppo-updates",
        type=int,
        default=1000,
        help="Number of PPO update cycles",
    )
    parser.add_argument(
        "--steps-per-rollout",
        type=int,
        default=200,
        help="Steps per rollout collection",
    )
    parser.add_argument(
        "--lr",
        type=float,
        default=3e-5,
        help="Learning rate for PPO optimizer",
    )
    parser.add_argument(
        "--freeze-pct",
        type=float,
        default=0.8,
        help="Fraction of transformer layers to freeze (bottom)",
    )
    parser.add_argument(
        "--max-train-assets",
        type=int,
        default=30,
        help="Max number of assets for PPO training",
    )
    parser.add_argument(
        "--min-volume",
        type=float,
        default=500_000,
        help="Minimum average 24h volume to include an asset",
    )
    args = parser.parse_args()

    # Resolve paths relative to project root
    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir

    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    logger.info("=" * 60)
    logger.info("FLAG-Trader Single Model Training")
    logger.info("=" * 60)
    logger.info("Model:          %s", args.model_name)
    logger.info("Device:         %s", args.device)
    logger.info("Freeze:         %.0f%%", args.freeze_pct * 100)
    logger.info("Warm-start:     %d steps", args.warmstart_steps)
    logger.info("PPO updates:    %d", args.ppo_updates)
    logger.info("Steps/rollout:  %d", args.steps_per_rollout)
    logger.info("LR:             %.1e", args.lr)
    logger.info("Max assets:     %d", args.max_train_assets)
    logger.info("Min volume:     $%.0f", args.min_volume)
    logger.info("Output:         %s", output_dir)
    logger.info("=" * 60)

    # ── 1. Load candles ──────────────────────────────────────────
    logger.info("[1/5] Loading candle data from %s ...", data_dir)
    candles_by_symbol = load_candles_by_symbol(data_dir, min_volume=args.min_volume)
    if not candles_by_symbol:
        logger.error("No assets passed volume filter. Aborting.")
        sys.exit(1)

    # ── 2. Create model ─────────────────────────────────────────
    logger.info("[2/5] Creating FlagTraderModel ...")
    model = FlagTraderModel(
        model_name=args.model_name,
        freeze_pct=args.freeze_pct,
        device=args.device,
    )
    logger.info(
        "Model loaded on %s | trainable params: %d",
        model.device,
        sum(p.numel() for p in model.parameters() if p.requires_grad),
    )

    prompt_builder = PromptBuilder()

    # ── 3. Supervised warm-start ─────────────────────────────────
    logger.info("[3/5] Supervised warm-start (%d steps) ...", args.warmstart_steps)
    warmstart = SupervisedWarmStart(model=model, prompt_builder=prompt_builder)
    ws_stats = warmstart.train(
        candles_by_symbol=candles_by_symbol,
        num_steps=args.warmstart_steps,
    )
    logger.info("Warm-start complete: %s", ws_stats)

    # ── 4. PPO training ─────────────────────────────────────────
    logger.info("[4/5] PPO training (%d updates) ...", args.ppo_updates)

    envs = build_envs(
        candles_by_symbol,
        max_assets=args.max_train_assets,
        reward_fn_name="enhanced_sharpe",
    )
    if not envs:
        logger.error("No valid environments created. Aborting.")
        sys.exit(1)

    trainer = PPOTrainer(
        model=model,
        prompt_builder=prompt_builder,
        lr=args.lr,
    )

    checkpoint_interval: int = 500
    all_stats: list[dict] = []

    for update_idx in range(1, args.ppo_updates + 1):
        # Collect rollout across multiple assets (blocks of 20 steps)
        rollout_stats = trainer.collect_rollout_multi_asset(
            envs,
            num_steps=args.steps_per_rollout,
            steps_per_block=20,
        )

        # PPO update
        update_stats = trainer.update()

        stats = {**rollout_stats, **update_stats, "update": update_idx}
        all_stats.append(stats)

        # Log every 100 updates
        if update_idx % 100 == 0:
            elapsed = time.time() - t0
            logger.info(
                "Update %d/%d | reward=%.4f | policy_loss=%.4f | "
                "value_loss=%.4f | entropy=%.4f | assets=%d | elapsed=%.0fs",
                update_idx,
                args.ppo_updates,
                stats["mean_reward"],
                stats["policy_loss"],
                stats["value_loss"],
                stats["entropy"],
                stats.get("num_assets_used", 0),
                elapsed,
            )

        # Periodic checkpoint
        if update_idx % checkpoint_interval == 0:
            ckpt_path = output_dir / f"checkpoint_{update_idx}.pt"
            model.save_trainable(ckpt_path)
            logger.info("Saved checkpoint: %s", ckpt_path)

    # ── 5. Save final model ──────────────────────────────────────
    logger.info("[5/5] Saving final model ...")
    final_path = output_dir / "final_model.pt"
    model.save_trainable(final_path)

    elapsed_total = time.time() - t0
    logger.info("=" * 60)
    logger.info("Training complete!")
    logger.info("Final checkpoint: %s", final_path)
    logger.info("Total time: %.1f minutes", elapsed_total / 60)
    logger.info("Total PPO updates: %d", len(all_stats))
    if all_stats:
        last = all_stats[-1]
        logger.info(
            "Last stats: reward=%.4f, policy_loss=%.4f, value_loss=%.4f",
            last["mean_reward"],
            last["policy_loss"],
            last["value_loss"],
        )
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
