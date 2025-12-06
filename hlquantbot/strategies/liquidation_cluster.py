"""
Liquidation Cluster Strategy (Enhanced Proxy Version v2)

Since Hyperliquid doesn't expose liquidation cluster data directly,
we infer likely liquidation zones using:
1. Open Interest changes + price direction
2. Funding rate bias (indicates market positioning)
3. Support/Resistance levels
4. Volume analysis
5. Order book imbalance (v2)
6. OI velocity/acceleration (v2)
7. Market structure score (v2)

Logic:
- When OI increases while price rises = new longs (liquidations below)
- When OI increases while price falls = new shorts (liquidations above)
- Trade INTO these zones expecting cascade moves
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Tuple

from ..core.models import ProposedTrade, AccountState, Bar, MarketContext, Position
from ..core.enums import StrategyId, Side, OrderType, MarketRegime
from ..config.settings import Settings, LiquidationClusterConfig
from .base import BaseStrategy


logger = logging.getLogger(__name__)


class LiquidationClusterStrategy(BaseStrategy):
    """
    Liquidation Cluster Strategy.

    Entry Logic:
    - Identify likely liquidation clusters based on OI and price action
    - Enter when price approaches these zones
    - Trade in direction that would trigger cascade

    Filters:
    - Minimum R:R ratio
    - Volume confirmation
    - Regime filter

    Exit Logic:
    - Take profit at cascade target
    - Tight stop loss if thesis invalidated
    """

    def __init__(self, settings: Settings):
        super().__init__(settings, StrategyId.LIQUIDATION_CLUSTER)
        self.config: LiquidationClusterConfig = settings.strategies.liquidation_cluster

        # Track OI history for delta calculation
        self._oi_history: dict[str, List[Tuple[datetime, Decimal]]] = {}
        self._last_price: dict[str, Decimal] = {}

    async def evaluate(
        self,
        symbol: str,
        bars: List[Bar],
        context: MarketContext,
        account: AccountState,
        position: Optional[Position] = None,
    ) -> Optional[ProposedTrade]:
        """Evaluate liquidation cluster signals."""

        if not bars or len(bars) < self.config.swing_lookback_bars:
            return None

        # Skip if we can't signal yet
        if not self.can_signal(symbol, min_interval_seconds=self.config.signal_cooldown_seconds):
            return None

        # Update OI history
        self._update_oi_history(symbol, context.open_interest)

        # If we have a position, check for exit
        if position:
            return await self._evaluate_exit(symbol, position, bars, context)

        # No position - check for entry
        return await self._evaluate_entry(symbol, bars, context, account)

    async def _evaluate_entry(
        self,
        symbol: str,
        bars: List[Bar],
        context: MarketContext,
        account: AccountState,
    ) -> Optional[ProposedTrade]:
        """Evaluate entry conditions with enhanced v2 indicators."""

        current_price = context.mid_price
        atr = self.calculate_atr(bars, period=14)

        if atr <= 0:
            return None

        # --- ENHANCED FILTERS (v2) ---

        # Filter 1: Skip near funding settlement
        if self._is_near_funding_settlement():
            logger.debug(f"{symbol}: Skipping - near funding settlement")
            return None

        # 1. Analyze OI change and price direction
        oi_change_pct = self._calculate_oi_change(symbol)
        price_change = self._calculate_price_change(symbol, current_price)

        # Filter 2: Market structure score
        structure_score = self._calculate_market_structure_score(bars, context, oi_change_pct)
        if structure_score < self.config.min_structure_score:
            logger.debug(f"{symbol}: Structure score {structure_score:.2f} below min {self.config.min_structure_score}")
            return None

        # Calculate enhanced indicators
        orderbook_imbalance = self._calculate_orderbook_imbalance(context)
        oi_velocity = self._calculate_oi_velocity(symbol)

        # 2. Find support/resistance levels
        swing_highs = self.find_swing_highs(bars, lookback=5)
        swing_lows = self.find_swing_lows(bars, lookback=5)

        # 3. Determine market bias from funding
        funding = context.funding_rate
        market_bias = self._determine_market_bias(oi_change_pct, price_change, funding)

        # 4. Find nearest liquidation zone
        liq_zone, zone_type = self._find_liquidation_zone(
            current_price, swing_highs, swing_lows, market_bias, atr
        )

        if not liq_zone:
            return None

        # 5. Check if price is approaching zone
        proximity_threshold = self.config.level_proximity_pct
        distance_to_zone = abs(current_price - liq_zone) / current_price

        if distance_to_zone > proximity_threshold * 2:  # Not close enough
            return None

        # 6. Determine trade direction
        # If liq zone is below (long liquidations), we SHORT to ride the cascade
        # If liq zone is above (short liquidations), we LONG to ride the squeeze
        if zone_type == "long_liquidations":
            side = Side.SHORT
            reason = f"Long liquidation zone at {liq_zone:.2f} (current: {current_price:.2f})"
        else:
            side = Side.LONG
            reason = f"Short liquidation zone at {liq_zone:.2f} (current: {current_price:.2f})"

        # 7. Calculate entry, stop, and target
        stop_loss, take_profit = self._calculate_levels(
            side, current_price, liq_zone, atr
        )

        # 8. Check R:R ratio
        risk = abs(current_price - stop_loss)
        reward = abs(take_profit - current_price)
        rr_ratio = reward / risk if risk > 0 else Decimal(0)

        if rr_ratio < self.config.min_rr_ratio:
            logger.debug(f"{symbol}: R:R {rr_ratio:.2f} below min {self.config.min_rr_ratio}")
            return None

        # 9. Volume confirmation
        vol_sma = self.calculate_volume_sma(bars, period=20)
        current_vol = bars[-1].volume if bars else Decimal(0)

        if vol_sma > 0:
            vol_ratio = current_vol / vol_sma
            if vol_ratio < self.config.volume_surge_multiplier * Decimal("0.7"):
                logger.debug(f"{symbol}: Volume {vol_ratio:.2f}x not enough")
                return None

        # 10. Calculate enhanced confidence (v2)
        confidence = self._calculate_confidence_v2(
            oi_change_pct=oi_change_pct,
            distance_to_zone=distance_to_zone,
            rr_ratio=rr_ratio,
            market_bias=market_bias,
            structure_score=structure_score,
            orderbook_imbalance=orderbook_imbalance,
            oi_velocity=oi_velocity,
        )

        logger.info(
            f"LIQ CLUSTER {symbol}: {side.value} signal - {reason} "
            f"(R:R: {rr_ratio:.2f}, conf: {confidence:.2f}, struct: {structure_score:.2f})"
        )

        self.record_signal(symbol)
        self._last_price[symbol] = current_price

        return self.create_proposal(
            symbol=symbol,
            side=side,
            context=context,
            confidence=confidence,
            stop_loss_price=stop_loss,
            take_profit_price=take_profit,
            reason=reason,
        )

    async def _evaluate_exit(
        self,
        symbol: str,
        position: Position,
        bars: List[Bar],
        context: MarketContext,
    ) -> Optional[ProposedTrade]:
        """Evaluate exit for existing position."""
        # This strategy relies mainly on SL/TP
        # But we can add early exit logic if needed

        # Check if cascade happened (big move in our direction)
        atr = self.calculate_atr(bars, period=14)
        move_size = abs(context.mid_price - position.entry_price)

        if move_size > atr * Decimal("3"):
            # Big move - consider taking profit
            if (position.side == Side.LONG and context.mid_price > position.entry_price) or \
               (position.side == Side.SHORT and context.mid_price < position.entry_price):
                logger.info(f"LIQ CLUSTER {symbol}: Cascade detected, taking profit")
                return self._create_exit_proposal(symbol, position, context, "Cascade profit")

        return None

    def _update_oi_history(self, symbol: str, oi: Decimal):
        """Update OI history for a symbol."""
        now = datetime.now(timezone.utc)

        if symbol not in self._oi_history:
            self._oi_history[symbol] = []

        self._oi_history[symbol].append((now, oi))

        # Keep only last N entries
        max_entries = self.config.oi_lookback_bars * 2
        if len(self._oi_history[symbol]) > max_entries:
            self._oi_history[symbol] = self._oi_history[symbol][-max_entries:]

    def _calculate_oi_change(self, symbol: str) -> Decimal:
        """Calculate OI change percentage."""
        history = self._oi_history.get(symbol, [])
        if len(history) < 2:
            return Decimal(0)

        lookback = min(self.config.oi_lookback_bars, len(history) - 1)
        old_oi = history[-lookback - 1][1]
        new_oi = history[-1][1]

        if old_oi <= 0:
            return Decimal(0)

        return (new_oi - old_oi) / old_oi

    def _calculate_price_change(self, symbol: str, current_price: Decimal) -> Decimal:
        """Calculate price change since last check."""
        last_price = self._last_price.get(symbol, current_price)
        if last_price <= 0:
            return Decimal(0)
        return (current_price - last_price) / last_price

    # -------------------------------------------------------------------------
    # Enhanced Indicators (v2)
    # -------------------------------------------------------------------------

    def _calculate_orderbook_imbalance(self, context: MarketContext) -> Decimal:
        """
        Calculate bid/ask depth imbalance.

        Returns:
            Positive = bid heavy (bullish), Negative = ask heavy (bearish)
            Range: -1.0 to 1.0
        """
        if not context.bid_depth or not context.ask_depth:
            return Decimal(0)

        total = context.bid_depth + context.ask_depth
        if total <= 0:
            return Decimal(0)

        return (context.bid_depth - context.ask_depth) / total

    def _calculate_oi_velocity(self, symbol: str) -> Decimal:
        """
        Calculate OI velocity (rate of change acceleration).

        A spike in OI velocity indicates rapid position buildup.
        """
        history = self._oi_history.get(symbol, [])
        if len(history) < 3:
            return Decimal(0)

        # Get last 3 OI values
        oi_t0 = history[-1][1]  # Current
        oi_t1 = history[-2][1]  # Previous
        oi_t2 = history[-3][1]  # Before that

        if oi_t0 <= 0 or oi_t1 <= 0:
            return Decimal(0)

        # First derivative (velocity)
        velocity_now = (oi_t0 - oi_t1) / oi_t1
        velocity_prev = (oi_t1 - oi_t2) / oi_t2 if oi_t2 > 0 else Decimal(0)

        # Second derivative (acceleration)
        acceleration = velocity_now - velocity_prev

        return acceleration

    def _calculate_market_structure_score(
        self,
        bars: List[Bar],
        context: MarketContext,
        oi_change_pct: Decimal,
    ) -> Decimal:
        """
        Calculate composite market structure score.

        High score = market is primed for liquidation cascade.
        Looks for: consolidation + volume contraction + OI buildup + extreme funding
        """
        if len(bars) < self.config.consolidation_bars:
            return Decimal("0.5")

        score = Decimal(0)
        recent_bars = bars[-self.config.consolidation_bars:]

        # 1. Consolidation detection (tight price range)
        high_prices = [b.high for b in recent_bars]
        low_prices = [b.low for b in recent_bars]
        price_range = max(high_prices) - min(low_prices)
        avg_price = sum(b.close for b in recent_bars) / len(recent_bars)

        range_pct = price_range / avg_price if avg_price > 0 else Decimal("1")
        if range_pct < Decimal("0.02"):  # < 2% range = tight consolidation
            score += Decimal("0.2")
        elif range_pct < Decimal("0.03"):
            score += Decimal("0.1")

        # 2. Volume contraction
        vol_recent = sum(b.volume for b in bars[-5:]) / 5
        vol_older = sum(b.volume for b in bars[-20:-5]) / 15 if len(bars) >= 20 else vol_recent

        if vol_older > 0 and vol_recent < vol_older * Decimal("0.7"):
            score += Decimal("0.15")

        # 3. OI buildup during consolidation
        if abs(oi_change_pct) > Decimal("0.03"):
            score += Decimal("0.25")
        elif abs(oi_change_pct) > Decimal("0.02"):
            score += Decimal("0.15")

        # 4. Extreme funding
        if context.funding_rate and abs(context.funding_rate) > self.config.funding_extreme_threshold:
            score += Decimal("0.2")

        # 5. Order book imbalance
        imbalance = self._calculate_orderbook_imbalance(context)
        if abs(imbalance) > Decimal("0.3"):
            score += Decimal("0.2")

        return min(score, Decimal("1.0"))

    def _is_near_funding_settlement(self) -> bool:
        """
        Check if we're near funding settlement time.

        Hyperliquid funding settles every 8 hours at 00:00, 08:00, 16:00 UTC.
        Skip trading N minutes before/after to avoid funding volatility.
        """
        now = datetime.now(timezone.utc)
        hour = now.hour
        minute = now.minute

        settlement_hours = [0, 8, 16]
        avoid_minutes = self.config.avoid_funding_settlement_minutes

        for sh in settlement_hours:
            # Check if within N minutes after settlement
            if hour == sh and minute < avoid_minutes:
                return True
            # Check if within N minutes before settlement
            if hour == (sh - 1) % 24 and minute > (60 - avoid_minutes):
                return True

        return False

    def _determine_market_bias(
        self,
        oi_change: Decimal,
        price_change: Decimal,
        funding: Decimal,
    ) -> str:
        """
        Determine market positioning bias.

        Returns: "long_heavy", "short_heavy", or "neutral"
        """
        threshold = self.config.oi_change_threshold_pct

        if oi_change > threshold:
            # OI increased significantly
            if price_change > 0:
                # Price up + OI up = new longs entered
                return "long_heavy"
            elif price_change < 0:
                # Price down + OI up = new shorts entered
                return "short_heavy"

        # Also consider funding
        if funding > Decimal("0.0003"):
            return "long_heavy"
        elif funding < Decimal("-0.0003"):
            return "short_heavy"

        return "neutral"

    def _find_liquidation_zone(
        self,
        current_price: Decimal,
        swing_highs: List[Decimal],
        swing_lows: List[Decimal],
        market_bias: str,
        atr: Decimal,
    ) -> Tuple[Optional[Decimal], str]:
        """
        Find the nearest likely liquidation zone.

        Returns: (price_level, zone_type)
        """
        # Long liquidations typically cluster below recent swing lows
        # Short liquidations cluster above recent swing highs

        # Adjust for typical liquidation distance (usually 3-10% from entries)
        # With leverage, liquidations are closer

        if market_bias == "long_heavy" and swing_lows:
            # Look for long liquidation zone below
            # Longs entered recently, their stops/liquidations are below
            lowest_recent = min(swing_lows[-3:]) if len(swing_lows) >= 3 else min(swing_lows)
            # Liquidations typically 2-5% below recent lows
            liq_zone = lowest_recent * Decimal("0.97")

            if liq_zone < current_price:
                return liq_zone, "long_liquidations"

        elif market_bias == "short_heavy" and swing_highs:
            # Look for short liquidation zone above
            highest_recent = max(swing_highs[-3:]) if len(swing_highs) >= 3 else max(swing_highs)
            # Liquidations typically 2-5% above recent highs
            liq_zone = highest_recent * Decimal("1.03")

            if liq_zone > current_price:
                return liq_zone, "short_liquidations"

        # Fallback: use ATR-based levels
        if market_bias == "long_heavy":
            return current_price - atr * Decimal("2"), "long_liquidations"
        elif market_bias == "short_heavy":
            return current_price + atr * Decimal("2"), "short_liquidations"

        return None, ""

    def _calculate_levels(
        self,
        side: Side,
        entry_price: Decimal,
        liq_zone: Decimal,
        atr: Decimal,
    ) -> Tuple[Decimal, Decimal]:
        """Calculate stop loss and take profit levels."""
        sl_mult = self.config.stop_loss_atr_multiplier
        tp_mult = self.config.take_profit_atr_multiplier

        if side == Side.LONG:
            # Going long into short squeeze
            stop_loss = entry_price - (atr * sl_mult)
            take_profit = entry_price + (atr * tp_mult)
        else:
            # Going short into long liquidation cascade
            stop_loss = entry_price + (atr * sl_mult)
            take_profit = entry_price - (atr * tp_mult)

        return stop_loss, take_profit

    def _calculate_confidence(
        self,
        oi_change: Decimal,
        distance_to_zone: Decimal,
        rr_ratio: Decimal,
        market_bias: str,
    ) -> Decimal:
        """Calculate signal confidence (legacy v1)."""
        confidence = Decimal("0.5")

        # Higher OI change = more confidence
        if abs(oi_change) > Decimal("0.1"):
            confidence += Decimal("0.15")
        elif abs(oi_change) > Decimal("0.05"):
            confidence += Decimal("0.1")

        # Closer to zone = more confidence
        if distance_to_zone < Decimal("0.005"):
            confidence += Decimal("0.15")
        elif distance_to_zone < Decimal("0.01"):
            confidence += Decimal("0.1")

        # Better R:R = more confidence
        if rr_ratio > Decimal("3"):
            confidence += Decimal("0.1")

        # Clear bias = more confidence
        if market_bias != "neutral":
            confidence += Decimal("0.05")

        return min(confidence, Decimal("0.9"))

    def _calculate_confidence_v2(
        self,
        oi_change_pct: Decimal,
        distance_to_zone: Decimal,
        rr_ratio: Decimal,
        market_bias: str,
        structure_score: Decimal,
        orderbook_imbalance: Decimal,
        oi_velocity: Decimal,
    ) -> Decimal:
        """
        Calculate enhanced signal confidence (v2).

        Incorporates all new indicators for better decision making.
        """
        # Base confidence
        confidence = Decimal("0.4")

        # 1. OI change (0 - 0.15)
        if abs(oi_change_pct) > Decimal("0.10"):
            confidence += Decimal("0.15")
        elif abs(oi_change_pct) > Decimal("0.05"):
            confidence += Decimal("0.10")

        # 2. Proximity to zone (0 - 0.15)
        if distance_to_zone < Decimal("0.003"):
            confidence += Decimal("0.15")
        elif distance_to_zone < Decimal("0.007"):
            confidence += Decimal("0.10")

        # 3. R:R ratio (0 - 0.10)
        if rr_ratio > Decimal("3.0"):
            confidence += Decimal("0.10")
        elif rr_ratio > Decimal("2.5"):
            confidence += Decimal("0.05")

        # 4. Market bias clarity (0 - 0.05)
        if market_bias != "neutral":
            confidence += Decimal("0.05")

        # --- ENHANCED FACTORS (v2) ---

        # 5. Structure score (0 - 0.15)
        # Higher structure score = better setup
        confidence += structure_score * Decimal("0.15")

        # 6. Order book imbalance (0 - 0.10)
        # Strong imbalance in either direction confirms thesis
        if abs(orderbook_imbalance) > self.config.orderbook_imbalance_threshold:
            confidence += Decimal("0.10")
        elif abs(orderbook_imbalance) > Decimal("0.15"):
            confidence += Decimal("0.05")

        # 7. OI velocity (0 - 0.10)
        # High acceleration = rapid position buildup
        if abs(oi_velocity) > self.config.oi_velocity_threshold:
            confidence += Decimal("0.10")
        elif abs(oi_velocity) > Decimal("0.01"):
            confidence += Decimal("0.05")

        # Cap at 0.95 (never 100% confident)
        return min(confidence, Decimal("0.95"))

    def _create_exit_proposal(
        self,
        symbol: str,
        position: Position,
        context: MarketContext,
        reason: str,
    ) -> ProposedTrade:
        """Create exit proposal."""
        return ProposedTrade(
            strategy_id=self.strategy_id,
            symbol=symbol,
            side=Side.FLAT,
            entry_type=OrderType.MARKET,
            confidence=Decimal("0.9"),
            reason=reason,
            market_context=context,
        )
