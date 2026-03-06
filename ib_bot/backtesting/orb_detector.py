"""
IB Backtesting - Opening Range Breakout Detector
==================================================

Detects the Opening Range from historical 1-min bars.
Reuses the same logic as MarketDataService._calculate_and_publish_or()
to ensure backtest/live parity.
"""

from __future__ import annotations

import logging
from datetime import time, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from ..core.contracts import FuturesSpec
from ..core.models import ORBRange
from .config import IBBacktestConfig

logger = logging.getLogger(__name__)


def detect_opening_range(
    bars: List[Dict[str, Any]],
    spec: FuturesSpec,
    cfg: IBBacktestConfig,
) -> Optional[ORBRange]:
    """Detect Opening Range from a day's 1-min bars.

    Mirrors the logic in MarketDataService._calculate_and_publish_or()
    to ensure backtest results match live behavior.

    Args:
        bars: List of bar dicts with keys:
              {"dt": datetime(ET), "o": Decimal, "h": Decimal,
               "l": Decimal, "c": Decimal, "v": Decimal}
        spec: Futures contract specification (tick_size, symbol, etc.).
        cfg: Backtest configuration (OR window times, range limits).

    Returns:
        ORBRange if OR window bars exist, None otherwise.
        The ``valid`` flag indicates whether range_ticks falls
        within [min_range_ticks, max_range_ticks].
    """
    or_start = time.fromisoformat(cfg.or_start)
    or_end = time.fromisoformat(cfg.or_end)

    # Filter bars within the Opening Range window
    or_bars = [b for b in bars if or_start <= b["dt"].time() < or_end]
    if not or_bars:
        return None

    # High / Low / Midpoint
    or_high: Decimal = max(b["h"] for b in or_bars)
    or_low: Decimal = min(b["l"] for b in or_bars)
    midpoint: Decimal = (or_high + or_low) / 2
    total_volume: Decimal = sum((b["v"] for b in or_bars), Decimal("0"))
    range_ticks: int = int((or_high - or_low) / spec.tick_size)

    # VWAP over the OR window
    cum_tp_vol = Decimal("0")
    cum_vol = Decimal("0")
    for b in or_bars:
        typical = (b["h"] + b["l"] + b["c"]) / 3
        cum_tp_vol += typical * b["v"]
        cum_vol += b["v"]
    vwap = cum_tp_vol / cum_vol if cum_vol > 0 else midpoint

    # Validate range size
    valid = cfg.min_range_ticks <= range_ticks <= cfg.max_range_ticks

    or_range = ORBRange(
        symbol=spec.symbol,
        or_high=or_high,
        or_low=or_low,
        midpoint=midpoint,
        range_ticks=range_ticks,
        volume=total_volume,
        vwap=vwap,
        timestamp=or_bars[-1]["dt"].replace(tzinfo=timezone.utc)
        if or_bars[-1]["dt"].tzinfo is None
        else or_bars[-1]["dt"],
        valid=valid,
    )

    if valid:
        logger.debug(
            "OR detected: %s high=%.2f low=%.2f range=%d ticks vwap=%.2f",
            spec.symbol,
            float(or_high),
            float(or_low),
            range_ticks,
            float(vwap),
        )
    else:
        logger.debug(
            "OR invalid: %s range=%d ticks (min=%d, max=%d)",
            spec.symbol,
            range_ticks,
            cfg.min_range_ticks,
            cfg.max_range_ticks,
        )

    return or_range
