"""Tests for ML trade selection model."""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd
import pytest

from simple_bot.services.ml_model import MLTradeModel
from simple_bot.core.models import MarketState, Regime, Direction


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def mock_dataset() -> pd.DataFrame:
    """Create a synthetic dataset for testing."""
    np.random.seed(42)
    n = 200
    data = {
        "adx": np.random.uniform(15, 60, n),
        "rsi": np.random.uniform(20, 80, n),
        "atr_pct": np.random.uniform(0.05, 1.0, n),
        "ema_spread_pct": np.random.uniform(0, 2, n),
        "volume_ratio": np.random.uniform(0.3, 3.0, n),
        "bb_position": np.random.uniform(0, 1, n),
        "hour_utc": np.random.randint(0, 24, n),
        "day_of_week": np.random.randint(0, 7, n),
        "direction_encoded": np.random.randint(0, 2, n),
        "ema9_slope": np.random.uniform(-0.01, 0.01, n),
        "ema21_slope": np.random.uniform(-0.01, 0.01, n),
        "close_vs_ema200": np.random.uniform(-5, 5, n),
        "label": np.random.randint(0, 2, n),
    }
    return pd.DataFrame(data)


@pytest.fixture
def trained_model(mock_dataset: pd.DataFrame) -> MLTradeModel:
    """Create a trained model for testing."""
    model = MLTradeModel()
    model.train(mock_dataset)
    return model


@pytest.fixture
def sample_market_state() -> MarketState:
    """Create a sample MarketState for feature extraction tests."""
    return MarketState(
        symbol="BTC",
        timeframe="15m",
        timestamp=datetime(2026, 2, 24, 14, 30, tzinfo=timezone.utc),
        open=Decimal("50000"),
        high=Decimal("50500"),
        low=Decimal("49800"),
        close=Decimal("50200"),
        volume=Decimal("100"),
        atr=Decimal("200"),
        atr_pct=Decimal("0.4"),
        adx=Decimal("35"),
        rsi=Decimal("55"),
        ema50=Decimal("49500"),
        ema200=Decimal("48000"),
        ema200_slope=Decimal("0.001"),
        sma20=Decimal("49800"),
        sma50=Decimal("49500"),
        ema9=Decimal("50100"),
        ema21=Decimal("49900"),
        prev_open=Decimal("49900"),
        prev_high=Decimal("50300"),
        prev_low=Decimal("49700"),
        prev_close=Decimal("50000"),
        volume_usd=Decimal("5000000"),
        volume_sma20=Decimal("4500000"),
        volume_ratio=Decimal("1.5"),
        choppiness=Decimal("45"),
        bb_upper=Decimal("51000"),
        bb_lower=Decimal("49000"),
        bb_mid=Decimal("50000"),
        regime=Regime.TREND,
        trend_direction=Direction.LONG,
    )


# ── Training Tests ────────────────────────────────────────────────────────

class TestMLTraining:
    def test_train_returns_metrics(self, mock_dataset: pd.DataFrame) -> None:
        model = MLTradeModel()
        metrics = model.train(mock_dataset)

        assert "accuracy" in metrics
        assert "auc" in metrics
        assert "cv_auc_mean" in metrics
        assert "cv_auc_std" in metrics
        assert "feature_importances" in metrics
        assert "n_samples" in metrics
        assert metrics["n_samples"] == len(mock_dataset)

    def test_train_model_is_loaded(self, trained_model: MLTradeModel) -> None:
        assert trained_model.is_loaded

    def test_train_missing_features(self) -> None:
        model = MLTradeModel()
        df = pd.DataFrame({"adx": [1, 2], "label": [0, 1]})
        with pytest.raises(ValueError, match="Missing feature columns"):
            model.train(df)

    def test_train_missing_label(self, mock_dataset: pd.DataFrame) -> None:
        model = MLTradeModel()
        df = mock_dataset.drop(columns=["label"])
        with pytest.raises(ValueError, match="label"):
            model.train(df)

    def test_metrics_range(self, mock_dataset: pd.DataFrame) -> None:
        model = MLTradeModel()
        metrics = model.train(mock_dataset)
        assert 0.0 <= metrics["accuracy"] <= 1.0
        assert 0.0 <= metrics["auc"] <= 1.0
        assert 0.0 <= metrics["cv_auc_mean"] <= 1.0


# ── Prediction Tests ──────────────────────────────────────────────────────

class TestMLPrediction:
    def test_predict_returns_probability_and_explanation(
        self, trained_model: MLTradeModel
    ) -> None:
        features = {
            "adx": 35, "rsi": 55, "atr_pct": 0.4,
            "ema_spread_pct": 0.5, "volume_ratio": 1.2,
            "bb_position": 0.6, "hour_utc": 14, "day_of_week": 1,
            "direction_encoded": 1, "ema9_slope": 0.002,
            "ema21_slope": 0.001, "close_vs_ema200": 2.0,
        }
        prob, explanation = trained_model.predict(features)

        assert 0.0 <= prob <= 1.0
        assert isinstance(explanation, str)
        assert "top:" in explanation

    def test_predict_without_model_raises(self) -> None:
        model = MLTradeModel()
        with pytest.raises(RuntimeError, match="not loaded"):
            model.predict({"adx": 35})


# ── Feature Extraction Tests ─────────────────────────────────────────────

class TestFeatureExtraction:
    def test_extract_features_all_keys(
        self, trained_model: MLTradeModel, sample_market_state: MarketState
    ) -> None:
        features = trained_model.extract_features(sample_market_state, 1)
        for feat in MLTradeModel.FEATURES:
            assert feat in features, f"Missing feature: {feat}"

    def test_extract_features_values_reasonable(
        self, trained_model: MLTradeModel, sample_market_state: MarketState
    ) -> None:
        features = trained_model.extract_features(sample_market_state, 1)
        assert features["adx"] == 35.0
        assert features["rsi"] == 55.0
        assert features["direction_encoded"] == 1
        assert features["hour_utc"] == 14
        assert features["day_of_week"] == 1  # Tuesday (2026-02-24)

    def test_extract_features_no_bollinger(
        self, trained_model: MLTradeModel, sample_market_state: MarketState
    ) -> None:
        sample_market_state.bb_upper = None
        sample_market_state.bb_lower = None
        features = trained_model.extract_features(sample_market_state, 0)
        assert features["bb_position"] == 0.5

    def test_extract_features_no_ema9(
        self, trained_model: MLTradeModel, sample_market_state: MarketState
    ) -> None:
        sample_market_state.ema9 = None
        sample_market_state.ema21 = None
        features = trained_model.extract_features(sample_market_state, 1)
        assert features["ema_spread_pct"] == 0.0


# ── Persistence Tests ─────────────────────────────────────────────────────

class TestMLPersistence:
    def test_save_load_roundtrip(
        self, trained_model: MLTradeModel
    ) -> None:
        features = {
            "adx": 35, "rsi": 55, "atr_pct": 0.4,
            "ema_spread_pct": 0.5, "volume_ratio": 1.2,
            "bb_position": 0.6, "hour_utc": 14, "day_of_week": 1,
            "direction_encoded": 1, "ema9_slope": 0.002,
            "ema21_slope": 0.001, "close_vs_ema200": 2.0,
        }
        prob_before, _ = trained_model.predict(features)

        with tempfile.NamedTemporaryFile(suffix=".joblib", delete=False) as f:
            path = f.name

        trained_model.save(path)

        loaded = MLTradeModel()
        assert loaded.load(path)
        assert loaded.is_loaded

        prob_after, _ = loaded.predict(features)
        assert abs(prob_before - prob_after) < 1e-6

    def test_load_nonexistent(self) -> None:
        model = MLTradeModel()
        assert not model.load("/tmp/nonexistent_model.joblib")
        assert not model.is_loaded


# ── Dataset Generator Tests ───────────────────────────────────────────────

class TestDatasetGenerator:
    @patch("simple_bot.services.ml_dataset.get_candles")
    def test_generate_dataset_basic(self, mock_get_candles: MagicMock) -> None:
        from simple_bot.services.ml_dataset import generate_dataset
        from backtesting.config import BacktestConfig

        # Create synthetic candle data (300 bars of 15m)
        np.random.seed(42)
        n_bars = 300
        base_price = 50000.0
        candles = []
        for i in range(n_bars):
            c = base_price + np.random.randn() * 100
            candles.append({
                "t": int(1708000000000 + i * 900000),  # 15m intervals
                "o": c - np.random.uniform(0, 50),
                "h": c + np.random.uniform(0, 100),
                "l": c - np.random.uniform(0, 100),
                "c": c,
                "v": np.random.uniform(50, 200),
            })

        mock_get_candles.return_value = candles

        cfg = BacktestConfig(warmup_bars=200)
        df = generate_dataset(["BTC"], days=7, cfg=cfg)

        # May or may not have signals depending on random data
        assert isinstance(df, pd.DataFrame)
        if not df.empty:
            for feat in MLTradeModel.FEATURES:
                assert feat in df.columns
            assert "label" in df.columns
            assert "symbol" in df.columns
            assert set(df["label"].unique()).issubset({0, 1})
