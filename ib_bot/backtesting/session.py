"""
IB Backtesting - Session Calendar & Phase Detection
=====================================================

Provides:
- US market holiday calendar (CME futures, 2025-2026)
- Trading day validation
- Session phase classification from bar timestamps
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import List
from zoneinfo import ZoneInfo

from ..core.enums import SessionPhase

ET = ZoneInfo("America/New_York")

# CME futures holidays 2025-2026
# Source: CME Group holiday calendar
US_HOLIDAYS: set[date] = {
    # ---- 2025 ----
    date(2025, 1, 20),   # Martin Luther King Jr. Day
    date(2025, 2, 17),   # Presidents' Day
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 26),   # Memorial Day
    date(2025, 6, 19),   # Juneteenth
    date(2025, 7, 4),    # Independence Day
    date(2025, 9, 1),    # Labor Day
    date(2025, 11, 27),  # Thanksgiving
    date(2025, 12, 25),  # Christmas
    # ---- 2026 ----
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # Martin Luther King Jr. Day
    date(2026, 2, 16),   # Presidents' Day
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
}


def is_trading_day(d: date) -> bool:
    """Check if date is a CME equity futures trading day.

    A trading day is a weekday (Mon-Fri) that is not a CME holiday.
    """
    if d.weekday() >= 5:  # Saturday = 5, Sunday = 6
        return False
    return d not in US_HOLIDAYS


def get_trading_days(start: date, end: date) -> List[date]:
    """Get all trading days in [start, end] inclusive range.

    Args:
        start: First date to consider.
        end: Last date to consider.

    Returns:
        Sorted list of valid trading dates.
    """
    days: List[date] = []
    current = start
    while current <= end:
        if is_trading_day(current):
            days.append(current)
        current += timedelta(days=1)
    return days


def classify_bar_phase(dt: datetime) -> SessionPhase:
    """Classify a bar's session phase from an Eastern Time datetime.

    Phase boundaries (all ET):
        PRE_MARKET:      before 09:30
        OPENING_RANGE:   09:30 - 09:45
        ACTIVE_TRADING:  09:45 - 11:30
        AFTERNOON:       11:30 - 15:45
        EOD_FLATTEN:     15:45 - 16:00
        CLOSED:          16:00+

    Args:
        dt: Bar datetime (must be in ET or timezone-aware convertible to ET).

    Returns:
        SessionPhase enum value.
    """
    # Ensure we're working with ET time
    if dt.tzinfo is not None:
        dt = dt.astimezone(ET)
    t = dt.time()

    if t < time(9, 30):
        return SessionPhase.PRE_MARKET
    elif t < time(9, 45):
        return SessionPhase.OPENING_RANGE
    elif t < time(11, 30):
        return SessionPhase.ACTIVE_TRADING
    elif t < time(15, 45):
        return SessionPhase.AFTERNOON
    elif t < time(16, 0):
        return SessionPhase.EOD_FLATTEN
    else:
        return SessionPhase.CLOSED
