"""MMR-HFT: Micro Mean Reversion Strategy (multi-timeframe)."""

import logging
from decimal import Decimal
from typing import List, Optional, Dict

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


class MMRHFTStrategy(HFTBaseStrategy):
    """
    Micro Mean Reversion HFT Strategy (Multi-Timeframe).

    Logic:
    - Monitor price deviation from short-term VWAP
    - When price deviates beyond threshold, expect reversion
    - Enter against the deviation, exit at VWAP or TP
    - Parameters adjust based on timeframe (M1, M5, M15)

    Timeframe-specific behavior:
    - M1: Tighter thresholds (0.03%-0.1%), faster signals
    - M5: Medium thresholds (0.1%-0.3%), balanced
    - M15: Wider thresholds (0.3%-0.8%), swing-like

    Ideal conditions:
    - Range-bound markets
    - High liquidity
    - Low to medium volatility
    """

    def __init__(self, settings: Settings):
        super().__init__(settings, StrategyId.MMR_HFT)

        # Get primary timeframe from config
        self.primary_timeframe = self._get_primary_timeframe()

        # Load timeframe-specific parameters
        self._tf_params = self._load_timeframe_params()

        # Set active parameters based on primary timeframe
        params = self._tf_params.get(self.primary_timeframe, self._tf_params.get(TimeFrame.M5))
        self.deviation_threshold_pct = params['deviation_threshold_pct']
        self.max_deviation_pct = params['max_deviation_pct']
        self.vwap_periods = params['vwap_periods']

        logger.info(
            f"MMR-HFT initialized with timeframe {self.primary_timeframe.value}: "
            f"deviation={self.deviation_threshold_pct:.4%}, max={self.max_deviation_pct:.4%}"
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

        # Default parameters per timeframe - AGGRESSIVE for HFT
        # Lowered thresholds to generate more signals (200-400 trades/day target)
        defaults = {
            TimeFrame.M1: {
                'deviation_threshold_pct': Decimal("0.0002"),  # 0.02% (was 0.03%)
                'max_deviation_pct': Decimal("0.002"),         # 0.2% (was 0.1%)
                'take_profit_pct': Decimal("0.0004"),
                'stop_loss_pct': Decimal("0.0008"),
                'vwap_periods': 20,
            },
            TimeFrame.M5: {
                'deviation_threshold_pct': Decimal("0.0005"),  # 0.05% (was 0.1%)
                'max_deviation_pct': Decimal("0.005"),         # 0.5% (was 0.3%)
                'take_profit_pct': Decimal("0.001"),
                'stop_loss_pct': Decimal("0.002"),
                'vwap_periods': 15,
            },
            TimeFrame.M15: {
                'deviation_threshold_pct': Decimal("0.001"),   # 0.1% (was 0.3%)
                'max_deviation_pct': Decimal("0.01"),          # 1% (was 0.8%)
                'take_profit_pct': Decimal("0.003"),
                'stop_loss_pct': Decimal("0.004"),
                'vwap_periods': 12,
            },
        }

        # First check for flat config values (top-level in mmr_hft config)
        # This takes priority over nested timeframe configs
        if self._hft_config:
            flat_deviation = getattr(self._hft_config, 'deviation_threshold_pct', None)
            flat_max_dev = getattr(self._hft_config, 'max_deviation_pct', None)
            flat_tp = getattr(self._hft_config, 'take_profit_pct', None)
            flat_sl = getattr(self._hft_config, 'stop_loss_pct', None)

            if flat_deviation is not None or flat_max_dev is not None:
                # Use flat config for all timeframes
                for tf in [TimeFrame.M1, TimeFrame.M5, TimeFrame.M15]:
                    params[tf] = {
                        'deviation_threshold_pct': Decimal(str(flat_deviation)) if flat_deviation is not None else defaults[tf]['deviation_threshold_pct'],
                        'max_deviation_pct': Decimal(str(flat_max_dev)) if flat_max_dev is not None else defaults[tf]['max_deviation_pct'],
                        'take_profit_pct': Decimal(str(flat_tp)) if flat_tp is not None else defaults[tf]['take_profit_pct'],
                        'stop_loss_pct': Decimal(str(flat_sl)) if flat_sl is not None else defaults[tf]['stop_loss_pct'],
                        'vwap_periods': defaults[tf]['vwap_periods'],
                    }
                logger.info(
                    f"MMR-HFT: Using flat config values - deviation={flat_deviation}, max={flat_max_dev}"
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
                        'deviation_threshold_pct': Decimal(str(getattr(tf_config, 'deviation_threshold_pct', defaults[tf]['deviation_threshold_pct']))),
                        'max_deviation_pct': Decimal(str(getattr(tf_config, 'max_deviation_pct', defaults[tf]['max_deviation_pct']))),
                        'take_profit_pct': Decimal(str(getattr(tf_config, 'take_profit_pct', defaults[tf]['take_profit_pct']))),
                        'stop_loss_pct': Decimal(str(getattr(tf_config, 'stop_loss_pct', defaults[tf]['stop_loss_pct']))),
                        'vwap_periods': int(getattr(tf_config, 'vwap_periods', defaults[tf]['vwap_periods'])),
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

    async def evaluate(
        self,
        symbol: str,
        bars: List[Bar],
        context: MarketContext,
        account: AccountState,
        position: Optional[Position] = None,
    ) -> Optional[ProposedTrade]:
        """
        Evaluate MMR strategy.

        Entry conditions:
        1. Price deviates from VWAP by > deviation_threshold
        2. Deviation < max_deviation (avoid catching falling knife)
        3. No existing position
        4. Signal cooldown passed

        Exit conditions (handled by SL/TP):
        1. Price returns to VWAP (TP)
        2. Price continues away from VWAP (SL)
        """
        logger.info(f"MMR-HFT EVAL: {symbol} bars={len(bars)}")

        # Need enough bars for VWAP
        if len(bars) < self.vwap_periods:
            logger.info(f"MMR-HFT: {symbol} not enough bars ({len(bars)} < {self.vwap_periods})")
            return None

        # Check signal cooldown
        if not self.can_signal_hft(symbol):
            logger.info(f"MMR-HFT: {symbol} cooldown active")
            return None

        # Skip if we have a position (let it hit TP/SL)
        if position:
            logger.info(f"MMR-HFT: {symbol} has position, skipping")
            # Check for hold timeout
            if self.should_close_for_timeout(symbol):
                logger.info(f"MMR-HFT: {symbol} position timeout, signaling close")
                return self._create_close_proposal(symbol, position, context)
            return None

        # Calculate VWAP
        recent_bars = bars[-self.vwap_periods:]
        vwap = self.calculate_vwap(recent_bars)
        if not vwap or vwap == 0:
            logger.info(f"MMR-HFT: {symbol} VWAP is None/0")
            return None

        # Get current price (mid)
        current_price = context.current_price
        if not current_price or current_price == 0:
            logger.info(f"MMR-HFT: {symbol} current_price is None/0")
            return None

        # Log deviation check
        deviation_pct = (current_price - vwap) / vwap
        logger.info(f"MMR-HFT Check: {symbol} | Dev: {deviation_pct:.6%} | Threshold: {self.deviation_threshold_pct:.6%}-{self.max_deviation_pct:.6%}")

        # Calculate deviation from VWAP
        deviation_pct = (current_price - vwap) / vwap

        # Check if deviation is significant but not too extreme
        abs_deviation = abs(deviation_pct)

        # Log every check for debugging (more frequent to see what's happening)
        logger.info(
            f"MMR-HFT Check [{self.primary_timeframe.value}]: {symbol} | "
            f"Price: {current_price:.2f} | VWAP: {vwap:.2f} | "
            f"Dev: {deviation_pct:.4%} | Threshold: {self.deviation_threshold_pct:.4%}-{self.max_deviation_pct:.4%}"
        )

        if abs_deviation < self.deviation_threshold_pct:
            # Not enough deviation
            return None

        if abs_deviation > self.max_deviation_pct:
            # Too extreme - might be trending, skip
            logger.debug(
                f"MMR-HFT: {symbol} deviation {deviation_pct:.4%} exceeds max, skipping"
            )
            return None

        # Determine direction (mean reversion = opposite of deviation)
        if deviation_pct > 0:
            # Price above VWAP -> expect reversion down -> SHORT
            side = Side.SHORT
            reason = f"Price {deviation_pct:.4%} above VWAP, expecting reversion"
        else:
            # Price below VWAP -> expect reversion up -> LONG
            side = Side.LONG
            reason = f"Price {abs_deviation:.4%} below VWAP, expecting reversion"

        # Calculate confidence based on deviation magnitude
        # More deviation = higher confidence (up to a point)
        confidence = min(
            Decimal("0.8"),
            Decimal("0.5") + (abs_deviation / self.deviation_threshold_pct) * Decimal("0.1")
        )

        # Additional filters
        if not self._passes_filters(bars, context, side):
            return None

        logger.info(
            f"MMR-HFT Signal: {symbol} {side.value} | "
            f"Price: {current_price} | VWAP: {vwap:.2f} | "
            f"Deviation: {deviation_pct:.4%} | Confidence: {confidence:.2f}"
        )

        # Create proposal
        return self.create_hft_proposal(
            symbol=symbol,
            side=side,
            entry_price=current_price,
            context=context,
            confidence=confidence,
            reason=reason,
        )

    def _passes_filters(
        self,
        bars: List[Bar],
        context: MarketContext,
        side: Side,
    ) -> bool:
        """Apply additional filters before signal."""
        # Filter 1: Volume check - need minimum activity
        if len(bars) < 5:
            return True

        recent_volume = sum(b.volume for b in bars[-5:])
        if recent_volume == 0:
            logger.debug("MMR-HFT: Zero volume, skipping")
            return False

        # Filter 2: Avoid during high volatility spikes
        if len(bars) >= 10:
            recent_range = max(b.high for b in bars[-10:]) - min(b.low for b in bars[-10:])
            avg_bar_range = sum(b.high - b.low for b in bars[-10:]) / 10

            if avg_bar_range > 0:
                range_expansion = recent_range / (avg_bar_range * 10)
                if range_expansion > Decimal("2.0"):
                    logger.debug(f"MMR-HFT: Range expansion {range_expansion:.2f}, skipping")
                    return False

        # Filter 3: Check spread (from context) - relaxed for testnet
        if context.spread:
            spread_pct = context.spread / context.current_price
            # Skip if spread is too wide (>0.15% - relaxed from 0.05%)
            if spread_pct > Decimal("0.0015"):
                logger.debug(f"MMR-HFT: Spread too wide {spread_pct:.4%}, skipping")
                return False

        return True

    def _create_close_proposal(
        self,
        symbol: str,
        position: Position,
        context: MarketContext,
    ) -> ProposedTrade:
        """Create a proposal to close position due to timeout."""
        close_side = Side.SHORT if position.side == Side.LONG else Side.LONG

        return ProposedTrade(
            strategy_id=self.strategy_id,
            symbol=symbol,
            side=close_side,
            notional_usd=position.notional_value,
            risk_per_trade=Decimal(0),
            confidence=Decimal("1.0"),
            reason="[MMR-HFT] Position timeout - closing at market",
            market_context=context,
        )
