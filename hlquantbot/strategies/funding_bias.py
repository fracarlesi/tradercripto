"""
Funding Bias Strategy

Trades based on funding rate extremes. When funding is very positive (longs pay shorts),
we go short to collect funding. When funding is very negative, we go long.

This is a DIRECTIONAL strategy (not arbitrage), so we manage price risk.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from ..core.models import ProposedTrade, AccountState, Bar, MarketContext, Position
from ..core.enums import StrategyId, Side, OrderType, MarketRegime
from ..config.settings import Settings, FundingBiasConfig
from .base import BaseStrategy


logger = logging.getLogger(__name__)


class FundingBiasStrategy(BaseStrategy):
    """
    Funding Bias Strategy.

    Entry Logic:
    - Go SHORT when funding rate > threshold (longs pay shorts)
    - Go LONG when funding rate < -threshold (shorts pay longs)

    Filters:
    - Minimum open interest
    - Maximum volatility (ATR-based)
    - Regime filter (avoid high volatility regimes)

    Exit Logic:
    - Funding rate normalizes (crosses zero or threshold)
    - Stop loss hit
    - Max hold time exceeded
    """

    def __init__(self, settings: Settings):
        super().__init__(settings, StrategyId.FUNDING_BIAS)
        self.config: FundingBiasConfig = settings.strategies.funding_bias

        # Track position entry times for max hold
        self._entry_times: dict[str, datetime] = {}

    async def evaluate(
        self,
        symbol: str,
        bars: List[Bar],
        context: MarketContext,
        account: AccountState,
        position: Optional[Position] = None,
    ) -> Optional[ProposedTrade]:
        """Evaluate funding bias signals."""

        # Skip if we can't signal yet
        if not self.can_signal(symbol, min_interval_seconds=self.config.signal_cooldown_seconds):
            return None

        # Get funding rate
        funding_rate = context.funding_rate
        predicted_funding = context.predicted_funding or funding_rate

        # Blend current and predicted funding
        weight = self.config.predicted_funding_weight
        effective_funding = (
            funding_rate * (1 - weight) + predicted_funding * weight
        )

        # Check if we have an existing position
        if position:
            return await self._evaluate_exit(symbol, position, effective_funding, bars, context)

        # No position - check for entry
        return await self._evaluate_entry(symbol, effective_funding, bars, context, account)

    async def _evaluate_entry(
        self,
        symbol: str,
        funding: Decimal,
        bars: List[Bar],
        context: MarketContext,
        account: AccountState,
    ) -> Optional[ProposedTrade]:
        """Evaluate entry conditions."""

        # Check regime filter
        if self._current_regime == MarketRegime.HIGH_VOLATILITY:
            logger.debug(f"{symbol}: Skipping due to high volatility regime")
            return None

        # Check minimum open interest
        min_oi = self.config.min_open_interest_usd
        if context.open_interest < min_oi:
            logger.debug(f"{symbol}: OI {context.open_interest} below min {min_oi}")
            return None

        # Check volatility filter
        atr = self.calculate_atr(bars, period=14)
        if context.mid_price > 0:
            atr_pct = atr / context.mid_price
            if atr_pct > self.config.max_volatility_atr_pct:
                logger.debug(f"{symbol}: ATR {atr_pct:.2%} too high")
                return None

        # Determine signal
        high_threshold = self.config.funding_threshold_high
        low_threshold = self.config.funding_threshold_low

        side = None
        confidence = Decimal("0.5")

        if funding >= high_threshold:
            # High positive funding = go SHORT (collect funding from longs)
            side = Side.SHORT
            # Higher confidence for more extreme funding
            confidence = min(Decimal("0.9"), Decimal("0.5") + (funding / high_threshold) * Decimal("0.2"))
            reason = f"Funding rate {funding:.4%} > threshold {high_threshold:.4%}"

        elif funding <= low_threshold:
            # High negative funding = go LONG (collect funding from shorts)
            side = Side.LONG
            confidence = min(Decimal("0.9"), Decimal("0.5") + (abs(funding) / abs(low_threshold)) * Decimal("0.2"))
            reason = f"Funding rate {funding:.4%} < threshold {low_threshold:.4%}"

        if not side:
            return None

        # Calculate stop loss and take profit
        current_price = context.mid_price
        stop_loss, take_profit = self._calculate_exit_levels(side, current_price, atr)

        logger.info(
            f"FUNDING BIAS {symbol}: {side.value} signal - {reason} "
            f"(confidence: {confidence:.2f})"
        )

        self.record_signal(symbol)
        self._entry_times[symbol] = datetime.now(timezone.utc)

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
        funding: Decimal,
        bars: List[Bar],
        context: MarketContext,
    ) -> Optional[ProposedTrade]:
        """Evaluate exit conditions for existing position."""

        # Check max hold time
        entry_time = self._entry_times.get(symbol)
        if entry_time:
            hold_hours = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
            if hold_hours >= self.config.max_hold_hours:
                logger.info(f"FUNDING BIAS {symbol}: Max hold time exceeded ({hold_hours:.1f}h)")
                return self._create_exit_proposal(symbol, position, context, "Max hold time exceeded")

        # Check if funding has normalized
        high_threshold = self.config.funding_threshold_high
        low_threshold = self.config.funding_threshold_low

        should_exit = False
        reason = ""

        if position.side == Side.SHORT:
            # We're short - exit if funding drops below threshold or goes negative
            if funding < high_threshold * Decimal("0.5"):
                should_exit = True
                reason = f"Funding normalized: {funding:.4%}"

        elif position.side == Side.LONG:
            # We're long - exit if funding rises above threshold or goes positive
            if funding > low_threshold * Decimal("0.5"):
                should_exit = True
                reason = f"Funding normalized: {funding:.4%}"

        if should_exit:
            logger.info(f"FUNDING BIAS {symbol}: Exit signal - {reason}")
            return self._create_exit_proposal(symbol, position, context, reason)

        return None

    def _calculate_exit_levels(
        self,
        side: Side,
        price: Decimal,
        atr: Decimal,
    ) -> tuple[Decimal, Decimal]:
        """Calculate stop loss and take profit levels."""

        # For funding strategy, we use tighter stops since we're
        # mainly trying to capture funding, not big price moves
        stop_distance = atr * Decimal("1.5")
        tp_distance = atr * Decimal("2.0")

        if side == Side.LONG:
            stop_loss = price - stop_distance
            take_profit = price + tp_distance
        else:
            stop_loss = price + stop_distance
            take_profit = price - tp_distance

        return stop_loss, take_profit

    def _create_exit_proposal(
        self,
        symbol: str,
        position: Position,
        context: MarketContext,
        reason: str,
    ) -> ProposedTrade:
        """Create a proposal to close position."""
        # Clean up entry time
        self._entry_times.pop(symbol, None)

        return ProposedTrade(
            strategy_id=self.strategy_id,
            symbol=symbol,
            side=Side.FLAT,  # Signal to close
            entry_type=OrderType.MARKET,
            confidence=Decimal("0.9"),
            reason=reason,
            market_context=context,
        )
