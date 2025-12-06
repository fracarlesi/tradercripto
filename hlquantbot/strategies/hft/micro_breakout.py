"""Micro-Breakout Strategy (multi-timeframe)."""

import logging
from decimal import Decimal
from typing import List, Optional, Tuple, Dict

from ...core.models import (
    ProposedTrade,
    AccountState,
    Bar,
    MarketContext,
    Position,
)
from ...core.enums import StrategyId, Side, TimeFrame
from ...config.settings import Settings
from .hft_base import HFTBaseStrategy


logger = logging.getLogger(__name__)


class MicroBreakoutStrategy(HFTBaseStrategy):
    """
    Micro-Breakout HFT Strategy (Multi-Timeframe).

    Logic:
    - Detect price consolidation (low range compression)
    - Wait for breakout with volume confirmation
    - Enter in breakout direction
    - Parameters adjust based on timeframe (M1, M5, M15)

    Timeframe-specific behavior:
    - M1: Tight ranges (0.05%), fast breakouts
    - M5: Medium ranges (0.2%), balanced
    - M15: Wider ranges (0.5%), swing-like

    Ideal conditions:
    - After consolidation periods
    - High volume on breakout
    - Clear support/resistance levels
    """

    def __init__(self, settings: Settings):
        super().__init__(settings, StrategyId.MICRO_BREAKOUT)

        # Get primary timeframe from config
        self.primary_timeframe = self._get_primary_timeframe()

        # Load timeframe-specific parameters
        self._tf_params = self._load_timeframe_params()

        # Set active parameters based on primary timeframe
        params = self._tf_params.get(self.primary_timeframe, self._tf_params.get(TimeFrame.M5))
        self.consolidation_bars = params['consolidation_bars']
        self.range_threshold_pct = params['range_threshold_pct']
        self.breakout_threshold_pct = params['breakout_threshold_pct']
        self.volume_surge_multiplier = self._get_param('volume_surge_multiplier', Decimal("1.5"))

        # Tracking
        self._consolidation_ranges: dict = {}  # symbol -> (low, high)

        logger.info(
            f"Micro-Breakout initialized with timeframe {self.primary_timeframe.value}: "
            f"range_threshold={self.range_threshold_pct:.4%}, breakout={self.breakout_threshold_pct:.4%}"
        )

    def _get_primary_timeframe(self) -> TimeFrame:
        """Get primary timeframe from config."""
        if self._hft_config:
            tf_str = getattr(self._hft_config, 'primary_timeframe', 'M5')
            try:
                # Handle both "M5" and "5m" formats
                if tf_str.upper().startswith('M'):
                    return TimeFrame(tf_str.lower().replace('m', '') + 'm')
                return TimeFrame(tf_str)
            except ValueError:
                pass
        return TimeFrame.M5

    def _load_timeframe_params(self) -> Dict[TimeFrame, dict]:
        """Load parameters for each timeframe."""
        params = {}

        # Default parameters per timeframe
        defaults = {
            TimeFrame.M1: {
                'range_threshold_pct': Decimal("0.0005"),
                'breakout_threshold_pct': Decimal("0.0003"),
                'take_profit_pct': Decimal("0.0008"),
                'stop_loss_pct': Decimal("0.001"),
                'consolidation_bars': 15,
            },
            TimeFrame.M5: {
                'range_threshold_pct': Decimal("0.002"),
                'breakout_threshold_pct': Decimal("0.001"),
                'take_profit_pct': Decimal("0.003"),
                'stop_loss_pct': Decimal("0.004"),
                'consolidation_bars': 10,
            },
            TimeFrame.M15: {
                'range_threshold_pct': Decimal("0.005"),
                'breakout_threshold_pct': Decimal("0.002"),
                'take_profit_pct': Decimal("0.006"),
                'stop_loss_pct': Decimal("0.008"),
                'consolidation_bars': 8,
            },
        }

        # First check for flat config values (top-level in micro_breakout config)
        # This takes priority over nested timeframe configs
        if self._hft_config:
            flat_range = getattr(self._hft_config, 'range_threshold_pct', None)
            flat_breakout = getattr(self._hft_config, 'breakout_threshold_pct', None)
            flat_tp = getattr(self._hft_config, 'take_profit_pct', None)
            flat_sl = getattr(self._hft_config, 'stop_loss_pct', None)
            flat_bars = getattr(self._hft_config, 'consolidation_bars', None)

            if flat_range is not None or flat_breakout is not None:
                # Use flat config for all timeframes
                for tf in [TimeFrame.M1, TimeFrame.M5, TimeFrame.M15]:
                    params[tf] = {
                        'range_threshold_pct': Decimal(str(flat_range)) if flat_range is not None else defaults[tf]['range_threshold_pct'],
                        'breakout_threshold_pct': Decimal(str(flat_breakout)) if flat_breakout is not None else defaults[tf]['breakout_threshold_pct'],
                        'take_profit_pct': Decimal(str(flat_tp)) if flat_tp is not None else defaults[tf]['take_profit_pct'],
                        'stop_loss_pct': Decimal(str(flat_sl)) if flat_sl is not None else defaults[tf]['stop_loss_pct'],
                        'consolidation_bars': int(flat_bars) if flat_bars is not None else defaults[tf]['consolidation_bars'],
                    }
                logger.info(
                    f"Micro-Breakout: Using flat config values - range={flat_range}, breakout={flat_breakout}"
                )
                return params

        # Try to load from nested timeframe config
        if self._hft_config:
            for tf, attr_name in [(TimeFrame.M1, 'timeframe_m1'),
                                  (TimeFrame.M5, 'timeframe_m5'),
                                  (TimeFrame.M15, 'timeframe_m15')]:
                tf_config = getattr(self._hft_config, attr_name, None)
                if tf_config:
                    params[tf] = {
                        'range_threshold_pct': Decimal(str(getattr(tf_config, 'range_threshold_pct', defaults[tf]['range_threshold_pct']))),
                        'breakout_threshold_pct': Decimal(str(getattr(tf_config, 'breakout_threshold_pct', defaults[tf]['breakout_threshold_pct']))),
                        'take_profit_pct': Decimal(str(getattr(tf_config, 'take_profit_pct', defaults[tf]['take_profit_pct']))),
                        'stop_loss_pct': Decimal(str(getattr(tf_config, 'stop_loss_pct', defaults[tf]['stop_loss_pct']))),
                        'consolidation_bars': int(getattr(tf_config, 'consolidation_bars', defaults[tf]['consolidation_bars'])),
                    }
                else:
                    params[tf] = defaults[tf]
        else:
            params = defaults

        return params

    def _get_param(self, name: str, default: Decimal) -> Decimal:
        """Get parameter from config or use default."""
        if self._hft_config:
            return Decimal(str(getattr(self._hft_config, name, default)))
        return default

    def _get_param_int(self, name: str, default: int) -> int:
        """Get integer parameter from config or use default."""
        if self._hft_config:
            return int(getattr(self._hft_config, name, default))
        return default

    async def evaluate(
        self,
        symbol: str,
        bars: List[Bar],
        context: MarketContext,
        account: AccountState,
        position: Optional[Position] = None,
    ) -> Optional[ProposedTrade]:
        """
        Evaluate Micro-Breakout strategy.

        Phase 1: Detect consolidation
        - Range over N bars < threshold
        - Store consolidation range

        Phase 2: Detect breakout
        - Price breaks above/below consolidation range
        - Volume confirms breakout
        - Enter in breakout direction
        """
        # Need enough bars
        if len(bars) < self.consolidation_bars + 2:
            return None

        # Check signal cooldown
        if not self.can_signal_hft(symbol):
            return None

        # Skip if we have a position
        if position:
            if self.should_close_for_timeout(symbol):
                logger.info(f"Micro-Breakout: {symbol} position timeout")
                return self._create_close_proposal(symbol, position, context)
            return None

        # Get current price
        current_price = context.current_price
        if not current_price or current_price == 0:
            return None

        # Check for consolidation
        consolidation = self._detect_consolidation(bars, symbol)
        if consolidation:
            # We're in consolidation - check for breakout
            cons_low, cons_high = consolidation
            return self._check_breakout(
                symbol, bars, current_price, cons_low, cons_high, context
            )

        return None

    def _detect_consolidation(
        self,
        bars: List[Bar],
        symbol: str,
    ) -> Optional[Tuple[Decimal, Decimal]]:
        """
        Detect if we're in a consolidation pattern.

        Returns (low, high) of consolidation range if detected.
        """
        # Use consolidation_bars for range detection
        recent_bars = bars[-self.consolidation_bars:]

        range_high = max(b.high for b in recent_bars)
        range_low = min(b.low for b in recent_bars)

        # Check if range is tight enough
        range_pct = (range_high - range_low) / range_low if range_low > 0 else Decimal(0)

        # Log periodically for debugging (every ~30 calls per symbol)
        if hash(f"{symbol}{int(bars[-1].timestamp.timestamp()) // 30}") % 30 == 0:
            logger.info(
                f"Micro-Breakout Check [{self.primary_timeframe.value}]: {symbol} | "
                f"Range: {range_pct:.4%} | Threshold: {self.range_threshold_pct:.4%}"
            )

        if range_pct <= self.range_threshold_pct:
            # Store consolidation range
            self._consolidation_ranges[symbol] = (range_low, range_high)
            return (range_low, range_high)

        # Check if we have a stored consolidation that's still valid
        stored = self._consolidation_ranges.get(symbol)
        if stored:
            stored_low, stored_high = stored
            current_close = bars[-1].close

            # If price is still within stored range, keep it
            if stored_low <= current_close <= stored_high:
                return stored

            # Range broken - will check for breakout
            return stored

        return None

    def _check_breakout(
        self,
        symbol: str,
        bars: List[Bar],
        current_price: Decimal,
        cons_low: Decimal,
        cons_high: Decimal,
        context: MarketContext,
    ) -> Optional[ProposedTrade]:
        """Check if price has broken out of consolidation range."""
        # Calculate breakout thresholds
        range_size = cons_high - cons_low
        breakout_amount = (cons_high + cons_low) / 2 * self.breakout_threshold_pct

        upside_breakout = cons_high + breakout_amount
        downside_breakout = cons_low - breakout_amount

        # Check for breakout
        side = None
        reason = ""

        if current_price > upside_breakout:
            side = Side.LONG
            reason = f"Upside breakout: {current_price} > {upside_breakout:.2f}"
        elif current_price < downside_breakout:
            side = Side.SHORT
            reason = f"Downside breakout: {current_price} < {downside_breakout:.2f}"

        if not side:
            return None

        # Volume confirmation
        if not self._check_volume_surge(bars):
            logger.debug(f"Micro-Breakout: {symbol} breakout without volume, skipping")
            return None

        # Clear stored consolidation
        if symbol in self._consolidation_ranges:
            del self._consolidation_ranges[symbol]

        # Calculate confidence based on breakout strength
        if side == Side.LONG:
            breakout_strength = (current_price - cons_high) / range_size
        else:
            breakout_strength = (cons_low - current_price) / range_size

        confidence = min(Decimal("0.8"), Decimal("0.5") + breakout_strength * Decimal("0.3"))

        logger.info(
            f"Micro-Breakout Signal: {symbol} {side.value} | "
            f"Price: {current_price} | Range: [{cons_low:.2f}, {cons_high:.2f}] | "
            f"Strength: {breakout_strength:.2%}"
        )

        return self.create_hft_proposal(
            symbol=symbol,
            side=side,
            entry_price=current_price,
            context=context,
            confidence=confidence,
            reason=reason,
        )

    def _check_volume_surge(self, bars: List[Bar]) -> bool:
        """Check if current bar has volume surge."""
        if len(bars) < 5:
            return True  # Not enough data, allow trade

        current_vol = bars[-1].volume
        avg_vol = sum(b.volume for b in bars[-6:-1]) / 5

        if avg_vol == 0:
            return current_vol > 0

        return current_vol >= avg_vol * self.volume_surge_multiplier

    def _create_close_proposal(
        self,
        symbol: str,
        position: Position,
        context: MarketContext,
    ) -> ProposedTrade:
        """Create a proposal to close position."""
        close_side = Side.SHORT if position.side == Side.LONG else Side.LONG

        return ProposedTrade(
            strategy_id=self.strategy_id,
            symbol=symbol,
            side=close_side,
            notional_usd=position.notional_value,
            risk_per_trade=Decimal(0),
            confidence=Decimal("1.0"),
            reason="[Micro-Breakout] Position timeout - closing",
            market_context=context,
        )
