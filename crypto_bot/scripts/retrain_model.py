#!/usr/bin/env python3
"""Retrain the XGBoost + LightGBM ensemble trade selection model.

Usage:
    python3 -m crypto_bot.scripts.retrain_model [--days 90]
    python3 -m crypto_bot.scripts.retrain_model --exclude-features log_volume_24h,funding_rate
    python3 -m crypto_bot.scripts.retrain_model --min-volume 500000

# FEATURE PARITY NOTES (training vs live data sources):
#
# - log_volume_24h:
#     Training: sum(candle_volume_base * close_price) over last 96 bars → log10.
#     Live: exchange API dayNtlVlm (24h notional volume in USD) → log10.
#     Assessment: Candle "v" field is in BASE UNITS (e.g. BTC), so
#     sum(v * close) ≈ dayNtlVlm. They should be CLOSE in practice,
#     but not identical: candle-based sums 96 discrete bars while
#     dayNtlVlm is a rolling 24h aggregate from the exchange.
#     OPTION B viable: keep this feature, values are approximately coherent.
#
# - funding_rate:
#     Training: always 0.0 (not available from candleSnapshot endpoint).
#     Live: actual funding rate from meta_and_asset_ctxs API.
#     Assessment: Historical funding IS available via the "fundingHistory"
#     endpoint (POST /info, type="fundingHistory", coin=X, startTime=ms).
#     Returns array of {coin, fundingRate, premium, time} records.
#     OPTION B viable: could fetch historical funding and align by timestamp,
#     but requires a new API integration in ml_dataset.py.
#     For now, use --exclude-features funding_rate to avoid the mismatch.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backtesting.api import get_all_assets, get_asset_volumes
from backtesting.config import load_config
from crypto_bot.services.ml_dataset import generate_dataset
from crypto_bot.services.ml_model import MLTradeModel

DEFAULT_MODEL_PATH = "models/trade_model.joblib"
DEFAULT_DAYS = 90
DEFAULT_EXCLUDE = "log_volume_24h,funding_rate"


def main() -> None:
    parser = argparse.ArgumentParser(description="Retrain XGBoost+LGB trade model")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="Days of data")
    parser.add_argument("--output", type=str, default=DEFAULT_MODEL_PATH, help="Output path")
    parser.add_argument("--min-volume", type=float, default=500_000,
                        help="Min 24h USD volume to include asset (default 500000)")
    parser.add_argument(
        "--exclude-features",
        type=str,
        default=DEFAULT_EXCLUDE,
        help=(
            "Comma-separated feature names to drop before training. "
            "These features are still computed in the dataset for forward "
            "compatibility, but excluded from model fitting. "
            f"Default: '{DEFAULT_EXCLUDE}'"
        ),
    )
    parser.add_argument(
        "--no-exclude",
        action="store_true",
        help="Train with ALL 27 features (override --exclude-features)",
    )
    # Labeling overrides (decouple training target from trading.yaml)
    parser.add_argument("--label-tp", type=float, default=None,
                        help="Override TP%% for labeling (e.g. 2.5 for 2.5%%)")
    parser.add_argument("--label-sl", type=float, default=None,
                        help="Override SL%% for labeling (e.g. 1.0 for 1.0%%)")
    parser.add_argument("--label-max-bars", type=int, default=None,
                        help="Override max forward bars for labeling (default 24)")
    parser.add_argument("--label-slippage", type=float, default=None,
                        help="Override slippage%% for labeling (default 0.05)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
    )
    logger = logging.getLogger("retrain")

    # Parse excluded features
    if args.no_exclude:
        exclude_features: list[str] = []
    elif args.exclude_features:
        exclude_features = [f.strip() for f in args.exclude_features.split(",") if f.strip()]
    else:
        exclude_features = []

    # Validate excluded feature names
    valid_features = set(MLTradeModel.FEATURES)
    invalid = [f for f in exclude_features if f not in valid_features]
    if invalid:
        logger.error("Unknown feature names in --exclude-features: %s", invalid)
        logger.error("Valid features: %s", sorted(valid_features))
        sys.exit(1)

    if exclude_features:
        logger.info("=" * 60)
        logger.info("FEATURE EXCLUSION (Option A)")
        logger.info("  Excluding %d features: %s", len(exclude_features), exclude_features)
        logger.info("  Training with %d / %d features",
                     len(valid_features) - len(exclude_features), len(valid_features))
        logger.info("=" * 60)
    else:
        logger.info("Training with ALL %d features (no exclusions)", len(valid_features))

    # 1. Get all symbols and volumes
    logger.info("Fetching asset list and volumes from Hyperliquid...")
    cfg = load_config()
    all_assets = get_all_assets()
    symbols = [s for s in all_assets if s not in cfg.exclude_symbols]
    logger.info(
        "Found %d symbols (%d excluded by config)",
        len(symbols),
        len(all_assets) - len(symbols),
    )

    # Fetch 24h volumes for universe filtering
    logger.info("Fetching 24h volumes (min $%.0f)...", args.min_volume)
    volumes = get_asset_volumes()
    above = sum(1 for s in symbols if volumes.get(s, 0) >= args.min_volume)
    below = len(symbols) - above
    logger.info(
        "Volume filter: %d/%d assets >= $%.0f 24h volume (%d filtered out)",
        above, len(symbols), args.min_volume, below,
    )

    # 2. Generate dataset
    # Convert label overrides from percentage to fraction
    label_tp = args.label_tp / 100.0 if args.label_tp is not None else None
    label_sl = args.label_sl / 100.0 if args.label_sl is not None else None
    label_slippage = args.label_slippage / 100.0 if args.label_slippage is not None else None

    if label_tp is not None or label_sl is not None:
        logger.info("LABELING OVERRIDES: TP=%s SL=%s max_bars=%s slippage=%s",
                     f"{args.label_tp}%" if args.label_tp else "cfg",
                     f"{args.label_sl}%" if args.label_sl else "cfg",
                     args.label_max_bars or "default(24)",
                     f"{args.label_slippage}%" if args.label_slippage else "default(0.05%)")

    logger.info("Generating dataset (%d days)...", args.days)
    df = generate_dataset(
        symbols, days=args.days, cfg=cfg,
        asset_volumes=volumes, min_volume_24h=args.min_volume,
        label_tp_pct=label_tp,
        label_sl_pct=label_sl,
        label_max_forward_bars=args.label_max_bars,
        label_slippage_pct=label_slippage,
    )

    if df.empty or len(df) < 50:
        logger.error("Insufficient data: %d samples (need >= 50)", len(df))
        sys.exit(1)

    # Log dataset statistics
    n_symbols = df["symbol"].nunique() if "symbol" in df.columns else 0
    logger.info("Dataset: %d samples from %d assets", len(df), n_symbols)
    logger.info(
        "Label distribution: %d wins (%.1f%%) / %d losses (%.1f%%)",
        int(df["label"].sum()),
        df["label"].mean() * 100,
        int(len(df) - df["label"].sum()),
        (1 - df["label"].mean()) * 100,
    )

    # 3. Train model (pass exclude_features to drop before fitting)
    logger.info("Training XGBoost + LightGBM ensemble model...")
    model = MLTradeModel()
    metrics = model.train(df, exclude_features=exclude_features or None)

    # 4. Save model
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    model.save(args.output)

    # 5. Report
    logger.info("=" * 60)
    logger.info("TRAINING COMPLETE")
    logger.info("=" * 60)
    logger.info(
        "Dataset: %d samples (%d wins, %d losses) from %d assets",
        metrics["n_samples"],
        metrics["n_positive"],
        metrics["n_negative"],
        n_symbols,
    )
    if exclude_features:
        logger.info(
            "Excluded features: %s (%d/%d used)",
            exclude_features,
            len(valid_features) - len(exclude_features),
            len(valid_features),
        )
    logger.info("Ensemble: %s", metrics.get("ensemble", "xgb"))
    logger.info("CV AUC: %.4f +/- %.4f", metrics["cv_auc_mean"], metrics["cv_auc_std"])
    logger.info("Walk-forward AUC: %.4f", metrics.get("wf_auc", 0.0))
    logger.info("In-sample accuracy: %.4f", metrics["accuracy"])
    logger.info("Optimal threshold: %.4f", metrics.get("optimal_threshold", 0.55))
    logger.info("Feature importances:")
    sorted_imp = sorted(
        metrics["feature_importances"].items(),
        key=lambda x: x[1],
        reverse=True,
    )
    for rank, (name, imp) in enumerate(sorted_imp, 1):
        logger.info("  %2d. %20s: %.4f", rank, name, imp)
    logger.info("Model saved to: %s", args.output)


if __name__ == "__main__":
    main()
