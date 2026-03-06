"""
Filtered ORB Backtest Simulator
================================

Extends ORBSimulator with pre-trade filters to skip low-quality setups:
  1. EMA Trend Filter  - only trade in trend direction
  2. ATR Percentile    - skip days with extreme volatility
  3. VWAP Slope        - skip range-bound / flat days
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import time
from decimal import Decimal
from typing import Any, Dict, List, Optional

from ..core.contracts import FuturesSpec
from ..core.enums import Direction
from ..strategies.orb import ORBStrategy
from ..services.market_data import VWAPCalculator, ATRCalculator
from .config import IBBacktestConfig
from .simulator import DailyRiskState, ORBSimulator

logger = logging.getLogger(__name__)


class FilteredORBSimulator(ORBSimulator):
    """ORBSimulator with pre-trade regime/volatility filters.

    Overrides _simulate_symbol_day to apply:
      - EMA trend filter (skip counter-trend entries)
      - ATR percentile filter (skip extreme-volatility days)
      - VWAP slope filter (skip flat/range-bound days)
    """

    def __init__(self, cfg: IBBacktestConfig) -> None:
        super().__init__(cfg)
        # Collect historical ATR values for percentile calculation
        self._atr_history: deque[float] = deque(maxlen=500)
        self._filter_stats: Dict[str, int] = {
            "ema_filtered": 0,
            "atr_filtered": 0,
            "vwap_filtered": 0,
            "passed": 0,
        }

    def _compute_ema(
        self,
        closes: List[Decimal],
        period: int,
    ) -> Optional[Decimal]:
        """Compute EMA over a list of close prices.

        Returns None if not enough data.
        """
        if len(closes) < period:
            return None

        # Use the last `period` closes
        data = closes[-period:]
        multiplier = Decimal("2") / (Decimal(str(period)) + Decimal("1"))

        ema = data[0]
        for price in data[1:]:
            ema = (price - ema) * multiplier + ema
        return ema

    def _compute_vwap_slope_ticks(
        self,
        bars: List[Dict[str, Any]],
        spec: FuturesSpec,
        or_end_time: time,
    ) -> Optional[Decimal]:
        """Compute VWAP slope between bar 5 and OR end.

        Returns slope in ticks, or None if insufficient data.
        """
        vwap_calc = VWAPCalculator()
        vwap_at_bar5: Optional[Decimal] = None
        vwap_at_or_end: Optional[Decimal] = None
        bar_count = 0

        for bar in bars:
            h = Decimal(str(bar["h"])) if not isinstance(bar["h"], Decimal) else bar["h"]
            l = Decimal(str(bar["l"])) if not isinstance(bar["l"], Decimal) else bar["l"]
            c = Decimal(str(bar["c"])) if not isinstance(bar["c"], Decimal) else bar["c"]
            v = Decimal(str(bar["v"])) if not isinstance(bar["v"], Decimal) else bar["v"]

            vwap = vwap_calc.update(h, l, c, v)
            bar_count += 1

            if bar_count == 5:
                vwap_at_bar5 = vwap

            t = bar["dt"].time()
            if t >= or_end_time:
                vwap_at_or_end = vwap
                break

        if vwap_at_bar5 is None or vwap_at_or_end is None:
            return None

        slope_price = abs(vwap_at_or_end - vwap_at_bar5)
        slope_ticks = slope_price / spec.tick_size if spec.tick_size > 0 else Decimal("0")
        return slope_ticks

    def _compute_or_atr(
        self,
        bars: List[Dict[str, Any]],
        or_end_time: time,
    ) -> Optional[Decimal]:
        """Compute ATR-14 from bars up to OR end."""
        atr_calc = ATRCalculator(period=14)
        atr_value: Optional[Decimal] = None

        for bar in bars:
            h = Decimal(str(bar["h"])) if not isinstance(bar["h"], Decimal) else bar["h"]
            l = Decimal(str(bar["l"])) if not isinstance(bar["l"], Decimal) else bar["l"]
            c = Decimal(str(bar["c"])) if not isinstance(bar["c"], Decimal) else bar["c"]

            atr_value = atr_calc.update(h, l, c)

            t = bar["dt"].time()
            if t >= or_end_time:
                break

        return atr_value

    def _simulate_symbol_day(
        self,
        symbol: str,
        bars: List[Dict[str, Any]],
        spec: FuturesSpec,
        strategy: ORBStrategy,
        or_detector_func: Any,
        risk: DailyRiskState,
    ) -> List[Dict[str, Any]]:
        """Simulate one symbol for one day, with pre-trade filters."""

        cfg = self.cfg
        or_end_time = time.fromisoformat(cfg.or_end)

        # ── Filter 1: VWAP Slope ──
        if cfg.vwap_slope_filter:
            slope = self._compute_vwap_slope_ticks(bars, spec, or_end_time)
            if slope is not None and slope < Decimal(str(cfg.vwap_min_slope_ticks)):
                self._filter_stats["vwap_filtered"] += 1
                logger.debug(
                    "VWAP slope filter: %s skipped (slope=%.2f ticks < %.1f)",
                    symbol, float(slope), cfg.vwap_min_slope_ticks,
                )
                return []

        # ── Filter 2: ATR Percentile ──
        if cfg.atr_percentile_filter:
            or_atr = self._compute_or_atr(bars, or_end_time)
            if or_atr is not None and or_atr > 0:
                atr_f = float(or_atr)
                self._atr_history.append(atr_f)

                # Need at least 10 days of history for percentile
                if len(self._atr_history) >= 10:
                    sorted_atrs = sorted(self._atr_history)
                    n = len(sorted_atrs)
                    low_idx = int(n * cfg.atr_low_pct / 100.0)
                    high_idx = int(n * cfg.atr_high_pct / 100.0)

                    low_threshold = sorted_atrs[low_idx]
                    high_threshold = sorted_atrs[min(high_idx, n - 1)]

                    if atr_f < low_threshold or atr_f > high_threshold:
                        self._filter_stats["atr_filtered"] += 1
                        logger.debug(
                            "ATR filter: %s skipped (ATR=%.4f, range=[%.4f, %.4f])",
                            symbol, atr_f, low_threshold, high_threshold,
                        )
                        return []

        # ── Filter 3: EMA Trend (applied per-trade, not per-day) ──
        # We compute EMA here and pass it down by temporarily monkey-patching
        # the strategy evaluate. Instead, we override the entry logic below.
        ema_value: Optional[Decimal] = None
        if cfg.ema_trend_filter:
            # Collect first 20 close prices (pre-OR + OR bars)
            closes: List[Decimal] = []
            for bar in bars:
                c = Decimal(str(bar["c"])) if not isinstance(bar["c"], Decimal) else bar["c"]
                closes.append(c)
                t = bar["dt"].time()
                if t >= or_end_time and len(closes) >= cfg.ema_period:
                    break

            ema_value = self._compute_ema(closes, cfg.ema_period)

        self._filter_stats["passed"] += 1

        # If no EMA filter, delegate entirely to parent
        if not cfg.ema_trend_filter or ema_value is None:
            return super()._simulate_symbol_day(
                symbol, bars, spec, strategy, or_detector_func, risk,
            )

        # With EMA filter: run parent logic but wrap to filter entries
        return self._simulate_symbol_day_with_ema(
            symbol, bars, spec, strategy, or_detector_func, risk, ema_value,
        )

    def _simulate_symbol_day_with_ema(
        self,
        symbol: str,
        bars: List[Dict[str, Any]],
        spec: FuturesSpec,
        strategy: ORBStrategy,
        or_detector_func: Any,
        risk: DailyRiskState,
        ema_value: Decimal,
    ) -> List[Dict[str, Any]]:
        """Simulate with EMA trend filter on entries.

        Duplicates parent _simulate_symbol_day logic but adds EMA check
        before opening positions.
        """
        from .simulator import calculate_pnl, size_trade

        trades: List[Dict[str, Any]] = []

        # Phase 1: Detect Opening Range
        or_range = or_detector_func(bars, spec, self.cfg)
        if or_range is None or not or_range.valid:
            return trades

        # Phase 2: Walk bars
        vwap_calc = VWAPCalculator()
        atr_calc = ATRCalculator()

        max_entry = time.fromisoformat(self.cfg.max_entry_time)
        eod_flatten = time.fromisoformat(self.cfg.eod_flatten_time)
        or_end = time.fromisoformat(self.cfg.or_end)

        from ..core.enums import SessionPhase
        from ..core.models import FuturesMarketState

        for bar in bars:
            h = Decimal(str(bar["h"])) if not isinstance(bar["h"], Decimal) else bar["h"]
            l = Decimal(str(bar["l"])) if not isinstance(bar["l"], Decimal) else bar["l"]
            c = Decimal(str(bar["c"])) if not isinstance(bar["c"], Decimal) else bar["c"]
            v = Decimal(str(bar["v"])) if not isinstance(bar["v"], Decimal) else bar["v"]

            vwap = vwap_calc.update(h, l, c, v)
            atr = atr_calc.update(h, l, c)

            bar_time = bar["dt"]
            t = bar_time.time()

            # --- Check exits FIRST ---
            if risk.has_position(symbol):
                exit_result = self._check_exit(risk.positions[symbol], bar, spec)
                if exit_result:
                    trade = risk.close_position(
                        symbol, exit_result["price"], exit_result["reason"],
                        bar_time, spec, self.cfg,
                    )
                    trades.append(trade)
                elif t >= eod_flatten and risk.has_position(symbol):
                    trade = risk.close_position(
                        symbol, c, "EOD", bar_time, spec, self.cfg,
                    )
                    trades.append(trade)

            # --- Check entries ---
            if t < or_end or t >= max_entry:
                continue
            if risk.has_position(symbol):
                continue
            if not risk.can_trade(symbol, Direction.LONG) and not risk.can_trade(symbol, Direction.SHORT):
                continue

            state = FuturesMarketState(
                symbol=symbol,
                last_price=c,
                vwap=vwap,
                atr_14=atr,
                volume=v,
                session_phase=SessionPhase.ACTIVE_TRADING,
                timestamp=bar_time,
            )

            result = strategy.evaluate(state, or_range)
            if result.has_setup and result.setup:
                setup = result.setup

                # ── EMA TREND FILTER ──
                if setup.direction == Direction.LONG and c < ema_value:
                    logger.debug(
                        "EMA filter: LONG %s rejected (price=%.2f < EMA=%.2f)",
                        symbol, float(c), float(ema_value),
                    )
                    self._filter_stats["ema_filtered"] += 1
                    continue
                if setup.direction == Direction.SHORT and c > ema_value:
                    logger.debug(
                        "EMA filter: SHORT %s rejected (price=%.2f > EMA=%.2f)",
                        symbol, float(c), float(ema_value),
                    )
                    self._filter_stats["ema_filtered"] += 1
                    continue

                if risk.can_trade(symbol, setup.direction):
                    contracts = size_trade(setup, spec, self.cfg)
                    if contracts > 0:
                        slip = Decimal(str(self.cfg.slippage_ticks)) * spec.tick_size
                        if setup.direction == Direction.LONG:
                            adj_entry = setup.entry_price + slip
                        else:
                            adj_entry = setup.entry_price - slip

                        risk.open_position(
                            symbol=symbol,
                            direction=setup.direction,
                            entry_price=adj_entry,
                            stop_price=setup.stop_price,
                            target_price=setup.target_price,
                            contracts=contracts,
                            bar_time=bar_time,
                            risk_ticks=setup.risk_ticks,
                        )

                        logger.debug(
                            "ENTRY: %s %s @ %.2f (SL=%.2f TP=%.2f) x%d [EMA=%.2f]",
                            setup.direction.value, symbol,
                            float(adj_entry), float(setup.stop_price),
                            float(setup.target_price), contracts,
                            float(ema_value),
                        )

        return trades

    def get_filter_stats(self) -> Dict[str, int]:
        """Return filter statistics for reporting."""
        return dict(self._filter_stats)
