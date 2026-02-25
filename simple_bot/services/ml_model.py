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
from sklearn.metrics import accuracy_score, precision_recall_curve, roc_auc_score

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
        "regime_encoded",   # TREND=2.0, CHAOS=1.0, RANGE=0.0
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
        self._optimal_threshold: Optional[float] = None

    @property
    def optimal_threshold(self) -> Optional[float]:
        return self._optimal_threshold

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def train(self, df: pd.DataFrame) -> dict:
        """Train XGBoost on DataFrame with FEATURES + 'label' columns.

        Pipeline:
        1. CV AUC via TimeSeriesSplit(n_splits=5) for reporting.
        2. Threshold calibration on a genuine holdout (last 20%) — a
           temporary model is trained on the first 80% only, then predicts
           on the holdout to pick the optimal F0.5 threshold.
        3. Final model trained on the first 80% with early stopping on
           the holdout set.

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

        # Handle class imbalance
        scale_pos_weight = max(1.0, n_negative / n_positive) if n_positive > 0 else 1.0

        # Merge default params with class weight and any user overrides
        params = {
            **self._DEFAULT_PARAMS,
            "scale_pos_weight": scale_pos_weight,
            "early_stopping_rounds": 20,
            **self._config.get("xgb_params", {}),
        }

        # -- Step 1: CV AUC (for reporting) --------------------------------
        cv_model = xgb.XGBClassifier(**{
            k: v for k, v in params.items() if k != "early_stopping_rounds"
        })
        tscv = TimeSeriesSplit(n_splits=5)
        cv_scores = cross_val_score(
            cv_model, X, y, cv=tscv, scoring="roc_auc"
        )

        # -- Step 2: Threshold calibration on genuine holdout --------------
        n_holdout = max(100, int(len(X) * 0.2))
        X_train, X_hold = X.iloc[:-n_holdout], X.iloc[-n_holdout:]
        y_train, y_hold = y.iloc[:-n_holdout], y.iloc[-n_holdout:]

        cal_model = xgb.XGBClassifier(**{
            k: v for k, v in params.items() if k != "early_stopping_rounds"
        })
        cal_model.fit(X_train, y_train)
        cal_probs = cal_model.predict_proba(X_hold)[:, 1]
        self._optimal_threshold = self._calibrate_threshold_from_probs(
            cal_probs, y_hold
        )

        # -- Step 3: Final model with early stopping on holdout ------------
        self._model = xgb.XGBClassifier(**params)
        self._model.fit(
            X_train, y_train,
            eval_set=[(X_hold, y_hold)],
            verbose=False,
        )

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
            "optimal_threshold": self._optimal_threshold,
        }

        logger.info(
            "Model trained: %d samples (%d pos / %d neg), "
            "CV AUC=%.4f +/- %.4f, optimal_threshold=%.4f",
            len(df),
            n_positive,
            n_negative,
            metrics["cv_auc_mean"],
            metrics["cv_auc_std"],
            self._optimal_threshold if self._optimal_threshold is not None else 0.55,
        )

        return metrics

    def _calibrate_threshold_from_probs(
        self, y_proba: np.ndarray, y_true: pd.Series
    ) -> float:
        """Find optimal threshold using F-beta (beta=0.5, precision-weighted).

        Operates on pre-computed probabilities from a model that has NOT seen
        y_true during training (holdout data), preventing calibration leakage.

        Only considers thresholds where precision >= 0.55.
        Clamps result to [0.50, 0.70], default 0.55 if not enough data.
        """
        default = 0.55
        try:
            if len(y_true) < 10 or y_true.sum() < 2:
                return default

            precision, recall, thresholds = precision_recall_curve(
                y_true, y_proba
            )

            # F-beta with beta=0.5 (precision-weighted)
            beta = 0.5
            beta_sq = beta ** 2
            best_threshold = default
            best_fbeta = -1.0

            for i in range(len(thresholds)):
                p = precision[i]
                r = recall[i]
                if p < 0.55:
                    continue
                denom = beta_sq * p + r
                if denom <= 0:
                    continue
                fbeta = (1 + beta_sq) * (p * r) / denom
                if fbeta > best_fbeta:
                    best_fbeta = fbeta
                    best_threshold = float(thresholds[i])

            # Clamp to [0.50, 0.70]
            best_threshold = max(0.50, min(0.70, best_threshold))

            logger.info(
                "Threshold calibration: %.4f (F0.5=%.4f)",
                best_threshold,
                best_fbeta if best_fbeta >= 0 else 0.0,
            )
            return best_threshold

        except Exception:
            logger.warning("Threshold calibration failed, using default %.2f", default)
            return default

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

    def extract_features(self, state: MarketState, **kwargs: float) -> dict:
        """Extract features from a live MarketState for prediction.

        Args:
            state: MarketState with all indicators
            **kwargs: Ignored. Accepts (but discards) legacy kwargs like
                spread_pct for backward compatibility.

        Returns:
            dict with keys matching FEATURES
        """
        from ..core.models import Regime

        _REGIME_MAP = {Regime.TREND: 2.0, Regime.CHAOS: 1.0, Regime.RANGE: 0.0}
        regime_encoded = _REGIME_MAP.get(state.regime, 1.0)

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
            "regime_encoded": regime_encoded,
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
            "optimal_threshold": self._optimal_threshold,
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
            self._optimal_threshold = payload.get("optimal_threshold", None)

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
