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
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

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

    rs = np.where(avg_loss != 0, avg_gain / avg_loss, 100)
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

    # Calculate DI+ and DI-
    plus_di = np.where(atr != 0, 100 * plus_dm_smooth / (atr * period), 0)
    minus_di = np.where(atr != 0, 100 * minus_dm_smooth / (atr * period), 0)

    # Calculate DX
    di_sum = plus_di + minus_di
    dx = np.where(di_sum != 0, 100 * np.abs(plus_di - minus_di) / di_sum, 0)

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

    # Regime detection thresholds
    trend_adx_min: float = 25.0
    range_adx_max: float = 20.0
    ema_slope_threshold: float = 0.001
    choppiness_range_min: float = 60.0
    regime_confirmation_bars: int = 2

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
        db: Optional[Any] = None,
        config: Optional[MarketStateConfig] = None,
        testnet: bool = True,
    ) -> None:
        """Initialize MarketStateService."""
        self._state_config = config or MarketStateConfig()

        super().__init__(
            name=name,
            bus=bus,
            db=db,
            loop_interval_seconds=self._state_config.interval_seconds,
        )

        self._testnet = testnet
        self._info: Optional[Info] = None

        # Cache for OHLCV data
        self._ohlcv_cache: Dict[str, Dict[str, np.ndarray]] = {}
        self._last_fetch: Dict[str, datetime] = {}

        # Current market states
        self._market_states: Dict[str, MarketState] = {}

        # Regime history for hysteresis
        self._regime_history: Dict[str, List[Regime]] = {}

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

    async def _run_iteration(self) -> None:
        """Fetch and publish market states."""
        await self._fetch_all_states()

    async def _health_check_impl(self) -> bool:
        """Check service health."""
        if self._info is None:
            return False

        # Check if we have states for all assets
        for asset in self._state_config.assets:
            if asset not in self._market_states:
                return False

        return True

    # =========================================================================
    # Data Fetching
    # =========================================================================

    async def _fetch_all_states(self) -> None:
        """Fetch market state for all configured assets."""
        for symbol in self._state_config.assets:
            try:
                state = await self._fetch_market_state(symbol)
                if state:
                    self._market_states[symbol] = state
                    await self._publish_state(state)
            except Exception as e:
                self._logger.error("Failed to fetch state for %s: %s", symbol, e)

    async def _fetch_market_state(self, symbol: str) -> Optional[MarketState]:
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

            # EMAs
            ema50 = calculate_ema(close, 50)
            ema200 = calculate_ema(close, 200)

            # EMA200 slope (normalized)
            ema200_slope = (ema200[-1] - ema200[-5]) / ema200[-5] if len(ema200) >= 5 else 0

            # ADX
            adx_values = calculate_adx(high, low, close, 14)
            adx = Decimal(str(max(0, min(100, adx_values[-1]))))

            # RSI
            rsi_values = calculate_rsi(close, 14)
            rsi = Decimal(str(max(0, min(100, rsi_values[-1]))))

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

            # Trend direction
            if current_price > Decimal(str(ema200[-1])):
                trend_direction = Direction.LONG
            elif current_price < Decimal(str(ema200[-1])):
                trend_direction = Direction.SHORT
            else:
                trend_direction = Direction.FLAT

            # Build MarketState
            state = MarketState(
                symbol=symbol,
                timeframe=self._state_config.timeframe,
                timestamp=datetime.utcnow(),
                open=Decimal(str(ohlcv["open"][-1])),
                high=Decimal(str(high[-1])),
                low=Decimal(str(low[-1])),
                close=current_price,
                volume=Decimal(str(volume[-1])),
                atr=atr,
                atr_pct=atr_pct,
                adx=adx,
                rsi=rsi,
                ema50=Decimal(str(ema50[-1])),
                ema200=Decimal(str(ema200[-1])),
                ema200_slope=Decimal(str(ema200_slope)),
                choppiness=choppiness,
                bb_lower=Decimal(str(bb_lower[-1])),
                bb_mid=Decimal(str(bb_mid[-1])),
                bb_upper=Decimal(str(bb_upper[-1])),
                regime=regime,
                trend_direction=trend_direction,
                bars_count=len(close),
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
        """Fetch OHLCV data from Hyperliquid."""
        try:
            loop = asyncio.get_event_loop()

            # Calculate time range
            end_time = int(datetime.utcnow().timestamp() * 1000)

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
            self._logger.error("Failed to fetch OHLCV for %s: %s", symbol, e)
            return None

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
        Detect market regime with hysteresis.

        Rules:
        - TREND: ADX > 25 and EMA200 slope confirms direction
        - RANGE: ADX < 20 and choppiness > 60
        - CHAOS: Everything else (stay flat)

        Hysteresis: Regime only changes after N consecutive bars confirm.
        """
        cfg = self._state_config

        # Determine raw regime
        if adx >= cfg.trend_adx_min and abs(ema200_slope) >= cfg.ema_slope_threshold:
            raw_regime = Regime.TREND
        elif adx <= cfg.range_adx_max and choppiness >= cfg.choppiness_range_min:
            raw_regime = Regime.RANGE
        else:
            raw_regime = Regime.CHAOS

        # Initialize history if needed
        if symbol not in self._regime_history:
            self._regime_history[symbol] = []

        history = self._regime_history[symbol]
        history.append(raw_regime)

        # Keep only last N+1 entries
        max_history = cfg.regime_confirmation_bars + 1
        if len(history) > max_history:
            history = history[-max_history:]
            self._regime_history[symbol] = history

        # Check if regime is confirmed
        if len(history) >= cfg.regime_confirmation_bars:
            recent = history[-cfg.regime_confirmation_bars:]
            if all(r == raw_regime for r in recent):
                return raw_regime

            # Return previous confirmed regime if no consensus
            if len(history) > cfg.regime_confirmation_bars:
                return history[-cfg.regime_confirmation_bars - 1]

        return raw_regime

    # =========================================================================
    # Publishing
    # =========================================================================

    async def _publish_state(self, state: MarketState) -> None:
        """Publish market state to message bus and persist to database."""
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

        # Persist to database for dashboard
        await self._save_to_db(state)

    async def _save_to_db(self, state: MarketState) -> None:
        """Persist market state to database."""
        if not self.db or not self.db.pool:
            return

        try:
            async with self.db.pool.acquire() as conn:
                await conn.execute(
                """
                INSERT INTO market_states (
                    timestamp, symbol, timeframe,
                    open, high, low, close, volume,
                    atr, atr_pct, adx, rsi, ema50, ema200, ema200_slope,
                    choppiness, bb_lower, bb_mid, bb_upper,
                    regime, trend_direction, bars_count
                ) VALUES (
                    $1, $2, $3,
                    $4, $5, $6, $7, $8,
                    $9, $10, $11, $12, $13, $14, $15,
                    $16, $17, $18, $19,
                    $20, $21, $22
                )
                ON CONFLICT (timestamp, symbol, timeframe)
                DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume,
                    atr = EXCLUDED.atr,
                    atr_pct = EXCLUDED.atr_pct,
                    adx = EXCLUDED.adx,
                    rsi = EXCLUDED.rsi,
                    ema50 = EXCLUDED.ema50,
                    ema200 = EXCLUDED.ema200,
                    ema200_slope = EXCLUDED.ema200_slope,
                    choppiness = EXCLUDED.choppiness,
                    bb_lower = EXCLUDED.bb_lower,
                    bb_mid = EXCLUDED.bb_mid,
                    bb_upper = EXCLUDED.bb_upper,
                    regime = EXCLUDED.regime,
                    trend_direction = EXCLUDED.trend_direction,
                    bars_count = EXCLUDED.bars_count
                """,
                state.timestamp,
                state.symbol,
                state.timeframe,
                float(state.open),
                float(state.high),
                float(state.low),
                float(state.close),
                float(state.volume),
                float(state.atr),
                float(state.atr_pct),
                float(state.adx),
                float(state.rsi),
                float(state.ema50),
                float(state.ema200),
                float(state.ema200_slope),
                float(state.choppiness) if state.choppiness else None,
                float(state.bb_lower) if state.bb_lower else None,
                float(state.bb_mid) if state.bb_mid else None,
                float(state.bb_upper) if state.bb_upper else None,
                state.regime.value,
                state.trend_direction.value,
                state.bars_count,
            )
        except Exception as e:
            self._logger.error("Failed to save market state to DB: %s", e)

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
    db: Optional[Any] = None,
    config: Optional[MarketStateConfig] = None,
    testnet: bool = True,
) -> MarketStateService:
    """Factory function to create MarketStateService."""
    return MarketStateService(
        name="market_state",
        bus=bus,
        db=db,
        config=config,
        testnet=testnet,
    )
