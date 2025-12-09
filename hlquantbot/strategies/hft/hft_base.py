"""Base class for HFT strategies with fee-aware logic."""

import logging
from abc import abstractmethod
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Dict, Tuple

from ...core.models import (
    ProposedTrade,
    AccountState,
    Bar,
    MarketContext,
    Position,
)
from ...core.enums import StrategyId, Side, MarketRegime, TimeFrame, OrderType
from ...config.settings import Settings
from ..base import BaseStrategy


logger = logging.getLogger(__name__)


# Fee constants (Hyperliquid)
MAKER_FEE_PCT = Decimal("0.0002")  # 0.02%
TAKER_FEE_PCT = Decimal("0.0005")  # 0.05%

# Minimum TP to be profitable after fees (maker entry + maker exit)
MIN_PROFITABLE_TP_PCT = MAKER_FEE_PCT * 2 + Decimal("0.0001")  # 0.05%


class HFTBaseStrategy(BaseStrategy):
    """
    Base class for all HFT strategies.

    Key differences from standard strategies:
    - Sub-second timeframes (1s, 5s, 15s)
    - Fee-aware TP/SL calculation
    - Maker-only order execution
    - Shorter position hold times
    - Signal cooldown in milliseconds
    - Higher leverage with smaller position sizes
    """

    def __init__(self, settings: Settings, strategy_id: StrategyId):
        super().__init__(settings, strategy_id)

        # HFT-specific config
        self._hft_config = self._get_hft_config()

        # Signal tracking (millisecond precision)
        self._last_signal_ms: Dict[str, int] = {}

        # Position tracking
        self._position_open_time: Dict[str, datetime] = {}

        # Performance tracking
        self._signal_count = 0
        self._fill_count = 0
        self._timeout_count = 0

    def _get_hft_config(self) -> Optional[object]:
        """Get HFT-specific configuration."""
        hft_strategies = getattr(self.settings.strategies, 'hft', None)
        if not hft_strategies:
            return None

        strategy_map = {
            StrategyId.MMR_HFT: 'mmr_hft',
            StrategyId.MICRO_BREAKOUT: 'micro_breakout',
            StrategyId.PAIR_TRADING: 'pair_trading',
            StrategyId.LIQUIDATION_SNIPING: 'liquidation_sniping',
            StrategyId.MOMENTUM_SCALPING: 'momentum_scalping',
        }

        config_name = strategy_map.get(self.strategy_id)
        if config_name:
            return getattr(hft_strategies, config_name, None)
        return None

    @property
    def timeframe_seconds(self) -> int:
        """Get the primary timeframe in seconds."""
        if self._hft_config:
            return getattr(self._hft_config, 'timeframe_seconds', 5)
        return 5

    @property
    def order_timeout_seconds(self) -> float:
        """Get order timeout in seconds."""
        if self._hft_config:
            return getattr(self._hft_config, 'order_timeout_seconds', 2)
        return 2.0

    @property
    def max_position_hold_seconds(self) -> int:
        """Get max position hold time in seconds."""
        if self._hft_config:
            return getattr(self._hft_config, 'max_position_hold_seconds', 60)
        return 60

    @property
    def min_signal_interval_ms(self) -> int:
        """Get minimum interval between signals in milliseconds."""
        if self._hft_config:
            return getattr(self._hft_config, 'min_signal_interval_ms', 100)
        return 100

    @property
    def default_leverage(self) -> Decimal:
        """Get default leverage for this strategy."""
        if self._hft_config:
            return Decimal(str(getattr(self._hft_config, 'default_leverage', 10)))
        return Decimal("10")

    @property
    def take_profit_pct(self) -> Decimal:
        """Get take profit percentage."""
        if self._hft_config:
            return Decimal(str(getattr(self._hft_config, 'take_profit_pct', 0.001)))
        return Decimal("0.001")

    @property
    def stop_loss_pct(self) -> Decimal:
        """Get stop loss percentage."""
        if self._hft_config:
            return Decimal(str(getattr(self._hft_config, 'stop_loss_pct', 0.002)))
        return Decimal("0.002")

    # -------------------------------------------------------------------------
    # Dynamic TP/SL based on ATR
    # -------------------------------------------------------------------------
    def calculate_dynamic_tp_sl(
        self,
        entry_price: Decimal,
        context: MarketContext,
    ) -> Tuple[Decimal, Decimal]:
        """
        Calculate dynamic TP/SL based on ATR if available.

        Uses ATR to scale TP/SL appropriately for current volatility:
        - High volatility: wider TP/SL to avoid premature stops
        - Low volatility: tighter TP/SL for faster exits

        Returns:
            Tuple of (take_profit_pct, stop_loss_pct)
        """
        # Check if ATR is available in context
        if context.atr_14 and entry_price > 0:
            atr_pct = context.atr_14 / entry_price

            # Clamp ATR between reasonable bounds
            MIN_ATR_PCT = Decimal("0.002")  # 0.2%
            MAX_ATR_PCT = Decimal("0.010")  # 1.0%
            atr_pct = max(MIN_ATR_PCT, min(atr_pct, MAX_ATR_PCT))

            # TP = 1.2x ATR, SL = 0.6x ATR (RR = 2.0)
            dynamic_tp = atr_pct * Decimal("1.2")
            dynamic_sl = atr_pct * Decimal("0.6")

            # Ensure minimum TP for profitability after fees (0.04% roundtrip)
            MIN_TP = Decimal("0.0035")  # 0.35% min
            MAX_SL = Decimal("0.0020")  # 0.20% max

            # Apply constraints
            final_tp = max(dynamic_tp, MIN_TP)
            final_sl = min(dynamic_sl, MAX_SL)

            logger.debug(
                f"Dynamic TP/SL for {context.symbol}: "
                f"ATR={float(atr_pct):.4%} -> TP={float(final_tp):.4%}, SL={float(final_sl):.4%}"
            )

            return final_tp, final_sl

        # Fallback to static config values
        return self.take_profit_pct, self.stop_loss_pct

    # -------------------------------------------------------------------------
    # Fee-Aware Calculations
    # -------------------------------------------------------------------------
    def calculate_net_tp(
        self,
        entry_price: Decimal,
        gross_tp_pct: Decimal,
        side: Side,
    ) -> Decimal:
        """
        Calculate take profit price accounting for fees.

        For HFT to be profitable:
        - Entry fee (maker): 0.02%
        - Exit fee (maker): 0.02%
        - Net TP must exceed total fees (0.04%)
        """
        # Total fees for round-trip (entry + exit)
        total_fee_pct = MAKER_FEE_PCT * 2

        # Gross TP must exceed fees
        if gross_tp_pct <= total_fee_pct:
            logger.warning(
                f"Gross TP {gross_tp_pct:.4%} <= fees {total_fee_pct:.4%}, "
                f"adjusting to minimum profitable"
            )
            gross_tp_pct = MIN_PROFITABLE_TP_PCT

        if side == Side.LONG:
            return entry_price * (1 + gross_tp_pct)
        else:
            return entry_price * (1 - gross_tp_pct)

    def calculate_sl(
        self,
        entry_price: Decimal,
        sl_pct: Decimal,
        side: Side,
    ) -> Decimal:
        """Calculate stop loss price."""
        if side == Side.LONG:
            return entry_price * (1 - sl_pct)
        else:
            return entry_price * (1 + sl_pct)

    def is_profitable_after_fees(
        self,
        entry_price: Decimal,
        exit_price: Decimal,
        side: Side,
    ) -> bool:
        """Check if a trade would be profitable after fees."""
        if side == Side.LONG:
            gross_pnl_pct = (exit_price - entry_price) / entry_price
        else:
            gross_pnl_pct = (entry_price - exit_price) / entry_price

        total_fee_pct = MAKER_FEE_PCT * 2
        return gross_pnl_pct > total_fee_pct

    # -------------------------------------------------------------------------
    # Signal Management
    # -------------------------------------------------------------------------
    def can_signal_hft(self, symbol: str) -> bool:
        """Check if enough time has passed since last signal (millisecond precision)."""
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        last_ms = self._last_signal_ms.get(symbol, 0)

        elapsed_ms = now_ms - last_ms
        return elapsed_ms >= self.min_signal_interval_ms

    def record_signal_hft(self, symbol: str):
        """Record signal time in milliseconds."""
        self._last_signal_ms[symbol] = int(datetime.now(timezone.utc).timestamp() * 1000)
        self._signal_count += 1

    # -------------------------------------------------------------------------
    # Position Management
    # -------------------------------------------------------------------------
    def should_close_for_timeout(self, symbol: str) -> bool:
        """Check if position should be closed due to hold time limit."""
        open_time = self._position_open_time.get(symbol)
        if not open_time:
            return False

        elapsed = (datetime.now(timezone.utc) - open_time).total_seconds()
        return elapsed >= self.max_position_hold_seconds

    def record_position_open(self, symbol: str):
        """Record when a position was opened."""
        self._position_open_time[symbol] = datetime.now(timezone.utc)

    def record_position_close(self, symbol: str):
        """Record when a position was closed."""
        if symbol in self._position_open_time:
            del self._position_open_time[symbol]

    # -------------------------------------------------------------------------
    # HFT-Specific Proposal Creation
    # -------------------------------------------------------------------------
    def create_hft_proposal(
        self,
        symbol: str,
        side: Side,
        entry_price: Decimal,
        context: MarketContext,
        confidence: Decimal = Decimal("0.6"),
        reason: str = "",
    ) -> ProposedTrade:
        """
        Create an HFT trade proposal with dynamic ATR-based TP/SL.

        All HFT trades use:
        - Maker-only (post-only) orders
        - Fee-aware TP calculation
        - Dynamic TP/SL based on ATR (if available)
        - High leverage
        """
        # Calculate dynamic TP/SL based on ATR (or fallback to static)
        tp_pct, sl_pct = self.calculate_dynamic_tp_sl(entry_price, context)

        # Calculate actual prices with fees
        tp_price = self.calculate_net_tp(entry_price, tp_pct, side)
        sl_price = self.calculate_sl(entry_price, sl_pct, side)

        # Validate profitability
        if not self.is_profitable_after_fees(entry_price, tp_price, side):
            logger.warning(f"HFT proposal for {symbol} would not be profitable after fees")
            return None

        # Record signal
        self.record_signal_hft(symbol)

        # Get allocation
        allocation_pct = Decimal("0.01")  # Default 1%
        if self._hft_config:
            allocation_pct = Decimal(str(getattr(self._hft_config, 'max_position_pct', 0.01)))

        return ProposedTrade(
            strategy_id=self.strategy_id,
            symbol=symbol,
            side=side,
            notional_usd=Decimal("1000"),  # Will be overridden by position sizer
            risk_per_trade=Decimal("70"),  # 0.7% of $10k = $70
            entry_type=OrderType.LIMIT_GTX,  # Post-only maker order
            entry_price=entry_price,
            stop_loss_price=sl_price,
            take_profit_price=tp_price,
            confidence=confidence,
            reason=f"[HFT-{self.strategy_id.value}] {reason}",
            market_context=context,
        )

    # -------------------------------------------------------------------------
    # Technical Indicators (Optimized for HFT)
    # -------------------------------------------------------------------------
    @staticmethod
    def calculate_vwap(bars: List[Bar]) -> Optional[Decimal]:
        """Calculate VWAP from bars."""
        if not bars:
            return None

        total_volume = Decimal(0)
        total_value = Decimal(0)

        for bar in bars:
            typical_price = (bar.high + bar.low + bar.close) / 3
            total_value += typical_price * bar.volume
            total_volume += bar.volume

        if total_volume == 0:
            return sum(b.close for b in bars) / len(bars)

        return total_value / total_volume

    @staticmethod
    def calculate_micro_range(bars: List[Bar]) -> Tuple[Decimal, Decimal]:
        """Calculate high/low range over bars."""
        if not bars:
            return Decimal(0), Decimal(0)

        high = max(b.high for b in bars)
        low = min(b.low for b in bars)
        return low, high

    @staticmethod
    def calculate_range_compression(bars: List[Bar]) -> Decimal:
        """
        Calculate range compression ratio.

        Lower values = more compressed (potential breakout).
        """
        if len(bars) < 2:
            return Decimal(1)

        recent_range = bars[-1].high - bars[-1].low
        avg_range = sum(b.high - b.low for b in bars) / len(bars)

        if avg_range == 0:
            return Decimal(1)

        return recent_range / avg_range

    @staticmethod
    def calculate_momentum(bars: List[Bar], periods: int = 5) -> Decimal:
        """Calculate short-term momentum."""
        if len(bars) < periods:
            return Decimal(0)

        return bars[-1].close - bars[-periods].close

    @staticmethod
    def calculate_volume_spike(bars: List[Bar], threshold: Decimal = Decimal("1.5")) -> bool:
        """Check if current volume is a spike compared to average."""
        if len(bars) < 2:
            return False

        current_vol = bars[-1].volume
        avg_vol = sum(b.volume for b in bars[:-1]) / (len(bars) - 1) if len(bars) > 1 else Decimal(0)

        if avg_vol == 0:
            return False

        return current_vol >= avg_vol * threshold

    # -------------------------------------------------------------------------
    # Abstract Method
    # -------------------------------------------------------------------------
    def get_bars_for_timeframe(
        self,
        bars_input: any,
        symbol: str,
    ) -> List[Bar]:
        """
        Extract bars for the strategy's primary timeframe.

        Handles both old format (Dict[str, List[Bar]]) and
        new format (Dict[str, Dict[TimeFrame, List[Bar]]]).
        """
        if not bars_input:
            return []

        # Check if it's a list (direct bars for symbol)
        if isinstance(bars_input, list):
            return bars_input

        # Check if it's a dict with TimeFrame keys (new format per-symbol)
        if isinstance(bars_input, dict):
            first_key = next(iter(bars_input.keys()), None)
            if isinstance(first_key, TimeFrame):
                # New format: {TimeFrame: [bars]}
                primary_tf = getattr(self, 'primary_timeframe', TimeFrame.M5)
                return bars_input.get(primary_tf, bars_input.get(TimeFrame.M5, []))
            elif isinstance(first_key, str):
                # Old format with symbol keys: {symbol: [bars]}
                return bars_input.get(symbol, [])

        return []

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
        Evaluate HFT strategy for a single symbol.

        Must be implemented by subclasses.
        """
        pass

    # -------------------------------------------------------------------------
    # Metrics
    # -------------------------------------------------------------------------
    def get_hft_metrics(self) -> Dict:
        """Get HFT-specific performance metrics."""
        return {
            "strategy_id": self.strategy_id.value,
            "signal_count": self._signal_count,
            "fill_count": self._fill_count,
            "timeout_count": self._timeout_count,
            "fill_rate": self._fill_count / self._signal_count if self._signal_count > 0 else 0,
            "active_positions": len(self._position_open_time),
            "config": {
                "timeframe_seconds": self.timeframe_seconds,
                "order_timeout_seconds": self.order_timeout_seconds,
                "max_hold_seconds": self.max_position_hold_seconds,
                "min_signal_interval_ms": self.min_signal_interval_ms,
                "leverage": float(self.default_leverage),
                "take_profit_pct": float(self.take_profit_pct),
                "stop_loss_pct": float(self.stop_loss_pct),
            }
        }
