#!/usr/bin/env python3
"""Retrain the XGBoost trade selection model.

Usage: python3 -m simple_bot.scripts.retrain_model [--days 90]
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backtesting.api import get_all_assets
from backtesting.config import load_config
from simple_bot.services.ml_dataset import generate_dataset
from simple_bot.services.ml_model import MLTradeModel

DEFAULT_MODEL_PATH = "models/trade_model.joblib"
DEFAULT_DAYS = 30


def main() -> None:
    parser = argparse.ArgumentParser(description="Retrain XGBoost trade model")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="Days of data")
    parser.add_argument("--output", type=str, default=DEFAULT_MODEL_PATH, help="Output path")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
    )
    logger = logging.getLogger("retrain")

    # 1. Get all symbols
    logger.info("Fetching asset list from Hyperliquid...")
    cfg = load_config()
    all_assets = get_all_assets()
    symbols = [s for s in all_assets if s not in cfg.exclude_symbols]
    logger.info(
        "Training on %d symbols (%d excluded)",
        len(symbols),
        len(all_assets) - len(symbols),
    )

    # 2. Generate dataset
    logger.info("Generating dataset (%d days)...", args.days)
    df = generate_dataset(symbols, days=args.days, cfg=cfg)

    if df.empty or len(df) < 50:
        logger.error("Insufficient data: %d samples (need >= 50)", len(df))
        sys.exit(1)

    # 3. Train model
    logger.info("Training XGBoost model...")
    model = MLTradeModel()
    metrics = model.train(df)

    # 4. Save model
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    model.save(args.output)

    # 5. Report
    logger.info("=" * 60)
    logger.info("TRAINING COMPLETE")
    logger.info("=" * 60)
    logger.info(
        "Dataset: %d samples (%d wins, %d losses)",
        metrics["n_samples"],
        metrics["n_positive"],
        metrics["n_negative"],
    )
    logger.info("CV AUC: %.4f +/- %.4f", metrics["cv_auc_mean"], metrics["cv_auc_std"])
    logger.info("In-sample accuracy: %.4f", metrics["accuracy"])
    logger.info("Feature importances:")
    sorted_imp = sorted(
        metrics["feature_importances"].items(),
        key=lambda x: x[1],
        reverse=True,
    )
    for name, imp in sorted_imp:
        logger.info("  %20s: %.4f", name, imp)
    logger.info("Model saved to: %s", args.output)


if __name__ == "__main__":
    main()
