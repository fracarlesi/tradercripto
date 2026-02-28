"""
Market Data Service
===================

Handles 1-min bar streaming, VWAP calculation, and Opening Range detection.
Publishes ORBRange on Topic.OPENING_RANGE at end of OR window.
Publishes FuturesMarketState on Topic.MARKET_DATA at regular intervals.
"""

import logging
from datetime import datetime, time, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .base import BaseService
from .message_bus import MessageBus
from .ib_client import IBClient
from ..config.loader import OpeningRangeConfig
from ..core.contracts import CONTRACTS, FuturesSpec
from ..core.enums import Direction, SessionPhase, Topic
from ..core.models import FuturesMarketState, ORBRange

logger = logging.getLogger(__name__)


class VWAPCalculator:
    """Incremental VWAP calculator.

    VWAP = sum(typical_price * volume) / sum(volume)
    typical_price = (high + low + close) / 3
    Resets each session.
    """

    def __init__(self) -> None:
        self._cum_tp_vol = Decimal("0")
        self._cum_vol = Decimal("0")

    def update(self, high: Decimal, low: Decimal, close: Decimal, volume: Decimal) -> Decimal:
        typical = (high + low + close) / 3
        self._cum_tp_vol += typical * volume
        self._cum_vol += volume
        if self._cum_vol == 0:
            return close
        return self._cum_tp_vol / self._cum_vol

    def reset(self) -> None:
        self._cum_tp_vol = Decimal("0")
        self._cum_vol = Decimal("0")

    @property
    def vwap(self) -> Decimal:
        if self._cum_vol == 0:
            return Decimal("0")
        return self._cum_tp_vol / self._cum_vol


class ATRCalculator:
    """ATR(14) calculator using Wilder's smoothing."""

    def __init__(self, period: int = 14) -> None:
        self._period = period
        self._true_ranges: List[Decimal] = []
        self._atr: Decimal = Decimal("0")
        self._prev_close: Optional[Decimal] = None

    def update(self, high: Decimal, low: Decimal, close: Decimal) -> Decimal:
        if self._prev_close is not None:
            tr = max(
                high - low,
                abs(high - self._prev_close),
                abs(low - self._prev_close),
            )
        else:
            tr = high - low

        self._prev_close = close
        self._true_ranges.append(tr)

        if len(self._true_ranges) < self._period:
            self._atr = sum(self._true_ranges) / len(self._true_ranges)
        elif len(self._true_ranges) == self._period:
            self._atr = sum(self._true_ranges) / self._period
        else:
            # Wilder's smoothing
            self._atr = (self._atr * (self._period - 1) + tr) / self._period

        return self._atr

    def reset(self) -> None:
        self._true_ranges.clear()
        self._atr = Decimal("0")
        self._prev_close = None

    @property
    def atr(self) -> Decimal:
        return self._atr


class MarketDataService(BaseService):
    """Accumulates bars, calculates VWAP/ATR, detects Opening Range."""

    def __init__(
        self,
        ib_client: IBClient,
        or_config: OpeningRangeConfig,
        symbols: List[str],
        bus: Optional[MessageBus] = None,
    ) -> None:
        super().__init__(name="market_data", bus=bus, loop_interval_seconds=5.0)
        self._ib_client = ib_client
        self._or_config = or_config
        self._symbols = symbols

        # Per-symbol state
        self._vwap: Dict[str, VWAPCalculator] = {}
        self._atr: Dict[str, ATRCalculator] = {}
        self._or_bars: Dict[str, List[Any]] = {}
        self._or_range: Dict[str, Optional[ORBRange]] = {}
        self._or_published: Dict[str, bool] = {}
        self._bar_subscriptions: Dict[str, Any] = {}

        for symbol in symbols:
            self._vwap[symbol] = VWAPCalculator()
            self._atr[symbol] = ATRCalculator()
            self._or_bars[symbol] = []
            self._or_range[symbol] = None
            self._or_published[symbol] = False

    async def _on_start(self) -> None:
        for symbol in self._symbols:
            bars = await self._ib_client.request_historical_bars(
                symbol=symbol,
                duration="1 D",
                bar_size=self._or_config.bar_size,
                keep_up_to_date=True,
            )
            self._bar_subscriptions[symbol] = bars

            # Process existing bars for VWAP/ATR warmup
            for bar in bars:
                h = Decimal(str(bar.high))
                l = Decimal(str(bar.low))
                c = Decimal(str(bar.close))
                v = Decimal(str(bar.volume))
                self._vwap[symbol].update(h, l, c, v)
                self._atr[symbol].update(h, l, c)

            self._logger.info("Subscribed to %s bars (%d historical)", symbol, len(bars))

    async def _on_stop(self) -> None:
        for symbol, bars in self._bar_subscriptions.items():
            self._ib_client.cancel_historical_data(bars)
        self._bar_subscriptions.clear()

    async def _run_iteration(self) -> None:
        for symbol in self._symbols:
            bars = self._bar_subscriptions.get(symbol)
            if not bars:
                continue

            # Process new bars
            spec = self._ib_client.get_spec(symbol)
            await self._process_bars(symbol, bars, spec)

    async def _process_bars(
        self, symbol: str, bars: Any, spec: FuturesSpec
    ) -> None:
        """Process latest bars: update indicators and check OR window."""
        if not bars:
            return

        latest = bars[-1]
        h = Decimal(str(latest.high))
        l = Decimal(str(latest.low))
        c = Decimal(str(latest.close))
        v = Decimal(str(latest.volume))

        # Update indicators
        vwap = self._vwap[symbol].update(h, l, c, v)
        atr = self._atr[symbol].update(h, l, c)

        # Check if bar is in OR window
        bar_time = latest.date if hasattr(latest, "date") else None
        if bar_time and self._is_in_or_window(bar_time):
            self._or_bars[symbol].append(latest)

        # Check if OR window just ended
        if bar_time and self._is_or_window_ended(bar_time) and not self._or_published[symbol]:
            await self._calculate_and_publish_or(symbol, spec)

        # Determine session phase
        phase = self._get_session_phase(bar_time) if bar_time else SessionPhase.CLOSED

        # Publish market state
        state = FuturesMarketState(
            symbol=symbol,
            last_price=c,
            vwap=vwap,
            atr_14=atr,
            volume=v,
            session_phase=phase,
            timestamp=datetime.now(timezone.utc),
        )
        await self.publish(Topic.MARKET_DATA, state.model_dump())

    def _is_in_or_window(self, bar_time: Any) -> bool:
        """Check if bar timestamp falls within Opening Range window."""
        try:
            if hasattr(bar_time, "time"):
                t = bar_time.time()
            else:
                return False

            or_start = time.fromisoformat(self._or_config.or_start)
            or_end = time.fromisoformat(self._or_config.or_end)
            return or_start <= t < or_end
        except Exception:
            return False

    def _is_or_window_ended(self, bar_time: Any) -> bool:
        """Check if we've passed the OR window end."""
        try:
            if hasattr(bar_time, "time"):
                t = bar_time.time()
            else:
                return False
            or_end = time.fromisoformat(self._or_config.or_end)
            return t >= or_end
        except Exception:
            return False

    def _get_session_phase(self, bar_time: Any) -> SessionPhase:
        """Determine current session phase from bar timestamp."""
        try:
            if hasattr(bar_time, "time"):
                t = bar_time.time()
            else:
                return SessionPhase.CLOSED

            or_start = time.fromisoformat(self._or_config.or_start)
            or_end = time.fromisoformat(self._or_config.or_end)
            max_entry = time(11, 30)
            eod = time(15, 45)
            close = time(16, 0)

            if t < or_start:
                return SessionPhase.PRE_MARKET
            elif or_start <= t < or_end:
                return SessionPhase.OPENING_RANGE
            elif or_end <= t < max_entry:
                return SessionPhase.ACTIVE_TRADING
            elif max_entry <= t < eod:
                return SessionPhase.AFTERNOON
            elif eod <= t < close:
                return SessionPhase.EOD_FLATTEN
            else:
                return SessionPhase.CLOSED
        except Exception:
            return SessionPhase.CLOSED

    async def _calculate_and_publish_or(
        self, symbol: str, spec: FuturesSpec
    ) -> None:
        """Calculate Opening Range from accumulated bars and publish."""
        bars = self._or_bars[symbol]
        if not bars:
            self._logger.warning("No bars for %s OR calculation", symbol)
            return

        or_high = Decimal(str(max(b.high for b in bars)))
        or_low = Decimal(str(min(b.low for b in bars)))
        midpoint = (or_high + or_low) / 2
        total_volume = Decimal(str(sum(b.volume for b in bars)))
        range_ticks = int((or_high - or_low) / spec.tick_size)

        # Validate range
        valid = (
            self._or_config.min_range_ticks <= range_ticks <= self._or_config.max_range_ticks
        )

        or_range = ORBRange(
            symbol=symbol,
            or_high=or_high,
            or_low=or_low,
            midpoint=midpoint,
            range_ticks=range_ticks,
            volume=total_volume,
            vwap=self._vwap[symbol].vwap,
            timestamp=datetime.now(timezone.utc),
            valid=valid,
        )

        self._or_range[symbol] = or_range
        self._or_published[symbol] = True

        if valid:
            await self.publish(Topic.OPENING_RANGE, or_range.model_dump())
            self._logger.info(
                "OR published: %s high=%.2f low=%.2f range=%d ticks",
                symbol, float(or_high), float(or_low), range_ticks,
            )
        else:
            self._logger.info(
                "OR invalid: %s range=%d ticks (min=%d, max=%d)",
                symbol, range_ticks,
                self._or_config.min_range_ticks,
                self._or_config.max_range_ticks,
            )

    def get_or_range(self, symbol: str) -> Optional[ORBRange]:
        """Get the calculated Opening Range for a symbol."""
        return self._or_range.get(symbol)

    def get_vwap(self, symbol: str) -> Decimal:
        """Get current VWAP for a symbol."""
        calc = self._vwap.get(symbol)
        return calc.vwap if calc else Decimal("0")

    def get_atr(self, symbol: str) -> Decimal:
        """Get current ATR(14) for a symbol."""
        calc = self._atr.get(symbol)
        return calc.atr if calc else Decimal("0")

    def reset_session(self) -> None:
        """Reset all session state (called at start of new session)."""
        for symbol in self._symbols:
            self._vwap[symbol].reset()
            self._atr[symbol].reset()
            self._or_bars[symbol].clear()
            self._or_range[symbol] = None
            self._or_published[symbol] = False
        self._logger.info("Session state reset for all symbols")
