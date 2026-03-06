"""
HLQuantBot Market State Service
================================

Focused market data service for conservative trading strategy.
Only fetches data for configured assets (BTC, ETH) instead of scanning all coins.

Features:
- Fetches OHLCV data for 2-3 configured assets only
- Calculates technical indicators: ATR, EMA, ADX, RSI, Bollinger Bands
- Detects market regime: TREND / RANGE / CHAOS
- Publishes MarketState to Topic.MARKET_STATE every 4h (configurable)
- Caches data to minimize API calls

Author: Francesco Carlesi
"""

import asyncio
import logging
import os
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Deque, Dict, List, Optional

import numpy as np
from hyperliquid.info import Info
from hyperliquid.utils import constants

from .base import BaseService, HealthStatus
from .message_bus import MessageBus
from ..core.enums import Topic
from ..core.models import MarketState, Regime, Direction


logger = logging.getLogger(__name__)


# =============================================================================
# Technical Indicator Calculations
# =============================================================================

def calculate_ema(prices: np.ndarray, period: int) -> np.ndarray:
    """Calculate Exponential Moving Average."""
    alpha = 2.0 / (period + 1)
    ema = np.zeros_like(prices)
    ema[0] = prices[0]
    for i in range(1, len(prices)):
        ema[i] = alpha * prices[i] + (1 - alpha) * ema[i - 1]
    return ema


def calculate_sma(prices: np.ndarray, period: int) -> np.ndarray:
    """Calculate Simple Moving Average."""
    return np.convolve(prices, np.ones(period) / period, mode='valid')


def calculate_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """Calculate Average True Range."""
    tr = np.maximum(
        high[1:] - low[1:],
        np.maximum(
            np.abs(high[1:] - close[:-1]),
            np.abs(low[1:] - close[:-1])
        )
    )
    atr = np.zeros(len(tr))
    atr[0] = np.mean(tr[:period]) if len(tr) >= period else tr[0]

    alpha = 1.0 / period
    for i in range(1, len(tr)):
        atr[i] = alpha * tr[i] + (1 - alpha) * atr[i - 1]

    return atr


def calculate_rsi(prices: np.ndarray, period: int = 14) -> np.ndarray:
    """Calculate Relative Strength Index."""
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)

    avg_gain = np.zeros(len(deltas))
    avg_loss = np.zeros(len(deltas))

    # Initial SMA
    avg_gain[period - 1] = np.mean(gains[:period])
    avg_loss[period - 1] = np.mean(losses[:period])

    # Smoothed averages
    for i in range(period, len(deltas)):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gains[i]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + losses[i]) / period

    # Use np.divide with where parameter to avoid RuntimeWarning for division by zero
    rs = np.divide(avg_gain, avg_loss, out=np.full_like(avg_gain, 100.0, dtype=float), where=avg_loss != 0)
    rsi = 100 - (100 / (1 + rs))

    return rsi


def calculate_adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """Calculate Average Directional Index (ADX)."""
    # Calculate +DM and -DM
    up_move = high[1:] - high[:-1]
    down_move = low[:-1] - low[1:]

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)

    # Calculate ATR for DI normalization
    atr = calculate_atr(high, low, close, period)

    # Smooth DM values
    plus_dm_smooth = np.zeros(len(plus_dm))
    minus_dm_smooth = np.zeros(len(minus_dm))

    plus_dm_smooth[period - 1] = np.sum(plus_dm[:period])
    minus_dm_smooth[period - 1] = np.sum(minus_dm[:period])

    for i in range(period, len(plus_dm)):
        plus_dm_smooth[i] = plus_dm_smooth[i - 1] - (plus_dm_smooth[i - 1] / period) + plus_dm[i]
        minus_dm_smooth[i] = minus_dm_smooth[i - 1] - (minus_dm_smooth[i - 1] / period) + minus_dm[i]

    # Calculate DI+ and DI- - use np.divide with where parameter to avoid RuntimeWarning
    atr_scaled = atr * period
    plus_di = np.divide(100 * plus_dm_smooth, atr_scaled, out=np.zeros_like(atr_scaled, dtype=float), where=atr_scaled != 0)
    minus_di = np.divide(100 * minus_dm_smooth, atr_scaled, out=np.zeros_like(atr_scaled, dtype=float), where=atr_scaled != 0)

    # Calculate DX - use np.divide with where parameter to avoid RuntimeWarning for division by zero
    di_sum = plus_di + minus_di
    di_diff = 100 * np.abs(plus_di - minus_di)
    dx = np.divide(di_diff, di_sum, out=np.zeros_like(di_sum, dtype=float), where=di_sum != 0)

    # Smooth DX to get ADX
    adx = np.zeros(len(dx))
    start_idx = 2 * period - 1
    if start_idx < len(dx):
        adx[start_idx] = np.mean(dx[period:start_idx + 1])
        for i in range(start_idx + 1, len(dx)):
            adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period

    return adx


def calculate_bollinger_bands(prices: np.ndarray, period: int = 20, std_mult: float = 2.0):
    """Calculate Bollinger Bands."""
    sma = calculate_ema(prices, period)  # Using EMA for faster response

    # Rolling standard deviation
    std = np.zeros(len(prices))
    for i in range(period - 1, len(prices)):
        std[i] = np.std(prices[i - period + 1:i + 1])

    upper = sma + std_mult * std
    lower = sma - std_mult * std

    return lower, sma, upper


def calculate_choppiness_index(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """Calculate Choppiness Index (0-100, higher = more choppy/range-bound)."""
    atr = calculate_atr(high, low, close, period)

    ci = np.zeros(len(close))
    for i in range(period, len(close)):
        atr_sum = np.sum(atr[i - period + 1:i + 1])
        high_low_range = np.max(high[i - period + 1:i + 1]) - np.min(low[i - period + 1:i + 1])

        if high_low_range > 0 and atr_sum > 0:
            ci[i] = 100 * np.log10(atr_sum / high_low_range) / np.log10(period)

    return ci


def detect_engulfing_pattern(
    curr_open: float,
    curr_close: float,
    prev_open: float,
    prev_close: float,
) -> tuple[bool, bool]:
    """
    Detect bullish and bearish engulfing candlestick patterns.

    Engulfing Pattern Rules:
    ========================

    BULLISH ENGULFING (reversal signal for LONG entry):
    - Previous candle is bearish (prev_close < prev_open)
    - Current candle is bullish (curr_close > curr_open)
    - Current body engulfs previous body:
      - Current open <= previous close (opens at or below previous close)
      - Current close >= previous open (closes at or above previous open)

    BEARISH ENGULFING (reversal signal for SHORT entry):
    - Previous candle is bullish (prev_close > prev_open)
    - Current candle is bearish (curr_close < curr_open)
    - Current body engulfs previous body:
      - Current open >= previous close (opens at or above previous close)
      - Current close <= previous open (closes at or below previous open)

    Args:
        curr_open: Current candle open price
        curr_close: Current candle close price
        prev_open: Previous candle open price
        prev_close: Previous candle close price

    Returns:
        Tuple of (bullish_engulfing, bearish_engulfing) booleans
    """
    # Previous candle direction
    prev_is_bearish = prev_close < prev_open
    prev_is_bullish = prev_close > prev_open

    # Current candle direction
    curr_is_bullish = curr_close > curr_open
    curr_is_bearish = curr_close < curr_open

    # Previous candle body boundaries
    prev_body_high = max(prev_open, prev_close)
    prev_body_low = min(prev_open, prev_close)

    # Current candle body boundaries
    curr_body_high = max(curr_open, curr_close)
    curr_body_low = min(curr_open, curr_close)

    # Bullish Engulfing: bearish previous + bullish current + current engulfs previous
    bullish_engulfing = (
        prev_is_bearish and
        curr_is_bullish and
        curr_body_low <= prev_body_low and
        curr_body_high >= prev_body_high
    )

    # Bearish Engulfing: bullish previous + bearish current + current engulfs previous
    bearish_engulfing = (
        prev_is_bullish and
        curr_is_bearish and
        curr_body_low <= prev_body_low and
        curr_body_high >= prev_body_high
    )

    return bullish_engulfing, bearish_engulfing


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class MarketStateConfig:
    """Configuration for MarketStateService."""

    # Assets to track
    assets: List[str] = None

    # Timeframe
    timeframe: str = "4h"
    bars_to_fetch: int = 200

    # Update interval
    interval_seconds: int = 14400  # 4 hours

    # Regime detection thresholds (level hysteresis)
    trend_adx_entry_min: float = 28.0   # Stricter threshold for entering TREND
    trend_adx_exit_min: float = 22.0    # Lenient threshold for staying in TREND
    range_adx_max: float = 20.0
    choppiness_range_min: float = 60.0
    regime_confirmation_bars: int = 3

    def __post_init__(self):
        if self.assets is None:
            self.assets = ["BTC", "ETH"]


# =============================================================================
# Market State Service
# =============================================================================

class MarketStateService(BaseService):
    """
    Service that tracks market state for configured assets.

    Unlike MarketScannerService, this focuses on few assets with deep analysis:
    - Full OHLCV history (200 bars)
    - Complete indicator suite
    - Regime detection
    - Published every 4h
    """

    def __init__(
        self,
        name: str = "market_state",
        bus: Optional[MessageBus] = None,
        config: Optional[MarketStateConfig] = None,
        testnet: bool = True,
    ) -> None:
        """Initialize MarketStateService."""
        self._state_config = config or MarketStateConfig()

        super().__init__(
            name=name,
            bus=bus,
            loop_interval_seconds=self._state_config.interval_seconds,
        )

        self._testnet = testnet
        self._info: Optional[Info] = None

        # Cache for OHLCV data
        self._ohlcv_cache: Dict[str, Dict[str, np.ndarray]] = {}
        self._last_fetch: Dict[str, datetime] = {}

        # Current market states
        self._market_states: Dict[str, MarketState] = {}

        # Regime hysteresis state (per symbol)
        self._confirmed_regime: Dict[str, Regime] = {}
        self._regime_change_counter: Dict[str, int] = {}
        self._pending_regime: Dict[str, Regime] = {}

        # EMA slope history buffers (keep last 5 values to compute 4-bar lookback)
        self._ema9_history: Dict[str, Deque[float]] = {}
        self._ema21_history: Dict[str, Deque[float]] = {}

        # RSI history buffer (keep last 3 values to compute 2-bar lookback)
        self._rsi_history: Dict[str, Deque[float]] = {}

        self._logger.info(
            "MarketStateService initialized: assets=%s, timeframe=%s, interval=%ds",
            self._state_config.assets,
            self._state_config.timeframe,
            self._state_config.interval_seconds,
        )

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def _on_start(self) -> None:
        """Initialize Hyperliquid client."""
        self._logger.info("Starting MarketStateService...")

        # Determine API URL
        testnet = self._testnet
        if os.getenv("ENVIRONMENT", "").lower() == "mainnet":
            testnet = False
        elif os.getenv("ENVIRONMENT", "").lower() == "testnet":
            testnet = True

        base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
        self._logger.info(
            "Connecting to Hyperliquid %s: %s",
            "TESTNET" if testnet else "MAINNET",
            base_url,
        )

        self._info = Info(base_url, skip_ws=True)

        # Verify connection
        try:
            meta = self._info.meta()
            self._logger.info("Connected to Hyperliquid. Found %d markets.", len(meta.get("universe", [])))
        except Exception as e:
            self._logger.error("Failed to connect: %s", e)
            raise

        # Initial fetch
        await self._fetch_all_states()

    async def _on_stop(self) -> None:
        """Cleanup."""
        self._logger.info("Stopping MarketStateService...")
        self._info = None
        self._ohlcv_cache.clear()
        self._ema9_history.clear()
        self._ema21_history.clear()
        self._rsi_history.clear()

    async def _run_iteration(self) -> None:
        """Fetch and publish market states."""
        await self._fetch_all_states()

    async def _health_check_impl(self) -> bool:
        """Check service health."""
        if self._info is None:
            return False

        # With dynamic asset loading, some assets may not have candle data
        # Consider healthy if we have states for at least 80% of assets
        total_assets = len(self._state_config.assets)
        if total_assets == 0:
            return False

        states_count = len(self._market_states)
        coverage = states_count / total_assets

        # Require at least 80% coverage for healthy status
        return coverage >= 0.8

    # =========================================================================
    # Data Fetching
    # =========================================================================

    async def _fetch_all_states(self) -> None:
        """Fetch market state for all configured assets."""
        # Fetch funding rates and open interest once per scan cycle
        asset_ctx_data = await self._fetch_asset_ctx_data()

        for symbol in self._state_config.assets:
            try:
                ctx = asset_ctx_data.get(symbol, {})
                state = await self._fetch_market_state(symbol, ctx)
                if state:
                    self._market_states[symbol] = state
                    await self._publish_state(state)
                # Rate limiting: 700ms delay between API calls to stay under
                # CloudFront's ~120 req/min limit (~85 req/min with this delay).
                # With 229 symbols, a full scan takes ~160s (within the 300s scan interval).
                await asyncio.sleep(0.7)
            except Exception as e:
                self._logger.error("Failed to fetch state for %s: %s", symbol, e)

    async def _fetch_asset_ctx_data(self) -> Dict[str, Dict[str, float]]:
        """Fetch funding rates and open interest for all assets in one API call.

        Returns dict mapping symbol to {funding, openInterest}.
        Data is logged for future ML training but NOT used in ML features yet.
        """
        if self._info is None:
            return {}

        try:
            loop = asyncio.get_running_loop()
            ctx = await loop.run_in_executor(
                None, lambda: self._info.meta_and_asset_ctxs()
            )

            result: Dict[str, Dict[str, float]] = {}
            meta = ctx[0] if len(ctx) > 0 else {}
            assets = ctx[1] if len(ctx) > 1 else []
            universe = meta.get("universe", [])

            for i, asset_ctx in enumerate(assets):
                if i < len(universe):
                    sym = universe[i].get("name", "")
                    result[sym] = {
                        "funding": float(asset_ctx.get("funding", 0)),
                        "openInterest": float(asset_ctx.get("openInterest", 0)),
                        "dayNtlVlm": float(asset_ctx.get("dayNtlVlm", 0)),
                    }

            return result

        except Exception as e:
            self._logger.warning("Failed to fetch asset context data: %s", e)
            return {}

    async def _fetch_market_state(self, symbol: str, asset_ctx: Optional[Dict[str, float]] = None) -> Optional[MarketState]:
        """Fetch and calculate market state for a single symbol."""
        if self._info is None:
            return None

        try:
            # Fetch OHLCV data
            ohlcv = await self._fetch_ohlcv(symbol)
            if ohlcv is None or len(ohlcv["close"]) < 50:
                self._logger.warning("Insufficient data for %s", symbol)
                return None

            # Extract arrays
            close = ohlcv["close"]
            high = ohlcv["high"]
            low = ohlcv["low"]
            volume = ohlcv["volume"]

            # Calculate indicators
            current_price = Decimal(str(close[-1]))

            # ATR
            atr_values = calculate_atr(high, low, close, 14)
            atr = Decimal(str(atr_values[-1]))
            atr_pct = atr / current_price * 100

            # ATR percentile rank (percentile of current ATR% in last 100 bars)
            atr_pct_array = atr_values / close[1:] * 100  # ATR as % of price for each bar
            lookback = min(100, len(atr_pct_array))
            window = atr_pct_array[-lookback:]
            atr_percentile_val = float(np.searchsorted(np.sort(window), atr_pct_array[-1])) / len(window)
            atr_percentile = Decimal(str(round(atr_percentile_val, 4)))

            # EMAs
            ema50 = calculate_ema(close, 50)
            ema200 = calculate_ema(close, 200)

            # EMA200 slope (normalized)
            ema200_slope = (ema200[-1] - ema200[-5]) / ema200[-5] if len(ema200) >= 5 else 0

            # SMAs for crossover strategy
            sma20 = calculate_sma(close, 20)
            sma50_arr = calculate_sma(close, 50)

            # Fast EMAs for momentum scalper
            ema9 = calculate_ema(close, 9)
            ema21 = calculate_ema(close, 21)

            # EMA slopes (4-bar lookback, matching ml_dataset formula)
            ema9_slope, ema21_slope = self._compute_ema_slopes(
                symbol, float(ema9[-1]), float(ema21[-1])
            )

            # ADX
            adx_values = calculate_adx(high, low, close, 14)
            adx = Decimal(str(max(0, min(100, adx_values[-1]))))

            # RSI
            rsi_values = calculate_rsi(close, 14)
            rsi = Decimal(str(max(0, min(100, rsi_values[-1]))))

            # RSI slope (2-bar lookback)
            rsi_slope = self._compute_rsi_slope(symbol, float(rsi))

            # Bollinger Bands
            bb_lower, bb_mid, bb_upper = calculate_bollinger_bands(close, 20, 2.0)

            # Choppiness Index
            chop_values = calculate_choppiness_index(high, low, close, 14)
            choppiness = Decimal(str(max(0, min(100, chop_values[-1]))))

            # Detect regime
            regime = self._detect_regime(
                adx=float(adx),
                ema200_slope=ema200_slope,
                choppiness=float(choppiness),
                symbol=symbol,
            )

            # Trend direction: EMA9 vs EMA21 (matches training signal)
            if ema9[-1] > ema21[-1]:
                trend_direction = Direction.LONG
            elif ema9[-1] < ema21[-1]:
                trend_direction = Direction.SHORT
            else:
                trend_direction = Direction.FLAT

            # Extract previous candle data for pattern detection
            opens = ohlcv["open"]
            prev_open = Decimal(str(opens[-2])) if len(opens) >= 2 else None
            prev_high = Decimal(str(high[-2])) if len(high) >= 2 else None
            prev_low = Decimal(str(low[-2])) if len(low) >= 2 else None
            prev_close = Decimal(str(close[-2])) if len(close) >= 2 else None

            # Detect engulfing patterns for entry confirmation
            bullish_engulfing = False
            bearish_engulfing = False
            if prev_open is not None and prev_close is not None:
                bullish_engulfing, bearish_engulfing = detect_engulfing_pattern(
                    curr_open=float(opens[-1]),
                    curr_close=float(close[-1]),
                    prev_open=float(prev_open),
                    prev_close=float(prev_close),
                )

            # Multi-timeframe indicators (1h-equivalent from 15m bars)
            # EMA(36) ≈ 1h EMA(9), EMA(84) ≈ 1h EMA(21)
            ema9_1h_arr = calculate_ema(close, 36)
            ema21_1h_arr = calculate_ema(close, 84)
            rsi_1h_arr = calculate_rsi(close, 56)
            adx_1h_arr = calculate_adx(high, low, close, 56)

            ema9_1h_val = Decimal(str(ema9_1h_arr[-1]))
            ema21_1h_val = Decimal(str(ema21_1h_arr[-1]))
            rsi_1h_val = Decimal(str(max(0, min(100, rsi_1h_arr[-1]))))
            adx_1h_val = Decimal(str(max(0, min(100, adx_1h_arr[-1]))))

            # Volume metrics
            current_volume = Decimal(str(volume[-1]))
            volume_usd = current_volume * Decimal(str(float(close[-1])))
            vol_sma20: Optional[Decimal] = None
            vol_ratio: Optional[Decimal] = None
            if len(volume) >= 20:
                vol_sma20_val = float(calculate_sma(volume, 20)[-1])
                if vol_sma20_val > 0:
                    vol_sma20 = Decimal(str(vol_sma20_val))
                    vol_ratio = current_volume / vol_sma20

            # Build MarketState
            state = MarketState(
                symbol=symbol,
                timeframe=self._state_config.timeframe,
                timestamp=datetime.now(timezone.utc),
                open=Decimal(str(ohlcv["open"][-1])),
                high=Decimal(str(high[-1])),
                low=Decimal(str(low[-1])),
                close=current_price,
                volume=current_volume,
                volume_usd=volume_usd,
                volume_sma20=vol_sma20,
                volume_ratio=vol_ratio,
                atr=atr,
                atr_pct=atr_pct,
                atr_percentile=atr_percentile,
                adx=adx,
                rsi=rsi,
                ema50=Decimal(str(ema50[-1])),
                ema200=Decimal(str(ema200[-1])),
                ema200_slope=Decimal(str(ema200_slope)),
                sma20=Decimal(str(sma20[-1])),
                sma50=Decimal(str(sma50_arr[-1])),
                ema9=Decimal(str(ema9[-1])),
                ema21=Decimal(str(ema21[-1])),
                ema9_slope=ema9_slope,
                ema21_slope=ema21_slope,
                rsi_slope=rsi_slope,
                prev_open=prev_open,
                prev_high=prev_high,
                prev_low=prev_low,
                prev_close=prev_close,
                bullish_engulfing=bullish_engulfing,
                bearish_engulfing=bearish_engulfing,
                choppiness=choppiness,
                bb_lower=Decimal(str(bb_lower[-1])),
                bb_mid=Decimal(str(bb_mid[-1])),
                bb_upper=Decimal(str(bb_upper[-1])),
                rsi_1h=rsi_1h_val,
                adx_1h=adx_1h_val,
                ema9_1h=ema9_1h_val,
                ema21_1h=ema21_1h_val,
                funding_rate=Decimal(str(asset_ctx.get("funding", 0))) if asset_ctx else None,
                open_interest=Decimal(str(asset_ctx.get("openInterest", 0))) if asset_ctx else None,
                volume_24h=Decimal(str(asset_ctx.get("dayNtlVlm", 0))) if asset_ctx else None,
                regime=regime,
                trend_direction=trend_direction,
                bars_count=len(close),
            )

            # Log funding/OI for future ML training data accumulation
            if asset_ctx:
                self._logger.debug(
                    "%s: funding=%.6f, OI=%.0f",
                    symbol,
                    asset_ctx.get("funding", 0),
                    asset_ctx.get("openInterest", 0),
                )

            self._logger.info(
                "%s: price=%.2f, regime=%s, ADX=%.1f, RSI=%.1f, ATR%%=%.2f",
                symbol, float(current_price), regime.value,
                float(adx), float(rsi), float(atr_pct)
            )

            return state

        except Exception as e:
            self._logger.error("Error calculating state for %s: %s", symbol, e, exc_info=True)
            return None

    async def _fetch_ohlcv(self, symbol: str) -> Optional[Dict[str, np.ndarray]]:
        """Fetch OHLCV data from Hyperliquid with 429 retry backoff."""
        max_retries = 3

        for attempt in range(max_retries + 1):
            try:
                loop = asyncio.get_event_loop()

                # Calculate time range
                end_time = int(datetime.now(timezone.utc).timestamp() * 1000)

                # Timeframe to milliseconds
                tf_map = {
                    "1m": 60 * 1000,
                    "5m": 5 * 60 * 1000,
                    "15m": 15 * 60 * 1000,
                    "1h": 60 * 60 * 1000,
                    "4h": 4 * 60 * 60 * 1000,
                    "1d": 24 * 60 * 60 * 1000,
                }
                interval_ms = tf_map.get(self._state_config.timeframe, 4 * 60 * 60 * 1000)
                start_time = end_time - (self._state_config.bars_to_fetch * interval_ms)

                # Fetch candles (positional args: symbol, interval, start_time, end_time)
                candles = await loop.run_in_executor(
                    None,
                    lambda: self._info.candles_snapshot(
                        symbol,
                        self._state_config.timeframe,
                        start_time,
                        end_time,
                    )
                )

                if not candles:
                    self._logger.warning("No candles returned for %s", symbol)
                    return None

                # Parse candles
                opens = []
                highs = []
                lows = []
                closes = []
                volumes = []

                for candle in candles:
                    opens.append(float(candle.get("o", 0)))
                    highs.append(float(candle.get("h", 0)))
                    lows.append(float(candle.get("l", 0)))
                    closes.append(float(candle.get("c", 0)))
                    volumes.append(float(candle.get("v", 0)))

                return {
                    "open": np.array(opens),
                    "high": np.array(highs),
                    "low": np.array(lows),
                    "close": np.array(closes),
                    "volume": np.array(volumes),
                }

            except Exception as e:
                err_msg = str(e).lower()
                is_rate_limit = "429" in err_msg or "rate" in err_msg or "too many" in err_msg
                if is_rate_limit and attempt < max_retries:
                    backoff = (2 ** attempt) * 2  # 2s, 4s, 8s
                    self._logger.warning(
                        "Rate limited fetching %s (attempt %d/%d), backing off %.0fs",
                        symbol, attempt + 1, max_retries, backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue
                self._logger.error("Failed to fetch OHLCV for %s: %s", symbol, e)
                return None

    # =========================================================================
    # EMA Slope Calculation
    # =========================================================================

    def _compute_ema_slopes(
        self,
        symbol: str,
        ema9_current: float,
        ema21_current: float,
    ) -> tuple[Decimal, Decimal]:
        """Compute EMA9 and EMA21 slopes using 4-bar lookback.

        Formula matches ml_dataset._extract_features:
            slope = (ema_current - ema_4bars_ago) / ema_4bars_ago

        Maintains a per-symbol deque of the last 5 EMA values.
        Returns (Decimal("0"), Decimal("0")) if fewer than 5 data points
        are available (i.e. 4-bar lookback not yet possible).
        """
        # Initialize history buffers for new symbols
        if symbol not in self._ema9_history:
            self._ema9_history[symbol] = deque(maxlen=5)
            self._ema21_history[symbol] = deque(maxlen=5)

        # Append current values
        self._ema9_history[symbol].append(ema9_current)
        self._ema21_history[symbol].append(ema21_current)

        ema9_buf = self._ema9_history[symbol]
        ema21_buf = self._ema21_history[symbol]

        # Need at least 5 values (current + 4 bars ago)
        if len(ema9_buf) < 5:
            return Decimal("0"), Decimal("0")

        # slope = (current - 4_bars_ago) / 4_bars_ago
        ema9_4ago = ema9_buf[0]  # oldest in the 5-element deque
        ema21_4ago = ema21_buf[0]

        if ema9_4ago > 0:
            ema9_slope = Decimal(str((ema9_current - ema9_4ago) / ema9_4ago))
        else:
            ema9_slope = Decimal("0")

        if ema21_4ago > 0:
            ema21_slope = Decimal(str((ema21_current - ema21_4ago) / ema21_4ago))
        else:
            ema21_slope = Decimal("0")

        return ema9_slope, ema21_slope

    # =========================================================================
    # RSI Slope Calculation
    # =========================================================================

    def _compute_rsi_slope(self, symbol: str, rsi_current: float) -> Decimal:
        """Compute RSI slope using 2-bar lookback: RSI[i] - RSI[i-2].

        Maintains a per-symbol deque of the last 3 RSI values.
        Returns Decimal("0") if fewer than 3 data points are available.
        """
        if symbol not in self._rsi_history:
            self._rsi_history[symbol] = deque(maxlen=3)

        self._rsi_history[symbol].append(rsi_current)

        buf = self._rsi_history[symbol]
        if len(buf) < 3:
            return Decimal("0")

        # slope = RSI[current] - RSI[2 bars ago]
        return Decimal(str(rsi_current - buf[0]))

    # =========================================================================
    # Regime Detection
    # =========================================================================

    def _detect_regime(
        self,
        adx: float,
        ema200_slope: float,
        choppiness: float,
        symbol: str,
    ) -> Regime:
        """
        Detect market regime with level hysteresis.

        Level hysteresis prevents whipsaw:
        - Entering TREND requires ADX >= trend_adx_entry_min (stricter)
        - Staying in TREND only requires ADX >= trend_adx_exit_min (lenient)
        - RANGE: ADX <= range_adx_max and choppiness >= choppiness_range_min
        - CHAOS: Everything else

        Note: ema200_slope is intentionally NOT used for regime classification.
        Direction is captured by EMA9/21 crossover (the ML entry signal).
        The ema200_slope parameter is kept in the signature for backward
        compatibility but is ignored.

        Hysteresis: Regime only changes after N consecutive bars confirm.
        """
        cfg = self._state_config

        # Level hysteresis: use different ADX thresholds depending on
        # whether we are already confirmed in TREND or not
        current_confirmed = self._confirmed_regime.get(symbol)

        if current_confirmed == Regime.TREND:
            # Already in TREND — use lower exit threshold (stay in TREND longer)
            if adx >= cfg.trend_adx_exit_min:
                raw_regime = Regime.TREND
            elif adx <= cfg.range_adx_max and choppiness >= cfg.choppiness_range_min:
                raw_regime = Regime.RANGE
            else:
                raw_regime = Regime.CHAOS
        else:
            # Not in TREND — use stricter entry threshold
            if adx >= cfg.trend_adx_entry_min:
                raw_regime = Regime.TREND
            elif adx <= cfg.range_adx_max and choppiness >= cfg.choppiness_range_min:
                raw_regime = Regime.RANGE
            else:
                raw_regime = Regime.CHAOS

        # Initialize confirmed regime on first call for this symbol
        if symbol not in self._confirmed_regime:
            self._confirmed_regime[symbol] = raw_regime
            self._regime_change_counter[symbol] = 0
            return raw_regime

        confirmed = self._confirmed_regime[symbol]

        if raw_regime != confirmed:
            pending = self._pending_regime.get(symbol)
            if pending is not None and pending != raw_regime:
                # Different alternative than last bar — reset counter
                self._regime_change_counter[symbol] = 1
            else:
                self._regime_change_counter[symbol] += 1
            self._pending_regime[symbol] = raw_regime

            if self._regime_change_counter[symbol] >= cfg.regime_confirmation_bars:
                self._confirmed_regime[symbol] = raw_regime
                self._regime_change_counter[symbol] = 0
                self._pending_regime.pop(symbol, None)
                return raw_regime
            return confirmed
        else:
            self._regime_change_counter[symbol] = 0
            self._pending_regime.pop(symbol, None)
            return confirmed

    def init_confirmed_regime_for_symbols(
        self, symbols: List[str], regime: Regime = Regime.TREND
    ) -> None:
        """Initialize confirmed regime for symbols with open positions.

        Called at startup to prevent the confirmation counter from being
        bypassed after a service restart. Without this, the first reading
        becomes confirmed immediately, which can cause a false regime
        change and close positions that should remain open.

        Args:
            symbols: List of symbols with open positions.
            regime: Regime to initialize to (default TREND since we only
                    open positions in TREND).
        """
        for symbol in symbols:
            if symbol not in self._confirmed_regime:
                self._confirmed_regime[symbol] = regime
                self._regime_change_counter[symbol] = 0
                self._logger.info(
                    "Initialized confirmed regime for %s to %s (open position)",
                    symbol, regime.value,
                )

    # =========================================================================
    # Publishing
    # =========================================================================

    async def _publish_state(self, state: MarketState) -> None:
        """Publish market state to message bus."""
        if self.bus:
            await self.publish(Topic.MARKET_STATE, state.model_dump())

            # Also publish regime separately for interested services
            await self.publish(Topic.REGIME, {
                "symbol": state.symbol,
                "regime": state.regime.value,
                "timestamp": state.timestamp.isoformat(),
                "adx": float(state.adx),
                "trend_direction": state.trend_direction.value,
            })

    # =========================================================================
    # Public API
    # =========================================================================

    def get_state(self, symbol: str) -> Optional[MarketState]:
        """Get current market state for a symbol."""
        return self._market_states.get(symbol)

    def get_all_states(self) -> Dict[str, MarketState]:
        """Get all current market states."""
        return self._market_states.copy()

    def get_regime(self, symbol: str) -> Optional[Regime]:
        """Get current regime for a symbol."""
        state = self._market_states.get(symbol)
        return state.regime if state else None

    @property
    def metrics(self) -> Dict[str, Any]:
        """Get service metrics."""
        return {
            "assets_tracked": len(self._market_states),
            "assets": list(self._market_states.keys()),
            "regimes": {
                symbol: state.regime.value
                for symbol, state in self._market_states.items()
            },
            "last_update": {
                symbol: state.timestamp.isoformat()
                for symbol, state in self._market_states.items()
            },
        }


# =============================================================================
# Factory
# =============================================================================

def create_market_state_service(
    bus: Optional[MessageBus] = None,
    config: Optional[MarketStateConfig] = None,
    testnet: bool = True,
) -> MarketStateService:
    """Factory function to create MarketStateService."""
    return MarketStateService(
        name="market_state",
        bus=bus,
        config=config,
        testnet=testnet,
    )
