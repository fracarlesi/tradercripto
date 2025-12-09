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
from ...core.enums import StrategyId, Side, TimeFrame, MarketRegime
from ...config.settings import Settings
from .hft_base import HFTBaseStrategy


logger = logging.getLogger(__name__)


# Regime-based direction filtering
# In TREND_UP: only LONG allowed (don't short against the trend)
# In TREND_DOWN: only SHORT allowed (don't long against the trend)
REGIME_ALLOWED_DIRECTIONS = {
    MarketRegime.TREND_UP: [Side.LONG],
    MarketRegime.TREND_DOWN: [Side.SHORT],
    MarketRegime.RANGE_BOUND: [Side.LONG, Side.SHORT],
    MarketRegime.LOW_VOLATILITY: [Side.LONG, Side.SHORT],
    MarketRegime.HIGH_VOLATILITY: [],  # Disable in high vol (too noisy)
    MarketRegime.UNCERTAIN: [],  # Disable when uncertain
}


class MicroBreakoutStrategy(HFTBaseStrategy):
    """
    Breakout Strategy 2.0 - HFT Compression Breakout with ATR-based TP/SL.

    PHASE 1 - Compression Detection:
    --------------------------------
    Identifies tight consolidation using compression ratio:
    - range_30 = max_high_30 - min_low_30
    - compression_ratio = range_30 / EMA(range_30, 30)
    - Trigger: ratio < 0.7 (tight compression)

    PHASE 2 - Breakout Validation:
    --------------------------------
    A breakout is valid ONLY if ALL conditions are met:
    1. Volume >= 2x media 20 barre (200% volume surge)
    2. ΔOI >= 5% (Open Interest change confirms momentum)

    TP/SL CALCULATION:
    -----------------
    Dynamic ATR-based risk/reward:
    - TP = entry ± 1.2 × ATR_5 (approx 0.35%+)
    - SL = entry ∓ 0.6 × ATR_5 (approx 0.20% max)
    - Target RR ≈ 2.0

    GLOBAL CONSTRAINTS:
    ------------------
    All trades must satisfy:
    - MIN_TP = 0.35% (gross)
    - MAX_SL = 0.20%
    - MIN_RR = 1.5
    - Fee-aware: TP_net = TP_gross - 0.04% >= 0.20%

    Timeframe-specific behavior:
    - M1: Ultra-fast breakouts, tight compression
    - M5: Balanced breakouts (default)
    - M15: Swing-style breakouts

    Ideal conditions:
    - Compression ratio < 0.7 (tight range)
    - Volume spike >= 2x average
    - OI increase >= 5%
    - Clear directional move after consolidation
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
        self.volume_surge_multiplier = self._get_param('volume_surge_multiplier', Decimal("2.0"))

        # Breakout Strategy 2.0 parameters
        self.oi_change_threshold_pct = self._get_param('oi_change_threshold_pct', Decimal("0.05"))  # ΔOI >= 5% (exact)
        self.compression_ratio_threshold = self._get_param('compression_ratio_threshold', Decimal("0.7"))  # Compression < 0.7
        self.atr_tp_multiplier = self._get_param('atr_tp_multiplier', Decimal("1.2"))  # TP = 1.2 × ATR_5
        self.atr_sl_multiplier = self._get_param('atr_sl_multiplier', Decimal("0.6"))  # SL = 0.6 × ATR_5

        # Tracking
        self._consolidation_ranges: dict = {}  # symbol -> (low, high)
        self._oi_history: dict = {}  # symbol -> [(timestamp, oi)]

        # Current market regime (set by bot)
        self._current_regime: MarketRegime = MarketRegime.UNCERTAIN

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

    def set_regime(self, regime: MarketRegime):
        """Set current market regime. Called by bot."""
        self._current_regime = regime

    def is_direction_allowed(self, side: Side) -> bool:
        """
        Check if trade direction is allowed in current regime.

        Prevents:
        - Shorting during uptrends (TREND_UP)
        - Longing during downtrends (TREND_DOWN)
        - Trading in HIGH_VOLATILITY or UNCERTAIN (too noisy)
        """
        allowed_directions = REGIME_ALLOWED_DIRECTIONS.get(
            self._current_regime, []
        )
        return side in allowed_directions

    async def evaluate(
        self,
        symbol: str,
        bars: List[Bar],
        context: MarketContext,
        account: AccountState,
        position: Optional[Position] = None,
    ) -> Optional[ProposedTrade]:
        """
        Evaluate Breakout Strategy 2.0.

        Phase 1: Compression Detection
        - Calculate 30-bar range: range_30 = max_high_30 - min_low_30
        - Calculate EMA of historical ranges
        - compression_ratio = range_30 / EMA(range_30, 30)
        - Trigger when ratio < 0.7 (tight compression)

        Phase 2: Breakout Validation
        - Price breaks above/below consolidation range
        - Volume >= 2x media 20 barre (200% surge)
        - ΔOI >= 5% (Open Interest confirms momentum)
        - Calculate ATR_5 based TP/SL
        - Validate global constraints (MIN_TP, MAX_SL, MIN_RR, fee-aware)
        - Enter in breakout direction if all conditions met
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

        # Track OI history for ΔOI check
        open_interest = getattr(context, 'open_interest', None)
        if open_interest:
            self._update_oi_history(symbol, open_interest)

        # Check for consolidation
        consolidation = self._detect_consolidation(bars, symbol)
        if consolidation:
            # We're in consolidation - check for breakout
            cons_low, cons_high = consolidation
            return self._check_breakout(
                symbol, bars, current_price, cons_low, cons_high, context
            )

        return None

    def _update_oi_history(self, symbol: str, oi: Decimal):
        """Track OI history for ΔOI calculation."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)

        if symbol not in self._oi_history:
            self._oi_history[symbol] = []

        self._oi_history[symbol].append((now, oi))

        # Keep last 10 minutes of data
        cutoff = now.timestamp() - 600
        self._oi_history[symbol] = [
            (ts, o) for ts, o in self._oi_history[symbol]
            if ts.timestamp() >= cutoff
        ]

    def _check_oi_change(self, symbol: str) -> bool:
        """
        Breakout Strategy 2.0 - Phase 2: OI Validation.

        A breakout is valid only if:
        - ΔOI >= 5% (absolute change, directional)

        Returns True if OI change confirms breakout momentum.
        """
        oi_history = self._oi_history.get(symbol, [])
        if len(oi_history) < 2:
            logger.debug(f"OI check: Not enough data for {symbol}, allowing trade")
            return True  # Not enough data, allow trade

        oldest_oi = oi_history[0][1]
        current_oi = oi_history[-1][1]

        if oldest_oi == 0:
            result = current_oi > 0
            logger.debug(f"OI check: oldest_oi=0, current_oi={current_oi}, result={result}")
            return result

        # Calculate absolute OI change percentage
        oi_change_pct = abs((current_oi - oldest_oi) / oldest_oi)
        is_valid = oi_change_pct >= Decimal("0.05")  # Exactly 5%

        logger.debug(
            f"OI check: oldest={oldest_oi}, current={current_oi}, "
            f"change={oi_change_pct:.2%}, valid={is_valid} (need >=5%)"
        )

        return is_valid

    def _calculate_atr(self, bars: List[Bar], period: int = 5) -> Decimal:
        """
        Calculate Average True Range for dynamic TP/SL.

        Breakout Strategy 2.0 uses ATR_5 (5-period ATR) for:
        - TP = 1.2 × ATR_5
        - SL = 0.6 × ATR_5
        - Target RR ≈ 2
        """
        if len(bars) < period + 1:
            return Decimal("0")

        tr_values = []
        for i in range(-period, 0):
            high = bars[i].high
            low = bars[i].low
            prev_close = bars[i - 1].close

            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close)
            )
            tr_values.append(tr)

        return sum(tr_values) / len(tr_values)

    def _calculate_ema(self, values: List[Decimal], period: int) -> Decimal:
        """
        Calculate Exponential Moving Average.

        Used for compression ratio calculation (EMA of range_30).
        """
        if len(values) < period:
            # Fallback to SMA if not enough data
            return sum(values) / len(values) if values else Decimal(0)

        multiplier = Decimal("2") / (period + 1)
        ema = values[0]  # Start with first value

        for value in values[1:]:
            ema = (value * multiplier) + (ema * (1 - multiplier))

        return ema

    def _detect_consolidation(
        self,
        bars: List[Bar],
        symbol: str,
    ) -> Optional[Tuple[Decimal, Decimal]]:
        """
        Breakout Strategy 2.0 - Phase 1: Compression Detection.

        Detects consolidation using compression ratio:
        - range_30 = max_high_30 - min_low_30
        - compression_ratio = range_30 / EMA(range_30, 30)
        - Trigger when ratio < 0.7 (tight compression)

        Returns (low, high) of consolidation range if detected.
        """
        # Need at least 30 bars for compression detection
        if len(bars) < 30:
            return None

        # Phase 1: Calculate 30-bar range
        lookback_30 = bars[-30:]
        max_high_30 = max(b.high for b in lookback_30)
        min_low_30 = min(b.low for b in lookback_30)
        range_30 = max_high_30 - min_low_30

        # Calculate historical ranges for EMA
        range_history = []
        for i in range(len(bars) - 30, len(bars)):
            if i < 30:
                continue
            window = bars[i-30:i]
            window_high = max(b.high for b in window)
            window_low = min(b.low for b in window)
            range_history.append(window_high - window_low)

        if not range_history:
            return None

        # Calculate EMA of range_30
        ema_range = self._calculate_ema(range_history, min(30, len(range_history)))

        # Calculate compression ratio
        if ema_range == 0:
            return None

        compression_ratio = range_30 / ema_range

        # Log periodically for debugging (every ~30 calls per symbol)
        if hash(f"{symbol}{int(bars[-1].timestamp.timestamp()) // 30}") % 30 == 0:
            logger.info(
                f"Micro-Breakout Compression Check [{self.primary_timeframe.value}]: {symbol} | "
                f"Range_30: {range_30:.2f} | EMA_Range: {ema_range:.2f} | "
                f"Compression Ratio: {compression_ratio:.3f} | Threshold: {self.compression_ratio_threshold:.3f}"
            )

        # Trigger compression when ratio < 0.7
        if compression_ratio < self.compression_ratio_threshold:
            # Store consolidation range (30-bar high/low)
            self._consolidation_ranges[symbol] = (min_low_30, max_high_30)
            logger.info(
                f"Micro-Breakout: {symbol} COMPRESSION DETECTED | "
                f"Ratio: {compression_ratio:.3f} | Range: [{min_low_30:.2f}, {max_high_30:.2f}]"
            )
            return (min_low_30, max_high_30)

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
        """
        Breakout Strategy 2.0 - Phase 2: Breakout Detection and Validation.

        Validates breakout with:
        1. Volume >= 2x media 20 barre
        2. ΔOI >= 5%
        3. ATR-based TP/SL with global constraints
        """
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

        # REGIME FILTER: Check if direction is allowed in current regime
        if not self.is_direction_allowed(side):
            logger.debug(
                f"Micro-Breakout: {symbol} {side.value} blocked by regime filter "
                f"(regime={self._current_regime.value})"
            )
            return None

        # Phase 2 Validation: Volume confirmation (>= 2x average 20 bars)
        if not self._check_volume_surge(bars):
            logger.debug(f"Micro-Breakout: {symbol} breakout without volume surge, skipping")
            return None

        # Phase 2 Validation: OI change confirmation (ΔOI >= 5%)
        if not self._check_oi_change(symbol):
            logger.debug(f"Micro-Breakout: {symbol} breakout without OI change, skipping")
            return None

        # Calculate ATR_5 for dynamic TP/SL
        atr_5 = self._calculate_atr(bars, period=5)
        if atr_5 == 0:
            logger.warning(f"Micro-Breakout: {symbol} ATR_5 is zero, skipping")
            return None

        # Calculate ATR-based TP/SL
        # TP = 1.2 × ATR_5, SL = 0.6 × ATR_5 (RR ≈ 2)
        if side == Side.LONG:
            tp_price = current_price + (atr_5 * self.atr_tp_multiplier)
            sl_price = current_price - (atr_5 * self.atr_sl_multiplier)
        else:
            tp_price = current_price - (atr_5 * self.atr_tp_multiplier)
            sl_price = current_price + (atr_5 * self.atr_sl_multiplier)

        # Calculate TP/SL percentages
        tp_pct = abs((tp_price - current_price) / current_price)
        sl_pct = abs((sl_price - current_price) / current_price)

        # Global Constraints Validation
        MIN_TP = Decimal("0.0035")  # 0.35%
        MAX_SL = Decimal("0.0020")  # 0.20%
        MIN_RR = Decimal("1.5")
        MAKER_FEE_ROUNDTRIP = Decimal("0.0004")  # 0.02% × 2 = 0.04%
        MIN_NET_TP = Decimal("0.0020")  # 0.20% net after fees

        # Check MIN_TP constraint
        if tp_pct < MIN_TP:
            logger.debug(
                f"Micro-Breakout: {symbol} TP too small: {tp_pct:.4%} < {MIN_TP:.4%}, skipping"
            )
            return None

        # Check MAX_SL constraint
        if sl_pct > MAX_SL:
            logger.debug(
                f"Micro-Breakout: {symbol} SL too large: {sl_pct:.4%} > {MAX_SL:.4%}, skipping"
            )
            return None

        # Check MIN_RR constraint
        risk_reward = tp_pct / sl_pct if sl_pct > 0 else Decimal("0")
        if risk_reward < MIN_RR:
            logger.debug(
                f"Micro-Breakout: {symbol} RR too low: {risk_reward:.2f} < {MIN_RR:.2f}, skipping"
            )
            return None

        # Check fee-awareness: TP_gross - fees >= MIN_NET_TP
        net_tp = tp_pct - MAKER_FEE_ROUNDTRIP
        if net_tp < MIN_NET_TP:
            logger.debug(
                f"Micro-Breakout: {symbol} Net TP too small after fees: "
                f"{net_tp:.4%} < {MIN_NET_TP:.4%}, skipping"
            )
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
            f"Strength: {breakout_strength:.2%} | ATR_5: {atr_5:.4f} | "
            f"TP: {tp_pct:.4%} | SL: {sl_pct:.4%} | RR: {risk_reward:.2f} | "
            f"Net TP: {net_tp:.4%}"
        )

        proposal = self.create_hft_proposal(
            symbol=symbol,
            side=side,
            entry_price=current_price,
            context=context,
            confidence=confidence,
            reason=reason,
        )

        # Override TP/SL with ATR-based values
        if proposal:
            proposal.take_profit_price = tp_price
            proposal.stop_loss_price = sl_price

        return proposal

    def _check_volume_surge(self, bars: List[Bar]) -> bool:
        """
        Breakout Strategy 2.0 - Phase 2: Volume Confirmation.

        A breakout is valid only if:
        - Volume >= 2x media 20 barre

        Uses exactly 20 bars for average calculation.
        """
        if len(bars) < 21:  # Need current bar + 20 previous bars
            return True  # Not enough data, allow trade

        current_vol = bars[-1].volume
        # Calculate average of previous 20 bars (not including current bar)
        avg_vol_20 = sum(b.volume for b in bars[-21:-1]) / 20

        if avg_vol_20 == 0:
            return current_vol > 0

        # Volume must be >= 2x average (200%)
        volume_ratio = current_vol / avg_vol_20
        is_surge = volume_ratio >= Decimal("2.0")

        if not is_surge:
            logger.debug(
                f"Volume check failed: current={current_vol:.0f}, "
                f"avg_20={avg_vol_20:.0f}, ratio={volume_ratio:.2f}x (need 2.0x)"
            )

        return is_surge

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
