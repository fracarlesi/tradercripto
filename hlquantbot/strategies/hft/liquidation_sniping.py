"""Liquidation Sniping Strategy - Trade liquidation cascades."""

import logging
from decimal import Decimal
from typing import List, Optional, Dict
from datetime import datetime, timezone

from ...core.models import (
    ProposedTrade,
    AccountState,
    Bar,
    MarketContext,
    Position,
)
from ...core.enums import StrategyId, Side, OrderType
from ...config.settings import Settings
from .hft_base import HFTBaseStrategy


logger = logging.getLogger(__name__)


class LiquidationSnipingStrategy(HFTBaseStrategy):
    """
    Liquidation Sniping HFT Strategy.

    Logic:
    - Detect liquidation cascades via:
      - Sudden OI (Open Interest) drops
      - Price spikes with high volume
      - Funding rate extremes
    - Trade the bounce after cascade exhaustion
    - Very short hold time (capture the snap-back)

    This is a counter-trend strategy that requires:
    - Fast detection of liquidation events
    - Quick entry after cascade peak
    - Very tight risk management

    Key indicators:
    - OI spike (sudden change)
    - Price spike (rapid move)
    - Volume surge
    - Funding rate (indicates crowded positions)
    """

    def __init__(self, settings: Settings):
        super().__init__(settings, StrategyId.LIQUIDATION_SNIPING)

        # Liquidation detection parameters
        self.oi_spike_threshold_pct = self._get_param('oi_spike_threshold_pct', Decimal("0.02"))
        self.oi_spike_window_seconds = self._get_param_int('oi_spike_window_seconds', 60)
        self.price_spike_threshold_pct = self._get_param('price_spike_threshold_pct', Decimal("0.005"))
        self.entry_delay_ms = self._get_param_int('entry_delay_ms', 100)

        # Tracking
        self._oi_history: Dict[str, List[tuple]] = {}  # symbol -> [(timestamp, oi)]
        self._price_history: Dict[str, List[tuple]] = {}  # symbol -> [(timestamp, price)]
        self._last_liquidation_signal: Dict[str, datetime] = {}
        self._cascade_detected: Dict[str, dict] = {}  # symbol -> cascade info

    def _get_param(self, name: str, default: Decimal) -> Decimal:
        if self._hft_config:
            return Decimal(str(getattr(self._hft_config, name, default)))
        return default

    def _get_param_int(self, name: str, default: int) -> int:
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
        Evaluate liquidation sniping strategy.

        Steps:
        1. Track OI and price history
        2. Detect liquidation cascade
        3. Wait for cascade exhaustion
        4. Enter counter-trend
        """
        if not self.can_signal_hft(symbol):
            return None

        if position:
            if self.should_close_for_timeout(symbol):
                return self._create_close_proposal(symbol, position, context)
            return None

        current_price = context.current_price
        if not current_price or current_price == 0:
            return None

        # Update history
        self._update_price_history(symbol, current_price)

        # Get OI from context if available
        open_interest = getattr(context, 'open_interest', None)
        if open_interest:
            self._update_oi_history(symbol, open_interest)

        # Detect liquidation cascade
        cascade = self._detect_liquidation_cascade(symbol, current_price, context)

        if cascade:
            # Wait for exhaustion and enter counter-trend
            return self._evaluate_cascade_entry(symbol, cascade, current_price, context)

        return None

    def _update_price_history(self, symbol: str, price: Decimal):
        """Update price history for a symbol."""
        now = datetime.now(timezone.utc)

        if symbol not in self._price_history:
            self._price_history[symbol] = []

        self._price_history[symbol].append((now, price))

        # Clean old entries
        cutoff = now.timestamp() - self.oi_spike_window_seconds
        self._price_history[symbol] = [
            (ts, p) for ts, p in self._price_history[symbol]
            if ts.timestamp() >= cutoff
        ]

    def _update_oi_history(self, symbol: str, oi: Decimal):
        """Update OI history for a symbol."""
        now = datetime.now(timezone.utc)

        if symbol not in self._oi_history:
            self._oi_history[symbol] = []

        self._oi_history[symbol].append((now, oi))

        # Clean old entries
        cutoff = now.timestamp() - self.oi_spike_window_seconds
        self._oi_history[symbol] = [
            (ts, o) for ts, o in self._oi_history[symbol]
            if ts.timestamp() >= cutoff
        ]

    def _detect_liquidation_cascade(
        self,
        symbol: str,
        current_price: Decimal,
        context: MarketContext,
    ) -> Optional[dict]:
        """
        Detect if a liquidation cascade is occurring or just occurred.

        Cascade indicators:
        1. OI drop > threshold (positions liquidated)
        2. Price spike > threshold (cascade price impact)
        3. Volume surge (liquidations executing)
        """
        now = datetime.now(timezone.utc)

        # Check if we already have an active cascade detection
        existing = self._cascade_detected.get(symbol)
        if existing:
            # Check if still valid (within entry window)
            elapsed = (now - existing["detected_at"]).total_seconds() * 1000
            if elapsed < self.entry_delay_ms * 5:  # 5x delay window to capture entry
                return existing

        # Detect new cascade
        cascade_detected = False
        cascade_direction = None
        cascade_strength = Decimal(0)

        # Method 1: Price spike detection
        price_history = self._price_history.get(symbol, [])
        if len(price_history) >= 5:
            oldest_price = price_history[0][1]
            price_change_pct = (current_price - oldest_price) / oldest_price

            if abs(price_change_pct) >= self.price_spike_threshold_pct:
                cascade_detected = True
                cascade_direction = Side.LONG if price_change_pct < 0 else Side.SHORT  # Counter-trend
                cascade_strength = abs(price_change_pct)

                logger.info(
                    f"Liquidation cascade detected: {symbol} | "
                    f"Price change: {price_change_pct:.4%} | "
                    f"Counter-trade: {cascade_direction.value}"
                )

        # Method 2: OI drop detection (if data available)
        oi_history = self._oi_history.get(symbol, [])
        if len(oi_history) >= 2:
            oldest_oi = oi_history[0][1]
            current_oi = oi_history[-1][1]

            if oldest_oi > 0:
                oi_change_pct = (current_oi - oldest_oi) / oldest_oi

                # OI drop = liquidations occurred
                if oi_change_pct <= -self.oi_spike_threshold_pct:
                    cascade_detected = True
                    # Determine direction from price movement
                    if len(price_history) >= 2:
                        if price_history[-1][1] < price_history[0][1]:
                            cascade_direction = Side.LONG  # Price dropped -> Long for bounce
                        else:
                            cascade_direction = Side.SHORT  # Price spiked -> Short for reversal

                    cascade_strength = max(cascade_strength, abs(oi_change_pct))

                    logger.info(
                        f"OI drop cascade: {symbol} | "
                        f"OI change: {oi_change_pct:.4%}"
                    )

        if cascade_detected and cascade_direction:
            cascade_info = {
                "symbol": symbol,
                "direction": cascade_direction,
                "strength": cascade_strength,
                "detected_at": now,
                "entry_price": current_price,
            }
            self._cascade_detected[symbol] = cascade_info
            return cascade_info

        return None

    def _evaluate_cascade_entry(
        self,
        symbol: str,
        cascade: dict,
        current_price: Decimal,
        context: MarketContext,
    ) -> Optional[ProposedTrade]:
        """
        Evaluate entry after cascade detection.

        Wait for exhaustion signal before entering.
        """
        now = datetime.now(timezone.utc)
        detected_at = cascade["detected_at"]
        elapsed_ms = (now - detected_at).total_seconds() * 1000

        # Wait for minimum delay (let cascade exhaust)
        if elapsed_ms < self.entry_delay_ms:
            return None

        # Check for exhaustion signals
        if not self._is_cascade_exhausted(symbol, cascade, current_price):
            # Cascade still active, wait
            if elapsed_ms < self.entry_delay_ms * 3:
                return None
            # Timeout - cascade too long, skip
            if symbol in self._cascade_detected:
                del self._cascade_detected[symbol]
            return None

        # Exhaustion confirmed - enter counter-trend
        side = cascade["direction"]
        strength = cascade["strength"]

        # Calculate confidence based on cascade strength
        confidence = min(Decimal("0.8"), Decimal("0.5") + strength * Decimal("5"))

        logger.info(
            f"Liquidation Snipe Entry: {symbol} {side.value} | "
            f"Cascade strength: {strength:.4%} | "
            f"Price: {current_price}"
        )

        # Clear cascade detection
        if symbol in self._cascade_detected:
            del self._cascade_detected[symbol]

        self._last_liquidation_signal[symbol] = now

        return self.create_hft_proposal(
            symbol=symbol,
            side=side,
            entry_price=current_price,
            context=context,
            confidence=confidence,
            reason=f"Liquidation cascade bounce ({strength:.4%} move)",
        )

    def _is_cascade_exhausted(
        self,
        symbol: str,
        cascade: dict,
        current_price: Decimal,
    ) -> bool:
        """
        Check if the cascade has exhausted and reversal starting.

        Exhaustion signals:
        - Price starting to reverse from cascade direction
        - Volume decreasing
        - Rate of price change slowing
        """
        price_history = self._price_history.get(symbol, [])
        if len(price_history) < 3:
            return True  # Not enough data, allow entry

        # Check last few price points for reversal
        recent_prices = [p for _, p in price_history[-3:]]

        cascade_direction = cascade["direction"]

        if cascade_direction == Side.LONG:
            # We want to go long after a drop
            # Exhaustion = price stopped dropping, starting to rise
            return recent_prices[-1] > recent_prices[-2]
        else:
            # We want to short after a spike
            # Exhaustion = price stopped rising, starting to drop
            return recent_prices[-1] < recent_prices[-2]

    def _create_close_proposal(
        self,
        symbol: str,
        position: Position,
        context: MarketContext,
    ) -> ProposedTrade:
        """Create proposal to close position."""
        close_side = Side.SHORT if position.side == Side.LONG else Side.LONG

        return ProposedTrade(
            strategy_id=self.strategy_id,
            symbol=symbol,
            side=close_side,
            notional_usd=position.notional_value,
            risk_per_trade=Decimal(0),
            confidence=Decimal("1.0"),
            reason="[Liquidation Snipe] Position timeout",
            market_context=context,
        )
