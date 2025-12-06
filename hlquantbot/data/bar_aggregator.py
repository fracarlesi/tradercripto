"""Bar aggregation from ticks and trades with HFT sub-second support."""

import asyncio
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Callable, Tuple

from ..core.models import Tick, Bar
from ..core.enums import TimeFrame, TIMEFRAME_SECONDS


# Sub-second timeframes for HFT
HFT_TIMEFRAMES = {TimeFrame.S1, TimeFrame.S3, TimeFrame.S5, TimeFrame.S15, TimeFrame.S30}


class BarBuilder:
    """Builds a single bar from ticks."""

    def __init__(self, symbol: str, timeframe: TimeFrame, start_time: datetime):
        self.symbol = symbol
        self.timeframe = timeframe
        self.start_time = start_time
        self.open: Optional[Decimal] = None
        self.high: Optional[Decimal] = None
        self.low: Optional[Decimal] = None
        self.close: Optional[Decimal] = None
        self.volume: Decimal = Decimal(0)
        self.trades_count: int = 0

    def update(self, price: Decimal, volume: Decimal = Decimal(0)):
        """Update bar with new price."""
        if self.open is None:
            self.open = price
            self.high = price
            self.low = price

        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += volume
        self.trades_count += 1

    def to_bar(self) -> Optional[Bar]:
        """Convert to Bar object."""
        if self.open is None:
            return None

        return Bar(
            symbol=self.symbol,
            timeframe=self.timeframe,
            timestamp=self.start_time,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            trades_count=self.trades_count,
        )


class BarAggregator:
    """Aggregates ticks into bars for multiple timeframes with HFT sub-second support."""

    def __init__(
        self,
        symbols: List[str],
        timeframes: List[TimeFrame] = None,
        hft_timeframes: List[TimeFrame] = None,
    ):
        self.symbols = symbols
        self.timeframes = timeframes or [TimeFrame.M1, TimeFrame.M5, TimeFrame.M15]

        # HFT timeframes (sub-second)
        self.hft_timeframes = hft_timeframes or []

        # Combine all timeframes
        self.all_timeframes = list(set(self.timeframes + self.hft_timeframes))

        # Current building bars: symbol -> timeframe -> BarBuilder
        self._builders: Dict[str, Dict[TimeFrame, BarBuilder]] = defaultdict(dict)

        # Completed bars: symbol -> timeframe -> List[Bar]
        self._completed_bars: Dict[str, Dict[TimeFrame, List[Bar]]] = defaultdict(
            lambda: defaultdict(list)
        )

        # Callbacks (separate for normal and HFT bars)
        self._bar_callbacks: List[Callable[[Bar], None]] = []
        self._hft_bar_callbacks: List[Callable[[Bar], None]] = []

        # Max bars to keep per symbol/timeframe
        # HFT keeps more bars due to higher frequency
        self._max_bars = 500
        self._max_hft_bars = 2000

        # Track last update time for high-frequency processing
        self._last_tick_time: Dict[str, datetime] = {}

    def is_hft_timeframe(self, timeframe: TimeFrame) -> bool:
        """Check if timeframe is HFT (sub-second)."""
        return timeframe in HFT_TIMEFRAMES

    def on_bar_complete(self, callback: Callable[[Bar], None]):
        """Register callback for completed bars."""
        self._bar_callbacks.append(callback)

    def on_hft_bar_complete(self, callback: Callable[[Bar], None]):
        """Register callback for completed HFT (sub-second) bars."""
        self._hft_bar_callbacks.append(callback)

    def _get_bar_start_time(self, timestamp: datetime, timeframe: TimeFrame) -> datetime:
        """Calculate the start time of the bar containing this timestamp."""
        seconds = TIMEFRAME_SECONDS.get(timeframe, 60)
        ts = timestamp.replace(tzinfo=timezone.utc)
        epoch = ts.timestamp()
        bar_epoch = (epoch // seconds) * seconds
        return datetime.fromtimestamp(bar_epoch, tz=timezone.utc)

    def process_tick(self, tick: Tick):
        """Process a tick and update bars (all timeframes including HFT)."""
        symbol = tick.symbol
        if symbol not in self.symbols:
            return

        # Track tick time for frequency monitoring
        self._last_tick_time[symbol] = tick.timestamp

        # Update all timeframes
        for timeframe in self.all_timeframes:
            self._update_bar(symbol, timeframe, tick.mid_price, tick.timestamp)

    def process_trade(self, symbol: str, price: Decimal, volume: Decimal, timestamp: datetime):
        """Process a trade and update bars (all timeframes including HFT)."""
        if symbol not in self.symbols:
            return

        for timeframe in self.all_timeframes:
            self._update_bar(symbol, timeframe, price, timestamp, volume)

    def _update_bar(
        self,
        symbol: str,
        timeframe: TimeFrame,
        price: Decimal,
        timestamp: datetime,
        volume: Decimal = Decimal(0),
    ):
        """Update or create bar for symbol/timeframe."""
        bar_start = self._get_bar_start_time(timestamp, timeframe)

        # Get or create builder
        builder = self._builders[symbol].get(timeframe)

        if builder is None or builder.start_time != bar_start:
            # Complete previous bar if exists
            if builder is not None:
                completed_bar = builder.to_bar()
                if completed_bar:
                    self._add_completed_bar(completed_bar)

            # Create new builder
            builder = BarBuilder(symbol, timeframe, bar_start)
            self._builders[symbol][timeframe] = builder

        # Update current bar
        builder.update(price, volume)

    def _add_completed_bar(self, bar: Bar):
        """Add a completed bar to storage and notify callbacks."""
        bars_list = self._completed_bars[bar.symbol][bar.timeframe]
        bars_list.append(bar)

        # Trim to max size (HFT bars have higher limit)
        is_hft = self.is_hft_timeframe(bar.timeframe)
        max_bars = self._max_hft_bars if is_hft else self._max_bars

        if len(bars_list) > max_bars:
            bars_list.pop(0)

        # Notify callbacks (separate for HFT and normal bars)
        if is_hft:
            for callback in self._hft_bar_callbacks:
                try:
                    callback(bar)
                except Exception:
                    pass
        else:
            for callback in self._bar_callbacks:
                try:
                    callback(bar)
                except Exception:
                    pass

        # Also notify all-bars callbacks
        for callback in self._bar_callbacks:
            if callback not in self._hft_bar_callbacks:
                try:
                    callback(bar)
                except Exception:
                    pass

    def get_bars(self, symbol: str, timeframe: TimeFrame, count: int = 100) -> List[Bar]:
        """Get completed bars for a symbol/timeframe."""
        bars = self._completed_bars[symbol][timeframe]
        return bars[-count:] if len(bars) > count else bars.copy()

    def get_current_bar(self, symbol: str, timeframe: TimeFrame) -> Optional[Bar]:
        """Get the currently building bar."""
        builder = self._builders.get(symbol, {}).get(timeframe)
        if builder:
            return builder.to_bar()
        return None

    def force_close_bars(self):
        """Force close all current bars (useful at shutdown)."""
        for symbol_builders in self._builders.values():
            for builder in symbol_builders.values():
                completed_bar = builder.to_bar()
                if completed_bar:
                    self._add_completed_bar(completed_bar)
        self._builders.clear()

    # -------------------------------------------------------------------------
    # HFT Helper Methods
    # -------------------------------------------------------------------------
    def get_hft_bars(self, symbol: str, timeframe: TimeFrame, count: int = 100) -> List[Bar]:
        """Get HFT bars for a symbol/timeframe."""
        if not self.is_hft_timeframe(timeframe):
            return []
        return self.get_bars(symbol, timeframe, count)

    def get_vwap(self, symbol: str, timeframe: TimeFrame, periods: int = 20) -> Optional[Decimal]:
        """Calculate VWAP from recent bars (for mean reversion)."""
        bars = self.get_bars(symbol, timeframe, periods)
        if not bars:
            return None

        total_volume = Decimal(0)
        total_value = Decimal(0)

        for bar in bars:
            typical_price = (bar.high + bar.low + bar.close) / 3
            total_value += typical_price * bar.volume
            total_volume += bar.volume

        if total_volume == 0:
            # No volume, use simple average
            return sum(b.close for b in bars) / len(bars)

        return total_value / total_volume

    def get_range(self, symbol: str, timeframe: TimeFrame, periods: int = 10) -> Optional[Tuple[Decimal, Decimal]]:
        """Get high/low range over recent bars (for breakout detection)."""
        bars = self.get_bars(symbol, timeframe, periods)
        if not bars:
            return None

        high = max(b.high for b in bars)
        low = min(b.low for b in bars)
        return (low, high)

    def get_tick_frequency(self, symbol: str, window_seconds: float = 5.0) -> float:
        """Get tick frequency (ticks per second) for a symbol."""
        # This is a simplified version - in production would track actual ticks
        last_tick = self._last_tick_time.get(symbol)
        if not last_tick:
            return 0.0

        # Estimate based on most recent sub-second bar
        if TimeFrame.S1 in self.all_timeframes:
            bars = self.get_bars(symbol, TimeFrame.S1, int(window_seconds))
            if bars:
                total_trades = sum(b.trades_count for b in bars)
                return total_trades / window_seconds

        return 0.0

    def add_hft_timeframe(self, timeframe: TimeFrame):
        """Dynamically add an HFT timeframe."""
        if timeframe not in self.hft_timeframes:
            self.hft_timeframes.append(timeframe)
            if timeframe not in self.all_timeframes:
                self.all_timeframes.append(timeframe)

    def remove_hft_timeframe(self, timeframe: TimeFrame):
        """Dynamically remove an HFT timeframe."""
        if timeframe in self.hft_timeframes:
            self.hft_timeframes.remove(timeframe)
            # Keep in all_timeframes if it's also in regular timeframes
            if timeframe not in self.timeframes and timeframe in self.all_timeframes:
                self.all_timeframes.remove(timeframe)
