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

# IMPORTANT: Import LightGBM BEFORE XGBoost on macOS ARM64.
# Both ship their own libomp.dylib; whichever loads first "wins" the
# dynamic linker symbol table.  If XGBoost's libomp loads first and
# LightGBM tries to use it later, a SIGSEGV occurs during LGB.fit().
try:
    import lightgbm as lgb
    HAS_LIGHTGBM = True
except ImportError:
    HAS_LIGHTGBM = False

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
        "volume_ratio",
        "bb_position",
        "ema9_slope",
        "ema21_slope",
        "close_vs_ema200",
        "regime_encoded",   # TREND=2.0, CHAOS=1.0, RANGE=0.0
        "session",          # 0=Asia(00-08), 1=London(08-13), 2=US(13-21), 3=LateNight(21-00)
        "signal_type",      # 0.0=EMA crossover, 1.0=volume breakout, 2.0=momentum burst
        "candle_body_pct",  # |close-open|/open * 100 — price conviction
        "rsi_slope",        # RSI[i] - RSI[i-2] — RSI acceleration
        # --- Tier 1 (v2) ---
        "is_weekend",       # 1 if Saturday/Sunday, 0 otherwise
        "atr_percentile",   # percentile rank of ATR in last 100 bars [0,1]
        "signed_ema_spread", # (ema9 - ema21) / ema21 * 100 (signed, not abs)
        "direction",        # 1.0=LONG, -1.0=SHORT
        # --- Tier 2 (v3) ---
        "btc_trend",        # BTC EMA9 vs EMA21: +1/-1/0
        "btc_rsi",          # BTC RSI(14)
        "btc_ema9_slope",   # BTC EMA9 4-bar slope
        "tf_alignment",     # 1h vs 15m direction: +1=agree, -1=disagree
        "rsi_1h",           # RSI(56) — 1h equivalent
        "adx_1h",           # ADX(56) — 1h equivalent
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
        self._lgb_model: Optional[object] = None  # LightGBM classifier
        self._feature_importances: dict[str, float] = {}
        self._optimal_threshold: Optional[float] = None

    @property
    def optimal_threshold(self) -> Optional[float]:
        return self._optimal_threshold

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def train(self, df: pd.DataFrame) -> dict:
        """Train XGBoost + LightGBM ensemble on DataFrame with FEATURES + 'label'.

        Pipeline:
        1. Data preparation and 3-way split.
        2. LightGBM fit FIRST (macOS ARM64: LGB must load libomp before XGB).
        3. XGBoost CV AUC (for reporting).
        4. XGBoost fit with early stopping.
        5. Threshold calibration on held-out calibration set.
        6. Walk-forward validation for realistic OOS metric.

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

        # Time-weighted sample weights: exp(-days_ago / 90)
        # Recent samples matter more than old ones
        sample_weight = None
        if "timestamp" in df.columns:
            latest_ts = df["timestamp"].max()
            days_ago = (latest_ts - df["timestamp"]) / (86_400_000)  # ms to days
            sample_weight = np.exp(-days_ago.values / 90.0).astype(float)

        # Merge default params with class weight and any user overrides
        params = {
            **self._DEFAULT_PARAMS,
            "scale_pos_weight": scale_pos_weight,
            "early_stopping_rounds": 20,
            **self._config.get("xgb_params", {}),
        }

        # -- Step 1: 3-way split (75% train / 15% validation / 10% calibration)
        n_cal = max(50, int(len(X) * 0.10))
        n_val = max(50, int(len(X) * 0.15))
        n_train = len(X) - n_val - n_cal

        X_train = X.iloc[:n_train]
        y_train = y.iloc[:n_train]
        X_val = X.iloc[n_train:n_train + n_val]
        y_val = y.iloc[n_train:n_train + n_val]
        X_cal = X.iloc[n_train + n_val:]
        y_cal = y.iloc[n_train + n_val:]
        sw_train = sample_weight[:n_train] if sample_weight is not None else None

        # -- Step 2: LightGBM ensemble FIRST (if available) ----------------
        # IMPORTANT: On macOS ARM64, LightGBM must load its libomp.dylib
        # before XGBoost loads its own copy, otherwise a SIGSEGV occurs.
        self._lgb_model = None
        if HAS_LIGHTGBM:
            lgb_params = {
                "n_estimators": params.get("n_estimators", 100),
                "max_depth": params.get("max_depth", 4),
                "learning_rate": params.get("learning_rate", 0.1),
                "min_child_weight": params.get("min_child_weight", 5),
                "subsample": params.get("subsample", 0.8),
                "colsample_bytree": params.get("colsample_bytree", 0.8),
                "scale_pos_weight": scale_pos_weight,
                "objective": "binary",
                "metric": "auc",
                "verbose": -1,
            }
            self._lgb_model = lgb.LGBMClassifier(**lgb_params)
            self._lgb_model.fit(
                X_train, y_train,
                sample_weight=sw_train,
                eval_set=[(X_val, y_val)],
                callbacks=[lgb.early_stopping(20, verbose=False)],
            )
            logger.info("LightGBM ensemble model trained")

        # -- Step 3: CV AUC (for reporting) --------------------------------
        # Skip full CV on large datasets (>20K) — too slow, only for reporting
        if len(X) > 20_000:
            logger.info("Large dataset (%d) — skipping CV, using walk-forward AUC only", len(X))
            cv_scores = np.array([0.0])
        else:
            cv_model = xgb.XGBClassifier(**{
                k: v for k, v in params.items() if k != "early_stopping_rounds"
            })
            tscv = TimeSeriesSplit(n_splits=5)
            cv_scores = cross_val_score(
                cv_model, X, y, cv=tscv, scoring="roc_auc",
            )

        # -- Step 4: Final XGBoost with early stopping on validation -------
        self._model = xgb.XGBClassifier(**params)
        self._model.fit(
            X_train, y_train,
            sample_weight=sw_train,
            eval_set=[(X_val, y_val)],
            verbose=False,
        )

        assert self._model is not None  # for type checker

        # -- Step 5: Threshold calibration on calibration set (ensemble) ---
        cal_xgb = self._model.predict_proba(X_cal)[:, 1]
        cal_lgb = self._lgb_model.predict_proba(X_cal)[:, 1] if self._lgb_model else cal_xgb
        cal_ensemble = (cal_xgb + cal_lgb) / 2.0
        self._optimal_threshold = self._calibrate_threshold_from_probs(
            cal_ensemble, y_cal
        )

        # -- Step 6: Walk-forward validation (expanding window) ------------
        # More realistic OOS metric: 5 folds with 100-bar embargo to avoid
        # lookahead bias.  Uses a temporary XGBoost (no early stopping) so it
        # is self-contained.
        wf_auc: float = 0.0
        try:
            n_folds = 5
            embargo = 100  # bars between train/test to prevent leakage
            n = len(X)
            fold_size = n // (n_folds + 1)  # reserve 1 chunk for initial train

            oos_preds = np.full(n, np.nan)  # out-of-sample predictions

            wf_params = {
                k: v for k, v in params.items() if k != "early_stopping_rounds"
            }

            for fold_idx in range(n_folds):
                split_point = fold_size * (fold_idx + 1)
                train_end = split_point - embargo
                test_start = split_point
                test_end = min(split_point + fold_size, n)

                if train_end < fold_size or test_start >= n or test_end <= test_start:
                    continue

                X_wf_train = X.iloc[:train_end]
                y_wf_train = y.iloc[:train_end]
                sw_wf = sample_weight[:train_end] if sample_weight is not None else None

                X_wf_test = X.iloc[test_start:test_end]
                y_wf_test = y.iloc[test_start:test_end]

                if len(y_wf_train) < 50 or len(y_wf_test) < 10:
                    continue
                if y_wf_train.nunique() < 2 or y_wf_test.nunique() < 2:
                    continue

                wf_model = xgb.XGBClassifier(**wf_params)
                wf_model.fit(X_wf_train, y_wf_train, sample_weight=sw_wf)
                fold_proba = wf_model.predict_proba(X_wf_test)[:, 1]
                oos_preds[test_start:test_end] = fold_proba

            # Compute AUC on all out-of-sample predictions
            mask = ~np.isnan(oos_preds)
            if mask.sum() >= 20 and y[mask].nunique() >= 2:
                wf_auc = round(float(roc_auc_score(y[mask], oos_preds[mask])), 4)
                logger.info(
                    "Walk-forward AUC=%.4f (%d OOS predictions across %d folds)",
                    wf_auc, int(mask.sum()), n_folds,
                )
            else:
                logger.warning(
                    "Walk-forward: insufficient OOS predictions (%d), skipping AUC",
                    int(mask.sum()),
                )
        except Exception:
            logger.exception("Walk-forward validation failed, continuing without it")

        # Feature importances (XGBoost primary)
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
            "wf_auc": wf_auc,
            "feature_importances": self._feature_importances,
            "n_samples": len(df),
            "n_positive": n_positive,
            "n_negative": n_negative,
            "optimal_threshold": self._optimal_threshold,
            "ensemble": "xgb+lgb" if self._lgb_model is not None else "xgb",
        }

        logger.info(
            "Model trained: %d samples (%d pos / %d neg), "
            "CV AUC=%.4f +/- %.4f, WF AUC=%.4f, optimal_threshold=%.4f, ensemble=%s",
            len(df),
            n_positive,
            n_negative,
            metrics["cv_auc_mean"],
            metrics["cv_auc_std"],
            metrics["wf_auc"],
            self._optimal_threshold,
            metrics["ensemble"],
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

            # Clamp to [0.50, 0.58]
            best_threshold = max(0.50, min(0.58, best_threshold))

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

        # Backward compat: if model was trained with fewer features, use only
        # the features the model knows about (e.g. 14-feature model + 25 FEATURES)
        n_model_features = getattr(self._model, "n_features_in_", row.shape[1])
        if row.shape[1] > n_model_features:
            booster = self._model.get_booster()
            model_feature_names = booster.feature_names if booster else None
            if model_feature_names:
                row = row[[f for f in model_feature_names if f in row.columns]]

        xgb_proba = float(self._model.predict_proba(row)[:, 1][0])

        # Ensemble: average XGBoost and LightGBM probabilities
        if self._lgb_model is not None:
            lgb_row = row  # same features
            n_lgb_features = getattr(self._lgb_model, "n_features_in_", lgb_row.shape[1])
            if lgb_row.shape[1] > n_lgb_features:
                lgb_row = lgb_row.iloc[:, :n_lgb_features]
            lgb_proba = float(self._lgb_model.predict_proba(lgb_row)[:, 1][0])
            proba = (xgb_proba + lgb_proba) / 2.0
        else:
            proba = xgb_proba

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

    def extract_features(
        self,
        state: MarketState,
        signal_type: float = 0.0,
        direction: float = 1.0,
        btc_state: Optional[MarketState] = None,
        **kwargs: float,
    ) -> dict:
        """Extract features from a live MarketState for prediction.

        Args:
            state: MarketState with all indicators
            signal_type: 0.0 for EMA crossover, 1.0 for volume breakout
            direction: 1.0 for LONG, -1.0 for SHORT
            btc_state: BTC MarketState for context features (None = defaults)
            **kwargs: Ignored. Accepts (but discards) legacy kwargs like
                spread_pct for backward compatibility.

        Returns:
            dict with keys matching FEATURES
        """
        from ..core.models import Regime

        _REGIME_MAP = {Regime.TREND: 2.0, Regime.CHAOS: 1.0, Regime.RANGE: 0.0}
        regime_encoded = _REGIME_MAP.get(state.regime, 1.0)

        close = float(state.close)
        open_price = float(state.open)
        ema9 = float(state.ema9) if state.ema9 is not None else close
        ema21 = float(state.ema21) if state.ema21 is not None else close
        ema200 = float(state.ema200)

        # Bollinger band position: (close - lower) / (upper - lower)
        bb_position = 0.5
        if state.bb_upper is not None and state.bb_lower is not None:
            bb_range = float(state.bb_upper) - float(state.bb_lower)
            if bb_range > 0:
                bb_position = (close - float(state.bb_lower)) / bb_range

        # Signed EMA spread (directional)
        signed_ema_spread = (ema9 - ema21) / ema21 * 100 if ema21 != 0 else 0.0

        # Close vs EMA200 as percentage
        close_vs_ema200 = (close - ema200) / ema200 * 100 if ema200 != 0 else 0.0

        # Session bin: 0=Asia(00-08), 1=London(08-13), 2=US(13-21), 3=LateNight(21-00)
        h = state.timestamp.hour
        session = 0 if h < 8 else (1 if h < 13 else (2 if h < 21 else 3))

        # Candle body percentage
        candle_body_pct = abs(close - open_price) / open_price * 100 if open_price > 0 else 0.0

        # Is weekend (Saturday=5, Sunday=6)
        is_weekend = 1 if state.timestamp.weekday() >= 5 else 0

        # ATR percentile from MarketState (computed from last 100 bars)
        atr_percentile = float(state.atr_percentile) if state.atr_percentile is not None else 0.5

        # --- BTC context features ---
        if btc_state is not None and state.symbol != "BTC":
            btc_ema9 = float(btc_state.ema9) if btc_state.ema9 is not None else 0.0
            btc_ema21 = float(btc_state.ema21) if btc_state.ema21 is not None else 0.0
            if btc_ema9 > btc_ema21:
                btc_trend = 1.0
            elif btc_ema9 < btc_ema21:
                btc_trend = -1.0
            else:
                btc_trend = 0.0
            btc_rsi = float(btc_state.rsi)
            btc_ema9_slope = float(btc_state.ema9_slope)
        else:
            btc_trend = 0.0
            btc_rsi = 50.0
            btc_ema9_slope = 0.0

        # --- Multi-TF alignment features ---
        rsi_1h = float(state.rsi_1h) if state.rsi_1h is not None else (float(state.rsi) if state.rsi is not None else 50.0)
        adx_1h = float(state.adx_1h) if state.adx_1h is not None else (float(state.adx) if state.adx is not None else 20.0)

        # TF alignment: compare 15m and 1h EMA direction
        ema9_1h = getattr(state, "ema9_1h", None)
        ema21_1h = getattr(state, "ema21_1h", None)
        if ema9_1h is not None and ema21_1h is not None:
            dir_15m = 1.0 if ema9 > ema21 else -1.0
            dir_1h = 1.0 if float(ema9_1h) > float(ema21_1h) else -1.0
            tf_alignment = 1.0 if dir_15m == dir_1h else -1.0
        else:
            tf_alignment = 0.0  # unknown

        return {
            "adx": float(state.adx),
            "rsi": float(state.rsi),
            "atr_pct": float(state.atr_pct),
            "volume_ratio": float(state.volume_ratio) if state.volume_ratio is not None else 1.0,
            "bb_position": bb_position,
            "ema9_slope": float(state.ema9_slope),
            "ema21_slope": float(state.ema21_slope),
            "close_vs_ema200": close_vs_ema200,
            "regime_encoded": regime_encoded,
            "session": session,
            "signal_type": signal_type,
            "candle_body_pct": candle_body_pct,
            "rsi_slope": float(state.rsi_slope),
            # Tier 1
            "is_weekend": is_weekend,
            "atr_percentile": atr_percentile,
            "signed_ema_spread": signed_ema_spread,
            "direction": direction,
            # Tier 2
            "btc_trend": btc_trend,
            "btc_rsi": btc_rsi,
            "btc_ema9_slope": btc_ema9_slope,
            "tf_alignment": tf_alignment,
            "rsi_1h": rsi_1h,
            "adx_1h": adx_1h,
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
            "lgb_model": self._lgb_model,
            "feature_importances": self._feature_importances,
            "optimal_threshold": self._optimal_threshold,
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(payload, path)
        logger.info("Model saved to %s (ensemble=%s)", path,
                     "xgb+lgb" if self._lgb_model else "xgb")

    def load(self, path: str) -> bool:
        """Load trained model from disk. Returns True if successful."""
        try:
            payload = joblib.load(path)
            self._model = payload["model"]
            self._lgb_model = payload.get("lgb_model", None)
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

            ensemble = "xgb+lgb" if self._lgb_model else "xgb"
            logger.info("Model loaded from %s (ensemble=%s)", path, ensemble)
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
