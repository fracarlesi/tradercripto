"""Base strategy interface."""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Dict, Union

from ..core.models import (
    ProposedTrade,
    AccountState,
    Bar,
    MarketContext,
    Position,
)
from ..core.enums import StrategyId, Side, MarketRegime, TimeFrame
from ..config.settings import Settings, StrategyConfigBase


class BaseStrategy(ABC):
    """
    Base class for all trading strategies.

    Strategies generate ProposedTrade objects which are then
    filtered and sized by the Risk Engine.
    """

    def __init__(self, settings: Settings, strategy_id: StrategyId):
        self.settings = settings
        self.strategy_id = strategy_id
        self.config: StrategyConfigBase = settings.get_strategy_config(strategy_id)

        # State
        self._last_signal_time: Dict[str, datetime] = {}
        self._current_regime: MarketRegime = MarketRegime.UNCERTAIN

    @property
    def name(self) -> str:
        return self.strategy_id.value

    @property
    def is_enabled(self) -> bool:
        return self.config.enabled

    @property
    def symbols(self) -> List[str]:
        return self.config.symbols

    def set_regime(self, regime: MarketRegime):
        """Set current market regime (from AI layer)."""
        self._current_regime = regime

    @abstractmethod
    async def evaluate(
        self,
        symbol: str,
        bars: List[Bar],
        context: MarketContext,
        account: AccountState,
        position: Optional[Position] = None,
    ) -> Optional[ProposedTrade]:
        """
        Evaluate strategy for a single symbol.

        Args:
            symbol: The trading symbol
            bars: Historical bars (newest last)
            context: Current market context
            account: Current account state
            position: Existing position if any

        Returns:
            ProposedTrade if signal generated, None otherwise
        """
        pass

    async def evaluate_all(
        self,
        bars_by_symbol: Union[Dict[str, List[Bar]], Dict[str, Dict[TimeFrame, List[Bar]]]],
        contexts: Dict[str, MarketContext],
        account: AccountState,
    ) -> List[ProposedTrade]:
        """
        Evaluate strategy for all configured symbols.

        Supports both old format (Dict[str, List[Bar]]) and
        new multi-timeframe format (Dict[str, Dict[TimeFrame, List[Bar]]]).

        Returns list of proposed trades.
        """
        if not self.is_enabled:
            return []

        proposals = []

        for symbol in self.symbols:
            if symbol not in bars_by_symbol or symbol not in contexts:
                continue

            # Extract bars - handle both old and new format
            symbol_bars = bars_by_symbol[symbol]
            if isinstance(symbol_bars, dict):
                # New format: {TimeFrame: [bars]} - use strategy's preferred timeframe
                primary_tf = getattr(self, 'primary_timeframe', TimeFrame.M5)
                bars = symbol_bars.get(primary_tf, symbol_bars.get(TimeFrame.M5, symbol_bars.get(TimeFrame.M15, [])))
            else:
                # Old format: [bars]
                bars = symbol_bars

            context = contexts[symbol]
            position = account.get_position(symbol)

            try:
                proposal = await self.evaluate(
                    symbol=symbol,
                    bars=bars,
                    context=context,
                    account=account,
                    position=position,
                )
                if proposal:
                    proposals.append(proposal)

            except Exception as e:
                # Log but don't crash
                import logging
                logging.getLogger(__name__).error(
                    f"Error evaluating {self.name} for {symbol}: {e}"
                )

        return proposals

    # -------------------------------------------------------------------------
    # Helper Methods
    # -------------------------------------------------------------------------
    def can_signal(self, symbol: str, min_interval_seconds: int = 60) -> bool:
        """Check if enough time has passed since last signal."""
        last_time = self._last_signal_time.get(symbol)
        if not last_time:
            return True

        elapsed = (datetime.now(timezone.utc) - last_time).total_seconds()
        return elapsed >= min_interval_seconds

    def record_signal(self, symbol: str):
        """Record that a signal was generated."""
        self._last_signal_time[symbol] = datetime.now(timezone.utc)

    def create_proposal(
        self,
        symbol: str,
        side: Side,
        context: MarketContext,
        confidence: Decimal = Decimal("0.5"),
        stop_loss_price: Optional[Decimal] = None,
        take_profit_price: Optional[Decimal] = None,
        reason: str = "",
    ) -> ProposedTrade:
        """Helper to create a ProposedTrade with common fields."""
        equity = Decimal("10000")  # Will be overridden by Risk Engine

        return ProposedTrade(
            strategy_id=self.strategy_id,
            symbol=symbol,
            side=side,
            notional_usd=equity * self.config.allocation_pct,
            risk_per_trade=equity * Decimal("0.02"),  # 2% default
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            confidence=confidence,
            reason=reason,
            market_context=context,
        )

    # -------------------------------------------------------------------------
    # Technical Analysis Helpers
    # -------------------------------------------------------------------------
    @staticmethod
    def calculate_atr(bars: List[Bar], period: int = 14) -> Decimal:
        """Calculate Average True Range."""
        if len(bars) < period + 1:
            return Decimal(0)

        true_ranges = []
        for i in range(1, len(bars)):
            high = bars[i].high
            low = bars[i].low
            prev_close = bars[i - 1].close

            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close),
            )
            true_ranges.append(tr)

        # Use last `period` TRs
        recent_trs = true_ranges[-period:]
        if not recent_trs:
            return Decimal(0)

        return sum(recent_trs) / Decimal(len(recent_trs))

    @staticmethod
    def calculate_ema(values: List[Decimal], period: int) -> List[Decimal]:
        """Calculate Exponential Moving Average."""
        if not values or period <= 0:
            return []

        multiplier = Decimal(2) / (Decimal(period) + 1)
        ema = [values[0]]

        for i in range(1, len(values)):
            new_ema = (values[i] * multiplier) + (ema[-1] * (1 - multiplier))
            ema.append(new_ema)

        return ema

    @staticmethod
    def calculate_sma(values: List[Decimal], period: int) -> List[Decimal]:
        """Calculate Simple Moving Average."""
        if len(values) < period:
            return []

        sma = []
        for i in range(period - 1, len(values)):
            window = values[i - period + 1 : i + 1]
            sma.append(sum(window) / Decimal(period))

        return sma

    @staticmethod
    def calculate_rsi(bars: List[Bar], period: int = 14) -> Decimal:
        """Calculate Relative Strength Index."""
        if len(bars) < period + 1:
            return Decimal(50)

        gains = []
        losses = []

        for i in range(1, len(bars)):
            change = bars[i].close - bars[i - 1].close
            if change > 0:
                gains.append(change)
                losses.append(Decimal(0))
            else:
                gains.append(Decimal(0))
                losses.append(abs(change))

        # Use last `period` values
        recent_gains = gains[-period:]
        recent_losses = losses[-period:]

        avg_gain = sum(recent_gains) / Decimal(period)
        avg_loss = sum(recent_losses) / Decimal(period)

        if avg_loss == 0:
            return Decimal(100)

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        return rsi

    @staticmethod
    def calculate_bollinger_bands(
        bars: List[Bar],
        period: int = 20,
        std_dev: Decimal = Decimal(2),
    ) -> tuple:
        """Calculate Bollinger Bands. Returns (upper, middle, lower, width)."""
        if len(bars) < period:
            return None, None, None, None

        closes = [b.close for b in bars[-period:]]
        middle = sum(closes) / Decimal(period)

        # Calculate standard deviation
        variance = sum((c - middle) ** 2 for c in closes) / Decimal(period)
        std = variance.sqrt() if hasattr(variance, 'sqrt') else Decimal(variance ** Decimal("0.5"))

        upper = middle + (std_dev * std)
        lower = middle - (std_dev * std)
        width = (upper - lower) / middle if middle > 0 else Decimal(0)

        return upper, middle, lower, width

    @staticmethod
    def find_swing_highs(bars: List[Bar], lookback: int = 5) -> List[Decimal]:
        """Find swing high levels."""
        highs = []
        for i in range(lookback, len(bars) - lookback):
            is_swing_high = True
            for j in range(1, lookback + 1):
                if bars[i].high <= bars[i - j].high or bars[i].high <= bars[i + j].high:
                    is_swing_high = False
                    break
            if is_swing_high:
                highs.append(bars[i].high)
        return highs

    @staticmethod
    def find_swing_lows(bars: List[Bar], lookback: int = 5) -> List[Decimal]:
        """Find swing low levels."""
        lows = []
        for i in range(lookback, len(bars) - lookback):
            is_swing_low = True
            for j in range(1, lookback + 1):
                if bars[i].low >= bars[i - j].low or bars[i].low >= bars[i + j].low:
                    is_swing_low = False
                    break
            if is_swing_low:
                lows.append(bars[i].low)
        return lows

    @staticmethod
    def calculate_volume_sma(bars: List[Bar], period: int = 20) -> Decimal:
        """Calculate volume SMA."""
        if len(bars) < period:
            return Decimal(0)

        volumes = [b.volume for b in bars[-period:]]
        return sum(volumes) / Decimal(period)
