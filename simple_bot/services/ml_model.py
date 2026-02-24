"""XGBoost ML model for trade selection.

Replaces heuristic filters + LLM veto with a trained binary classifier
that predicts P(take-profit hit) for each trade setup.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.model_selection import TimeSeriesSplit, cross_val_score
from sklearn.metrics import accuracy_score, roc_auc_score

from ..core.models import MarketState

logger = logging.getLogger(__name__)


class MLTradeModel:
    """XGBoost model for trade selection. Replaces all filters + LLM."""

    FEATURES = [
        "adx",
        "rsi",
        "atr_pct",
        "ema_spread_pct",
        "volume_ratio",
        "bb_position",
        "ema9_slope",
        "ema21_slope",
        "close_vs_ema200",
    ]

    _DEFAULT_PARAMS: dict = {
        "n_estimators": 100,
        "max_depth": 4,
        "learning_rate": 0.1,
        "min_child_weight": 5,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "objective": "binary:logistic",
        "eval_metric": "auc",
    }

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or {}
        self._model: Optional[xgb.XGBClassifier] = None
        self._feature_importances: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def train(self, df: pd.DataFrame) -> dict:
        """Train XGBoost on DataFrame with FEATURES + 'label' columns.

        Uses TimeSeriesSplit(n_splits=5) for cross-validation.

        Returns dict with:
            accuracy, auc, feature_importances, n_samples, n_positive, n_negative
        """
        missing = [f for f in self.FEATURES if f not in df.columns]
        if missing:
            raise ValueError(f"Missing feature columns: {missing}")
        if "label" not in df.columns:
            raise ValueError("DataFrame must contain a 'label' column")

        X = df[self.FEATURES].astype(float)
        y = df["label"].astype(int)

        n_positive = int(y.sum())
        n_negative = int(len(y) - n_positive)

        # Merge default params with any user overrides
        params = {**self._DEFAULT_PARAMS, **self._config.get("xgb_params", {})}

        self._model = xgb.XGBClassifier(**params)

        # Time-series aware cross-validation
        tscv = TimeSeriesSplit(n_splits=5)
        cv_scores = cross_val_score(
            self._model, X, y, cv=tscv, scoring="roc_auc"
        )

        # Final fit on full data
        self._model.fit(X, y)

        assert self._model is not None  # for type checker

        # Feature importances
        importances = self._model.feature_importances_
        self._feature_importances = {
            name: round(float(imp), 4)
            for name, imp in zip(self.FEATURES, importances)
        }

        # In-sample metrics (for reporting only; CV score is the real metric)
        y_pred = self._model.predict(X)
        y_proba = self._model.predict_proba(X)[:, 1]

        metrics = {
            "accuracy": round(float(accuracy_score(y, y_pred)), 4),
            "auc": round(float(roc_auc_score(y, y_proba)), 4),
            "cv_auc_mean": round(float(np.mean(cv_scores)), 4),
            "cv_auc_std": round(float(np.std(cv_scores)), 4),
            "feature_importances": self._feature_importances,
            "n_samples": len(df),
            "n_positive": n_positive,
            "n_negative": n_negative,
        }

        logger.info(
            "Model trained: %d samples (%d pos / %d neg), "
            "CV AUC=%.4f +/- %.4f",
            len(df),
            n_positive,
            n_negative,
            metrics["cv_auc_mean"],
            metrics["cv_auc_std"],
        )

        return metrics

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(self, features: dict) -> tuple[float, str]:
        """Predict P(TP) for a setup.

        Args:
            features: dict with keys matching FEATURES

        Returns:
            (probability float 0-1, explanation string with top 3 features)
        """
        if self._model is None:
            raise RuntimeError("Model not loaded. Call load() or train() first.")

        row = pd.DataFrame([features])[self.FEATURES].astype(float)
        proba = float(self._model.predict_proba(row)[:, 1][0])

        # Build explanation from top 3 feature importances
        sorted_feats = sorted(
            self._feature_importances.items(), key=lambda x: x[1], reverse=True
        )[:3]
        parts = [f"{name}({imp:.2f})" for name, imp in sorted_feats]
        explanation = "top: " + ", ".join(parts)

        return proba, explanation

    # ------------------------------------------------------------------
    # Feature extraction
    # ------------------------------------------------------------------

    def extract_features(self, state: MarketState) -> dict:
        """Extract features from a live MarketState for prediction.

        Args:
            state: MarketState with all indicators

        Returns:
            dict with keys matching FEATURES
        """
        close = float(state.close)
        ema9 = float(state.ema9) if state.ema9 is not None else close
        ema21 = float(state.ema21) if state.ema21 is not None else close
        ema200 = float(state.ema200)

        # Bollinger band position: (close - lower) / (upper - lower)
        bb_position = 0.5
        if state.bb_upper is not None and state.bb_lower is not None:
            bb_range = float(state.bb_upper) - float(state.bb_lower)
            if bb_range > 0:
                bb_position = (close - float(state.bb_lower)) / bb_range

        # EMA spread as percentage
        ema_spread_pct = abs(ema9 - ema21) / ema21 * 100 if ema21 != 0 else 0.0

        # Close vs EMA200 as percentage
        close_vs_ema200 = (close - ema200) / ema200 * 100 if ema200 != 0 else 0.0

        return {
            "adx": float(state.adx),
            "rsi": float(state.rsi),
            "atr_pct": float(state.atr_pct),
            "ema_spread_pct": ema_spread_pct,
            "volume_ratio": float(state.volume_ratio) if state.volume_ratio is not None else 1.0,
            "bb_position": bb_position,
            "ema9_slope": float(state.ema9_slope),
            "ema21_slope": float(state.ema21_slope),
            "close_vs_ema200": close_vs_ema200,
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: str) -> None:
        """Save model and metadata to disk."""
        if self._model is None:
            raise RuntimeError("No model to save. Call train() first.")

        payload = {
            "model": self._model,
            "feature_importances": self._feature_importances,
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(payload, path)
        logger.info("Model saved to %s", path)

    def load(self, path: str) -> bool:
        """Load trained model from disk. Returns True if successful."""
        try:
            payload = joblib.load(path)
            self._model = payload["model"]
            self._feature_importances = payload.get("feature_importances", {})

            # Warn if saved model was trained with different features
            model_features = set(self._feature_importances.keys())
            expected_features = set(self.FEATURES)
            if model_features and model_features != expected_features:
                logger.warning(
                    "ML model features mismatch - retrain required: "
                    "model=%s, expected=%s",
                    sorted(model_features),
                    sorted(expected_features),
                )

            logger.info("Model loaded from %s", path)
            return True
        except FileNotFoundError:
            logger.warning("Model file not found: %s", path)
            return False
        except Exception:
            logger.exception("Failed to load model from %s", path)
            return False

    @property
    def is_loaded(self) -> bool:
        return self._model is not None
