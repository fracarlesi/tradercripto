"""Momentum Scalping Strategy - Trend following for HFT (Context Pack 2.0).

This strategy trades momentum in trending markets, using EMA crossovers,
RSI confirmation, and volume analysis to identify high-probability entries.

BOOSTER STRATEGY - Active ONLY in TREND_UP / TREND_DOWN regimes.

Key Features (Context Pack 2.0):
- Regime restriction: ONLY trades in trend_up or trend_down
- EMA confirmation: Price > EMA20 > EMA50 (long) or Price < EMA20 < EMA50 (short)
- RSI filter: RSI > 60 (long) or RSI < 40 (short)
- Volume confirmation: >= 1.2x average volume
- TP/SL constraints:
  - TP: 0.35-0.45%
  - SL: 0.15-0.20%
  - Min RR ratio: 1.5
- Fee-aware: TP_net >= 0.20% after roundtrip fees (0.04%)
"""

import logging
from decimal import Decimal
from typing import List, Optional

from .hft_base import HFTBaseStrategy
from ...core.models import (
    ProposedTrade,
    Bar,
    MarketContext,
    AccountState,
    Position,
)
from ...core.enums import StrategyId, Side, MarketRegime
from ...config.settings import Settings


logger = logging.getLogger(__name__)


class MomentumScalpingStrategy(HFTBaseStrategy):
    """
    Momentum scalping strategy for trending markets.

    Active ONLY when market regime is TREND_UP or TREND_DOWN.
    Uses EMA crossovers + RSI + volume confirmation for entries.

    Key features:
    - Trend following (not mean reversion)
    - Only trades with the trend direction
    - RSI confirmation to avoid chasing extended moves
    - Volume confirmation for conviction
    - Fee-aware TP/SL calculation
    """

    def __init__(self, settings: Settings):
        super().__init__(settings, StrategyId.MOMENTUM_SCALPING)

        # Context Pack 2.0: Load config parameters with updated defaults
        self.min_rsi_up = self._get_param('min_rsi_up', Decimal("60"))      # Long: RSI > 60
        self.max_rsi_down = self._get_param('max_rsi_down', Decimal("40"))  # Short: RSI < 40
        self.min_volume_ratio = self._get_param('min_volume_ratio', Decimal("1.2"))  # Vol >= 1.2x

        # EMA periods for trend confirmation
        self.ema_fast = int(self._get_param('ema_fast', 20))   # EMA20
        self.ema_slow = int(self._get_param('ema_slow', 50))   # EMA50

        # TP/SL constraints (Context Pack 2.0)
        # TP: 0.35-0.45%, SL: 0.15-0.20%, Min RR: 1.5
        self.min_tp_pct = Decimal("0.0035")   # 0.35% minimum TP
        self.max_tp_pct = Decimal("0.0045")   # 0.45% maximum TP
        self.min_sl_pct = Decimal("0.0015")   # 0.15% minimum SL
        self.max_sl_pct = Decimal("0.0020")   # 0.20% maximum SL
        self.min_rr_ratio = Decimal("1.5")    # Minimum Risk/Reward ratio

        # Fee awareness: TP_gross - fee_roundtrip >= 0.20%
        # fee_roundtrip = 0.02% + 0.02% = 0.04%
        # So TP_net >= 0.20% means TP_gross >= 0.24%
        self.min_tp_net_after_fees = Decimal("0.0020")  # 0.20% net profit minimum
        self.fee_roundtrip = Decimal("0.0004")           # 0.04% (maker + maker)

        # Max volatility threshold (skip extreme volatility)
        self.max_volatility_pct = Decimal("0.05")  # 5%

        logger.info(
            f"MomentumScalpingStrategy initialized (Context Pack 2.0): "
            f"RSI_up>{self.min_rsi_up}, RSI_down<{self.max_rsi_down}, "
            f"vol_ratio>={self.min_volume_ratio}, "
            f"TP: {self.min_tp_pct:.4%}-{self.max_tp_pct:.4%}, "
            f"SL: {self.min_sl_pct:.4%}-{self.max_sl_pct:.4%}, "
            f"Min RR: {self.min_rr_ratio}"
        )

    def _get_param(self, name: str, default):
        """Get parameter from HFT config."""
        if self._hft_config:
            return getattr(self._hft_config, name, default)
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
        Evaluate momentum scalping conditions.

        Entry conditions for LONG (TREND_UP regime):
        - Price > EMA20 > EMA50
        - RSI > 60 (confirming momentum)
        - Volume >= 1.2x average
        - Volatility < 5% (not extreme)

        Entry conditions for SHORT (TREND_DOWN regime):
        - Price < EMA20 < EMA50
        - RSI < 40 (confirming momentum)
        - Volume >= 1.2x average
        - Volatility < 5% (not extreme)
        """
        # 1. Check market regime - ONLY trade in trends
        if self._current_regime not in (MarketRegime.TREND_UP, MarketRegime.TREND_DOWN):
            return None

        # 2. Check volatility - skip extreme volatility
        if context.volatility_1h and context.volatility_1h > self.max_volatility_pct:
            return None

        # 3. Skip if position exists (no pyramiding)
        if position:
            return None

        # 4. Check signal cooldown
        if not self.can_signal_hft(symbol):
            return None

        # 5. Validate bar data
        if len(bars) < max(self.ema_slow + 10, 60):
            return None

        current_price = context.mid_price
        if not current_price or current_price <= 0:
            return None

        # 6. Calculate indicators
        ema_fast_val = self._calculate_ema(bars, self.ema_fast)
        ema_slow_val = self._calculate_ema(bars, self.ema_slow)
        rsi = self._calculate_rsi(bars, 14)
        volume_ratio = self._calculate_volume_ratio(bars)

        if not all([ema_fast_val, ema_slow_val, rsi]):
            return None

        # 7. LONG signal (trend up)
        if self._current_regime == MarketRegime.TREND_UP:
            if (current_price > ema_fast_val and
                ema_fast_val > ema_slow_val and
                rsi > self.min_rsi_up and
                volume_ratio >= self.min_volume_ratio):

                confidence = self._calculate_confidence(
                    rsi, volume_ratio, is_long=True
                )

                logger.info(
                    f"[MOMENTUM] LONG signal {symbol}: "
                    f"RSI={rsi:.1f}, Vol={volume_ratio:.2f}x, conf={confidence:.2f}"
                )

                # Create proposal with custom TP/SL (Context Pack 2.0 compliant)
                return self._create_momentum_proposal(
                    symbol=symbol,
                    side=Side.LONG,
                    entry_price=current_price,
                    context=context,
                    confidence=confidence,
                    reason=f"Momentum LONG: RSI={rsi:.1f}, Vol={volume_ratio:.2f}x"
                )

        # 8. SHORT signal (trend down)
        if self._current_regime == MarketRegime.TREND_DOWN:
            if (current_price < ema_fast_val and
                ema_fast_val < ema_slow_val and
                rsi < self.max_rsi_down and
                volume_ratio >= self.min_volume_ratio):

                confidence = self._calculate_confidence(
                    rsi, volume_ratio, is_long=False
                )

                logger.info(
                    f"[MOMENTUM] SHORT signal {symbol}: "
                    f"RSI={rsi:.1f}, Vol={volume_ratio:.2f}x, conf={confidence:.2f}"
                )

                # Create proposal with custom TP/SL (Context Pack 2.0 compliant)
                return self._create_momentum_proposal(
                    symbol=symbol,
                    side=Side.SHORT,
                    entry_price=current_price,
                    context=context,
                    confidence=confidence,
                    reason=f"Momentum SHORT: RSI={rsi:.1f}, Vol={volume_ratio:.2f}x"
                )

        return None

    def _calculate_ema(self, bars: List[Bar], period: int) -> Optional[Decimal]:
        """Calculate Exponential Moving Average."""
        if len(bars) < period:
            return None

        multiplier = Decimal(2) / (Decimal(period) + 1)
        ema = bars[0].close

        for bar in bars[1:]:
            ema = (bar.close - ema) * multiplier + ema

        return ema

    def _calculate_rsi(self, bars: List[Bar], period: int = 14) -> Optional[Decimal]:
        """Calculate Relative Strength Index."""
        if len(bars) < period + 1:
            return None

        gains = []
        losses = []

        for i in range(1, len(bars)):
            change = bars[i].close - bars[i-1].close
            if change > 0:
                gains.append(change)
                losses.append(Decimal(0))
            else:
                gains.append(Decimal(0))
                losses.append(abs(change))

        # Use last 'period' values
        recent_gains = gains[-period:]
        recent_losses = losses[-period:]

        avg_gain = sum(recent_gains) / period
        avg_loss = sum(recent_losses) / period

        if avg_loss == 0:
            return Decimal(100)

        rs = avg_gain / avg_loss
        rsi = Decimal(100) - (Decimal(100) / (1 + rs))

        return rsi

    def _calculate_volume_ratio(self, bars: List[Bar], lookback: int = 20) -> Decimal:
        """Calculate current volume vs N-bar average."""
        if len(bars) < lookback + 1:
            return Decimal("1.0")

        avg_vol = sum(b.volume for b in bars[-lookback-1:-1]) / lookback
        current_vol = bars[-1].volume

        if avg_vol <= 0:
            return Decimal("1.0")

        return current_vol / avg_vol

    def _calculate_confidence(
        self,
        rsi: Decimal,
        volume_ratio: Decimal,
        is_long: bool
    ) -> Decimal:
        """
        Calculate confidence score based on signal strength.

        Factors:
        - RSI distance from threshold (stronger momentum = higher confidence)
        - Volume ratio (higher volume = higher conviction)
        """
        # Base confidence
        confidence = Decimal("0.6")

        # RSI component (0-0.2)
        if is_long:
            rsi_strength = (rsi - self.min_rsi_up) / Decimal("40")  # 60-100 range
        else:
            rsi_strength = (self.max_rsi_down - rsi) / Decimal("40")  # 0-40 range

        rsi_component = min(max(rsi_strength, Decimal("0")), Decimal("1")) * Decimal("0.2")
        confidence += rsi_component

        # Volume component (0-0.2)
        if volume_ratio >= Decimal("2.0"):
            volume_component = Decimal("0.2")
        elif volume_ratio >= Decimal("1.5"):
            volume_component = Decimal("0.1")
        else:
            volume_component = Decimal("0.05")
        confidence += volume_component

        # Cap at 0.9
        return min(confidence, Decimal("0.9"))

    def _create_momentum_proposal(
        self,
        symbol: str,
        side: Side,
        entry_price: Decimal,
        context: MarketContext,
        confidence: Decimal,
        reason: str,
    ) -> Optional[ProposedTrade]:
        """
        Create a momentum scalping proposal with Context Pack 2.0 compliant TP/SL.

        Constraints:
        - MIN_TP = 0.35% (0.0035)
        - MAX_TP = 0.45% (0.0045)
        - MIN_SL = 0.15% (0.0015)
        - MAX_SL = 0.20% (0.0020)
        - MIN_RR = 1.5
        - Fee-awareness: TP_net >= 0.20% after roundtrip fees (0.04%)

        Strategy:
        Use default TP/SL from base class but clamp to our ranges and validate RR.
        """
        from ...core.enums import OrderType

        # Get base TP/SL from parent class (may come from config)
        base_tp_pct = self.take_profit_pct  # From HFTBaseStrategy
        base_sl_pct = self.stop_loss_pct    # From HFTBaseStrategy

        # Clamp TP to our range (0.35%-0.45%)
        tp_pct = max(self.min_tp_pct, min(base_tp_pct, self.max_tp_pct))

        # Clamp SL to our range (0.15%-0.20%)
        sl_pct = max(self.min_sl_pct, min(base_sl_pct, self.max_sl_pct))

        # Validate Risk/Reward ratio (must be >= 1.5)
        rr_ratio = tp_pct / sl_pct if sl_pct > 0 else Decimal("0")
        if rr_ratio < self.min_rr_ratio:
            # Adjust TP to meet minimum RR (keep SL fixed)
            tp_pct = sl_pct * self.min_rr_ratio
            # Re-clamp to max TP
            if tp_pct > self.max_tp_pct:
                # If we can't meet RR with max TP, reduce SL
                sl_pct = self.max_tp_pct / self.min_rr_ratio
                tp_pct = self.max_tp_pct

            logger.info(
                f"[MOMENTUM] {symbol} adjusted TP/SL for RR >= {self.min_rr_ratio}: "
                f"TP={tp_pct:.4%}, SL={sl_pct:.4%}, RR={tp_pct/sl_pct:.2f}"
            )

        # Fee-awareness check: TP_net >= 0.20%
        tp_net = tp_pct - self.fee_roundtrip
        if tp_net < self.min_tp_net_after_fees:
            logger.warning(
                f"[MOMENTUM] {symbol} TP net after fees ({tp_net:.4%}) below minimum "
                f"({self.min_tp_net_after_fees:.4%}), skipping trade"
            )
            return None

        # Calculate actual prices
        if side == Side.LONG:
            tp_price = entry_price * (1 + tp_pct)
            sl_price = entry_price * (1 - sl_pct)
        else:
            tp_price = entry_price * (1 - tp_pct)
            sl_price = entry_price * (1 + sl_pct)

        # Record signal
        self.record_signal_hft(symbol)

        # Get allocation from config
        allocation_pct = Decimal("0.01")  # Default 1%
        if self._hft_config:
            allocation_pct = Decimal(str(getattr(self._hft_config, 'max_position_pct', 0.01)))

        logger.info(
            f"[MOMENTUM] {symbol} {side.value} proposal: "
            f"Entry={entry_price:.2f}, TP={tp_price:.2f} ({tp_pct:.4%}), "
            f"SL={sl_price:.2f} ({sl_pct:.4%}), RR={rr_ratio:.2f}, "
            f"TP_net={tp_net:.4%}"
        )

        return ProposedTrade(
            strategy_id=self.strategy_id,
            symbol=symbol,
            side=side,
            notional_usd=Decimal("1000"),  # Will be overridden by position sizer
            risk_per_trade=Decimal("70"),   # 0.7% of $10k = $70
            entry_type=OrderType.LIMIT_GTX, # Post-only maker order
            entry_price=entry_price,
            stop_loss_price=sl_price,
            take_profit_price=tp_price,
            confidence=confidence,
            reason=f"[HFT-MOMENTUM] {reason}",
            market_context=context,
        )
