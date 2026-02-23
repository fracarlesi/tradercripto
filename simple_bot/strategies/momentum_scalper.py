"""
Momentum Scalper Strategy - EMA9/EMA21 Crossover
====================================================

Aggressive strategy using fast EMA crossover with RSI filter
for BTC scalping on 15m timeframe.

Entry conditions:
- LONG: EMA9 > EMA21 + RSI(14) between 30-65
- SHORT: EMA9 < EMA21 + RSI(14) between 40-70
- Volatility filter: ATR% > min_atr_pct

Exit:
- Fixed TP/SL (default 0.8% TP, 0.4% SL = 1:2 R:R)

No regime restriction - trades in all market conditions.
No engulfing candle confirmation required.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal

from .base import BaseStrategy, StrategyResult
from ..core.models import MarketState, Setup, Regime, Direction, SetupType


logger = logging.getLogger(__name__)


class MomentumScalperStrategy(BaseStrategy):
    """
    EMA9/EMA21 momentum crossover strategy.

    Designed for aggressive BTC scalping on 15m timeframe
    with fixed percentage TP/SL.
    """

    def __init__(self, config: dict = None):
        super().__init__(config)

        self.allow_short = self.config.get("allow_short", True)
        self.min_atr_pct = self.config.get("min_atr_pct", 0.1)
        self.stop_loss_pct = self.config.get("stop_loss_pct", 0.8)
        self.take_profit_pct = self.config.get("take_profit_pct", 1.6)
        self._min_volume_usd = Decimal(str(self.config.get("min_volume_usd", 20000)))

        # RSI thresholds
        self.rsi_long_min = self.config.get("rsi_long_min", 30)
        self.rsi_long_max = self.config.get("rsi_long_max", 65)
        self.rsi_short_min = self.config.get("rsi_short_min", 40)
        self.rsi_short_max = self.config.get("rsi_short_max", 70)

        self._logger.info(
            "MomentumScalper initialized: SL=%.1f%%, TP=%.1f%%, short=%s, min_atr=%.2f%%",
            self.stop_loss_pct,
            self.take_profit_pct,
            self.allow_short,
            self.min_atr_pct,
        )

    @property
    def name(self) -> str:
        return "trend_momentum"

    @property
    def required_regime(self) -> Regime:
        return Regime.TREND

    def can_trade(self, state: MarketState) -> bool:
        """Only trade in TREND regime - confirmed by backtest results."""
        return state.regime == Regime.TREND

    def evaluate(self, state: MarketState) -> StrategyResult:
        """
        Evaluate market state for EMA crossover setup.

        LONG: EMA9 > EMA21 + RSI in [30, 65]
        SHORT: EMA9 < EMA21 + RSI in [40, 70]
        """
        # Check EMA9/EMA21 are available
        if state.ema9 is None or state.ema21 is None:
            return self.reject("EMA9/EMA21 not available")

        # Check minimum volatility
        if float(state.atr_pct) < self.min_atr_pct:
            return self.reject(
                f"ATR too low: {state.atr_pct:.3f}% < {self.min_atr_pct}%"
            )

        # Volume filter (hard floor)
        if state.volume_usd is not None and state.volume_usd < self._min_volume_usd:
            self._logger.info(
                "VOLUME_FILTER | BLOCKED | %s | vol_usd=$%.0f | min=$%.0f",
                state.symbol, float(state.volume_usd), float(self._min_volume_usd),
            )
            return StrategyResult(
                has_setup=False,
                reason=f"Volume too low: ${float(state.volume_usd):,.0f} < ${float(self._min_volume_usd):,.0f} min",
            )

        if state.volume_usd is not None:
            self._logger.debug(
                "VOLUME_FILTER | PASSED | %s | vol_usd=$%.0f | min=$%.0f",
                state.symbol, float(state.volume_usd), float(self._min_volume_usd),
            )

        # Determine direction
        direction = self._determine_direction(state)
        if direction == Direction.FLAT:
            return self.reject("No EMA crossover signal")

        if direction == Direction.SHORT and not self.allow_short:
            return self.reject("Short positions disabled")

        # Check RSI filter
        if not self._check_rsi(state, direction):
            rsi_val = float(state.rsi)
            if direction == Direction.LONG:
                return self.reject(
                    f"RSI {rsi_val:.1f} outside LONG range [{self.rsi_long_min}-{self.rsi_long_max}]"
                )
            return self.reject(
                f"RSI {rsi_val:.1f} outside SHORT range [{self.rsi_short_min}-{self.rsi_short_max}]"
            )

        # Calculate fixed % stop price
        entry_price = state.close
        stop_price = self._calculate_fixed_stop(entry_price, direction)
        stop_distance_pct = Decimal(str(self.stop_loss_pct))

        # Pre-validate: reject if stop distance is too small for price precision.
        # Hyperliquid rounds prices to 4 decimals + 5 sig figs. If SL would
        # collapse to entry after rounding, the trade is unprotectable.
        min_tick = self._estimate_min_tick(entry_price)
        if abs(entry_price - stop_price) < min_tick * 2:
            return self.reject(
                f"Stop distance too small for price precision: "
                f"|{float(entry_price):.6f} - {float(stop_price):.6f}| < "
                f"2 * tick({float(min_tick):.6f})"
            )

        # Quality score
        quality = self._calculate_quality(state, direction)

        setup = Setup(
            id=self.generate_setup_id(),
            symbol=state.symbol,
            timestamp=datetime.now(timezone.utc),
            setup_type=SetupType.MOMENTUM,
            direction=direction,
            regime=state.regime,
            entry_price=entry_price,
            stop_price=stop_price,
            stop_distance_pct=stop_distance_pct,
            atr=state.atr,
            adx=state.adx,
            rsi=state.rsi,
            setup_quality=quality,
            confidence=quality,
        )

        self._logger.info(
            "SETUP: %s %s @ %.2f (EMA9=%.2f, EMA21=%.2f, RSI=%.1f), "
            "stop=%.2f (%.2f%%), quality=%.2f",
            direction.value.upper(),
            state.symbol,
            float(entry_price),
            float(state.ema9 or 0),
            float(state.ema21 or 0),
            float(state.rsi),
            float(stop_price),
            float(stop_distance_pct),
            float(quality),
        )

        return StrategyResult(
            has_setup=True,
            setup=setup,
            reason=(
                f"EMA Momentum: EMA9 {'>' if direction == Direction.LONG else '<'} EMA21, "
                f"RSI={float(state.rsi):.1f}"
            ),
        )

    def _determine_direction(self, state: MarketState) -> Direction:
        """Determine direction from EMA9/EMA21 crossover."""
        ema9 = state.ema9
        ema21 = state.ema21

        if ema9 is None or ema21 is None:
            return Direction.FLAT

        if ema9 > ema21:
            return Direction.LONG
        if ema9 < ema21:
            return Direction.SHORT

        return Direction.FLAT

    def _check_rsi(self, state: MarketState, direction: Direction) -> bool:
        """Check RSI is in acceptable range for the direction."""
        rsi = float(state.rsi)

        if direction == Direction.LONG:
            return self.rsi_long_min <= rsi <= self.rsi_long_max
        else:
            return self.rsi_short_min <= rsi <= self.rsi_short_max

    def _calculate_fixed_stop(
        self, entry_price: Decimal, direction: Direction
    ) -> Decimal:
        """Calculate stop price using fixed percentage."""
        sl_mult = Decimal(str(self.stop_loss_pct)) / Decimal("100")

        if direction == Direction.LONG:
            return entry_price * (Decimal("1") - sl_mult)
        else:
            return entry_price * (Decimal("1") + sl_mult)

    @staticmethod
    def _estimate_min_tick(price: Decimal) -> Decimal:
        """Estimate minimum price increment for Hyperliquid rounding.

        Mirrors _round_price logic: 4 decimal places + 5 significant figures.
        Returns the smallest meaningful price change for this price level.
        """
        from math import log10, floor

        p = float(price)
        if p <= 0:
            return Decimal("0.0001")
        magnitude = floor(log10(p))
        max_decimals = min(4, max(0, 4 - magnitude))
        return Decimal(str(10 ** (-max_decimals)))

    def _calculate_quality(
        self, state: MarketState, direction: Direction
    ) -> Decimal:
        """
        Calculate setup quality score 0-1.

        Factors:
        - EMA separation strength
        - RSI positioning (closer to neutral = better)
        - ATR strength
        """
        score = Decimal("0.5")

        # EMA separation bonus (max +0.2)
        ema9_val = float(state.ema9) if state.ema9 is not None else 0.0
        ema21_val = float(state.ema21) if state.ema21 is not None else 1.0
        ema_diff_pct = (
            abs(ema9_val - ema21_val)
            / ema21_val
            * 100
        ) if ema21_val > 0 else 0.0
        ema_bonus = min(0.2, ema_diff_pct / 2)
        score += Decimal(str(round(ema_bonus, 4)))

        # RSI positioning bonus (max +0.15)
        rsi = float(state.rsi)
        if direction == Direction.LONG:
            # Best RSI for longs: 40-55 (room to grow, not overbought)
            if 40 <= rsi <= 55:
                score += Decimal("0.15")
            elif 30 <= rsi < 40 or 55 < rsi <= 65:
                score += Decimal("0.08")
        else:
            # Best RSI for shorts: 45-60
            if 45 <= rsi <= 60:
                score += Decimal("0.15")
            elif 40 <= rsi < 45 or 60 < rsi <= 70:
                score += Decimal("0.08")

        # ATR bonus (max +0.15)
        atr_pct = float(state.atr_pct)
        if atr_pct > 0.3:
            score += Decimal("0.15")
        elif atr_pct > 0.2:
            score += Decimal("0.10")
        elif atr_pct > 0.1:
            score += Decimal("0.05")

        quality = float(min(Decimal("1.0"), max(Decimal("0.0"), score)))

        # Volume ratio quality adjustment
        if state.volume_ratio is not None and state.volume_ratio < Decimal("0.7"):
            vol_penalty = float(state.volume_ratio) / 0.7  # e.g., ratio 0.5 -> penalty 0.71
            quality *= vol_penalty

        return Decimal(str(round(max(0.0, min(1.0, quality)), 4)))
