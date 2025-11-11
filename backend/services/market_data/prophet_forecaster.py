"""
Prophet Price Forecasting (RIZZO VIDEO - Priority 6)

Implements Meta/Facebook Prophet for short-term price forecasting.
Used as **bias primario** (primary bias) for AI trading decisions.

Features:
- 90-day historical data training
- 6-24 hour forecast horizon
- Confidence intervals for risk management
- Daily model retraining for adaptation
- Graceful degradation on errors

Configuration optimized for crypto volatility:
- Weekly + daily seasonality
- Changepoint detection enabled
- Short-term focus (6-24h)
"""

import logging
import time
from datetime import datetime, timedelta
from threading import Lock
from typing import Any, Dict, Optional

import pandas as pd
from prophet import Prophet

logger = logging.getLogger(__name__)


class ProphetForecaster:
    """
    Price forecaster using Meta/Facebook Prophet.

    Optimized for crypto trading with:
    - Short-term horizons (6-24h)
    - Frequent model retraining
    - Confidence-based filtering
    """

    def __init__(
        self,
        training_days: int = 90,
        forecast_hours: int = 24,
        cache_ttl_hours: int = 24,
    ):
        """
        Initialize Prophet forecaster.

        Args:
            training_days: Days of historical data for training (default: 90)
            forecast_hours: Hours ahead to forecast (default: 24)
            cache_ttl_hours: Hours to cache models (default: 24 = daily retrain)
        """
        self.training_days = training_days
        self.forecast_hours = forecast_hours
        self.cache_ttl_hours = cache_ttl_hours

        # Cache: {symbol: {model, forecast, timestamp}}
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = Lock()

        # Metrics
        self._forecast_count = 0
        self._cache_hits = 0
        self._cache_misses = 0
        self._errors = 0

        logger.info(
            f"ProphetForecaster initialized: {training_days}d training, "
            f"{forecast_hours}h forecast, {cache_ttl_hours}h cache TTL"
        )

    def forecast_price(
        self,
        symbol: str,
        historical_data: pd.DataFrame,
    ) -> Dict[str, Any]:
        """
        Generate price forecast for symbol.

        Args:
            symbol: Trading symbol (e.g., 'BTC', 'ETH')
            historical_data: DataFrame with columns ['ds', 'y']
                            - ds: datetime (UTC)
                            - y: price (float)

        Returns:
            Dictionary with forecast data:
            {
                "symbol": str,
                "current_price": float,
                "forecast_6h": float,
                "forecast_24h": float,
                "trend": str ("up", "down", "neutral"),
                "confidence": float (0.0-1.0),
                "confidence_interval_6h": (lower, upper),
                "confidence_interval_24h": (lower, upper),
                "timestamp": datetime,
            }

        Example:
            >>> df = pd.DataFrame({
            ...     'ds': pd.date_range('2024-01-01', periods=90, freq='1h'),
            ...     'y': [100.0 + i * 0.5 for i in range(90 * 24)]
            ... })
            >>> forecast = forecaster.forecast_price('BTC', df)
            >>> print(f"BTC 24h forecast: ${forecast['forecast_24h']:.2f}")
        """
        with self._lock:
            # Check cache first
            if self._is_forecast_valid(symbol):
                self._cache_hits += 1
                cached = self._cache[symbol]
                logger.debug(
                    f"Prophet cache HIT for {symbol} "
                    f"(age: {time.time() - cached['timestamp']:.0f}s, "
                    f"hits: {self._cache_hits}, misses: {self._cache_misses})"
                )
                return cached["forecast"]

            # Cache miss - generate new forecast
            self._cache_misses += 1
            logger.info(
                f"Prophet cache MISS for {symbol} "
                f"(hits: {self._cache_hits}, misses: {self._cache_misses})"
            )

            try:
                # Validate input data
                if historical_data.empty or len(historical_data) < 14:
                    raise ValueError(
                        f"Insufficient data for {symbol}: "
                        f"{len(historical_data)} rows (need >= 14 days)"
                    )

                # Prepare Prophet dataframe
                df = historical_data[["ds", "y"]].copy()
                df = df.dropna()

                # Prophet requires timezone-naive datetime
                if df["ds"].dt.tz is not None:
                    df["ds"] = df["ds"].dt.tz_localize(None)

                # Configure Prophet for crypto
                model = Prophet(
                    # Seasonality (crypto has 24/7 trading)
                    daily_seasonality=True,
                    weekly_seasonality=True,
                    yearly_seasonality=False,  # Too long for crypto
                    # Changepoints (adapt to volatility)
                    changepoint_prior_scale=0.05,  # Default, balanced
                    # Confidence intervals
                    interval_width=0.80,  # 80% confidence (±1.28σ)
                    # Performance
                    uncertainty_samples=1000,
                )

                # Suppress Prophet verbose logging
                import logging as prophet_logging

                prophet_logging.getLogger("prophet").setLevel(prophet_logging.WARNING)

                # Train model
                logger.info(f"Training Prophet model for {symbol} ({len(df)} data points)...")
                model.fit(df)

                # Generate forecast
                future = model.make_future_dataframe(
                    periods=self.forecast_hours, freq="h"
                )
                forecast_df = model.predict(future)

                # Extract current and forecast prices
                current_price = float(df["y"].iloc[-1])
                forecast_6h = forecast_df.iloc[-18]["yhat"]  # -24 + 6 = -18
                forecast_24h = forecast_df.iloc[-1]["yhat"]

                # Confidence intervals
                ci_6h_lower = forecast_df.iloc[-18]["yhat_lower"]
                ci_6h_upper = forecast_df.iloc[-18]["yhat_upper"]
                ci_24h_lower = forecast_df.iloc[-1]["yhat_lower"]
                ci_24h_upper = forecast_df.iloc[-1]["yhat_upper"]

                # Calculate trend and confidence
                trend = "up" if forecast_24h > current_price else "down"
                if abs(forecast_24h - current_price) / current_price < 0.01:  # <1% change
                    trend = "neutral"

                # Confidence = 1 - (interval_width / price)
                # Narrower intervals = higher confidence
                interval_width_24h = ci_24h_upper - ci_24h_lower
                confidence = max(
                    0.0, min(1.0, 1.0 - (interval_width_24h / forecast_24h / 2))
                )

                # Build result
                result = {
                    "symbol": symbol,
                    "current_price": current_price,
                    "forecast_6h": float(forecast_6h),
                    "forecast_24h": float(forecast_24h),
                    "trend": trend,
                    "confidence": round(confidence, 3),
                    "confidence_interval_6h": (float(ci_6h_lower), float(ci_6h_upper)),
                    "confidence_interval_24h": (float(ci_24h_lower), float(ci_24h_upper)),
                    "timestamp": datetime.utcnow(),
                }

                # Cache result
                self._cache[symbol] = {
                    "model": model,
                    "forecast": result,
                    "timestamp": time.time(),
                }

                self._forecast_count += 1
                logger.info(
                    f"Prophet forecast for {symbol}: "
                    f"${current_price:.2f} → ${forecast_24h:.2f} (24h, {trend}, "
                    f"confidence: {confidence:.1%})"
                )

                return result

            except Exception as err:
                self._errors += 1
                logger.error(
                    f"Failed to generate Prophet forecast for {symbol}: {err}",
                    exc_info=True,
                )

                # Return neutral fallback
                return {
                    "symbol": symbol,
                    "current_price": None,
                    "forecast_6h": None,
                    "forecast_24h": None,
                    "trend": "neutral",
                    "confidence": 0.0,
                    "confidence_interval_6h": (None, None),
                    "confidence_interval_24h": (None, None),
                    "timestamp": datetime.utcnow(),
                    "error": str(err),
                }

    def _is_forecast_valid(self, symbol: str) -> bool:
        """
        Check if cached forecast is still valid.

        Returns:
            True if cache is valid (not expired), False otherwise
        """
        if symbol not in self._cache:
            return False

        cache_age_seconds = time.time() - self._cache[symbol]["timestamp"]
        cache_age_hours = cache_age_seconds / 3600

        return cache_age_hours < self.cache_ttl_hours

    def invalidate(self, symbol: Optional[str] = None) -> None:
        """
        Manually invalidate cached forecast(s).

        Args:
            symbol: Symbol to invalidate, or None to clear all
        """
        with self._lock:
            if symbol:
                if symbol in self._cache:
                    del self._cache[symbol]
                    logger.info(f"Prophet forecast cache invalidated for {symbol}")
            else:
                count = len(self._cache)
                self._cache.clear()
                logger.info(f"Prophet forecast cache cleared ({count} symbols)")

    def get_cache_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics for monitoring.

        Returns:
            Dictionary with metrics
        """
        total_requests = self._cache_hits + self._cache_misses
        hit_rate = (
            (self._cache_hits / total_requests * 100) if total_requests > 0 else 0.0
        )

        return {
            "forecasts_generated": self._forecast_count,
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "hit_rate": round(hit_rate, 2),
            "errors": self._errors,
            "cached_symbols": list(self._cache.keys()),
            "cache_ttl_hours": self.cache_ttl_hours,
        }


async def calculate_prophet_forecasts_batch(
    symbols: list[str],
    mode: str = "lite",
    cache_manager=None,
) -> Dict[str, dict]:
    """
    Calculate Prophet forecasts for multiple symbols in batch (optimized for 142 symbols).

    This is the NEW API for the orchestrator system with LITE mode for efficiency.

    Args:
        symbols: List of symbols to forecast
        mode: "lite" (7 days, fast) or "full" (90 days, accurate)
        cache_manager: Optional CacheManager instance (uses global if None)

    Returns:
        Dictionary mapping symbol to forecast data:
        {
            "BTC": {
                "current_price": 102450.0,
                "forecast_6h": 102800.0,
                "forecast_24h": 103120.0,
                "change_pct_6h": 0.34,
                "change_pct_24h": 0.65,
                "trend": "up",
                "confidence": 0.885,
                "confidence_interval_24h": [101500.0, 104700.0]
            },
            ...
        }

    Example:
        >>> forecasts = await calculate_prophet_forecasts_batch(["BTC", "ETH"], mode="lite")
        >>> btc_forecast = forecasts["BTC"]["forecast_24h"]

    Note:
        - LITE mode: 7 days training, 994 API calls for 142 symbols
        - FULL mode: 90 days training, 12,780 API calls for 142 symbols
        - Results cached for 24 hours
    """
    import asyncio
    from hyperliquid.info import Info

    logger.info(f"[BATCH] Calculating Prophet forecasts for {len(symbols)} symbols (mode={mode})")

    # Use global cache manager if not provided
    if cache_manager is None:
        from services.orchestrator.cache_manager import get_cache_manager
        cache_manager = get_cache_manager()

    # Configure training days based on mode
    training_days = 7 if mode == "lite" else 90
    logger.info(f"[BATCH] Training mode: {mode} ({training_days} days)")

    # Check cache first
    cached = cache_manager.get_batch("prophet_forecasts", symbols)
    logger.info(f"[BATCH] Cache hit: {len(cached)}/{len(symbols)} symbols")

    # Find missing symbols
    missing_symbols = [s for s in symbols if s not in cached]

    # Calculate missing forecasts
    if missing_symbols:
        logger.info(f"[BATCH] Calculating {len(missing_symbols)} missing forecasts")

        # Get forecaster (singleton)
        forecaster = get_prophet_forecaster(
            training_days=training_days,
            forecast_hours=24,
            cache_ttl_hours=24
        )

        # Fetch klines for all missing symbols in parallel
        info = Info()

        async def fetch_and_forecast(symbol: str) -> tuple[str, Optional[dict]]:
            """Fetch klines and calculate forecast for one symbol."""
            try:
                # Calculate time range
                end_time_ms = int(datetime.utcnow().timestamp() * 1000)
                hours_back = training_days * 24
                start_time_ms = end_time_ms - (hours_back * 3600 * 1000)

                # Fetch klines (1h candles)
                candles = info.candles_snapshot(
                    name=symbol,
                    interval="1h",
                    startTime=start_time_ms,
                    endTime=end_time_ms
                )

                if not candles or len(candles) < 14:
                    logger.warning(f"[BATCH] Insufficient data for {symbol}: {len(candles) if candles else 0} candles")
                    return symbol, None

                # Prepare Prophet dataframe
                df = pd.DataFrame(candles)
                df["ds"] = pd.to_datetime(df["t"], unit="ms", utc=True)
                df["y"] = df["c"].astype(float)  # Close price
                df = df[["ds", "y"]].copy()

                # Generate forecast
                forecast = forecaster.forecast_price(symbol, df)

                # Check for errors
                if "error" in forecast or forecast["current_price"] is None:
                    logger.warning(f"[BATCH] Forecast error for {symbol}")
                    return symbol, None

                # Convert to structured format for JSON builder
                structured = {
                    "current_price": forecast["current_price"],
                    "forecast_6h": forecast["forecast_6h"],
                    "forecast_24h": forecast["forecast_24h"],
                    "change_pct_6h": round(
                        (forecast["forecast_6h"] - forecast["current_price"]) / forecast["current_price"] * 100,
                        2
                    ),
                    "change_pct_24h": round(
                        (forecast["forecast_24h"] - forecast["current_price"]) / forecast["current_price"] * 100,
                        2
                    ),
                    "trend": forecast["trend"],
                    "confidence": forecast["confidence"],
                    "confidence_interval_24h": list(forecast["confidence_interval_24h"]),
                }

                return symbol, structured

            except Exception as e:
                logger.error(f"[BATCH] Failed to forecast {symbol}: {e}", exc_info=True)
                return symbol, None

        # Run all forecasts in parallel
        tasks = [fetch_and_forecast(symbol) for symbol in missing_symbols]
        results = await asyncio.gather(*tasks)

        # Process results and cache
        for symbol, forecast_data in results:
            if forecast_data:
                # Cache for 24 hours
                cache_manager.set("prophet_forecasts", symbol, forecast_data, ttl=86400)
                cached[symbol] = forecast_data

    logger.info(
        f"[BATCH] Completed: {len(cached)}/{len(symbols)} symbols with Prophet forecasts"
    )

    # Log sample forecasts
    if cached:
        sample = list(cached.items())[:3]
        for symbol, data in sample:
            logger.info(
                f"  {symbol}: ${data['current_price']:.2f} → ${data['forecast_24h']:.2f} "
                f"({data['change_pct_24h']:+.2f}%, confidence={data['confidence']:.1%})"
            )

    return cached


# Global forecaster instance (singleton pattern)
_global_forecaster: Optional[ProphetForecaster] = None


def get_prophet_forecaster(
    training_days: int = 90,
    forecast_hours: int = 24,
    cache_ttl_hours: int = 24,
) -> ProphetForecaster:
    """
    Get the global Prophet forecaster instance (singleton).

    Args:
        training_days: Days of historical data (default: 90)
        forecast_hours: Forecast horizon in hours (default: 24)
        cache_ttl_hours: Cache TTL in hours (default: 24 = daily retrain)

    Returns:
        Global ProphetForecaster instance

    Example:
        >>> forecaster = get_prophet_forecaster()
        >>> forecast = forecaster.forecast_price('BTC', historical_df)
    """
    global _global_forecaster

    if _global_forecaster is None:
        _global_forecaster = ProphetForecaster(
            training_days=training_days,
            forecast_hours=forecast_hours,
            cache_ttl_hours=cache_ttl_hours,
        )
        logger.info(
            f"Global Prophet forecaster created: {training_days}d training, "
            f"{forecast_hours}h forecast"
        )

    return _global_forecaster
