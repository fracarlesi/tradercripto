"""Enums for IB ORB Trading Bot.

Defines message topics, trade directions, session phases,
setup types, and kill switch statuses.
"""

from enum import Enum


class Topic(str, Enum):
    """Message bus topics for inter-service communication."""

    MARKET_DATA = "market_data"
    OPENING_RANGE = "opening_range"
    SIGNAL = "signal"
    ORDER = "order"
    FILL = "fill"
    ORDER_STATUS = "order_status"
    POSITION = "position"
    RISK = "risk"
    KILL_SWITCH = "kill_switch"
    NOTIFICATION = "notification"


class Direction(str, Enum):
    """Trade direction."""

    LONG = "long"
    SHORT = "short"


class SessionPhase(str, Enum):
    """Trading session phases for futures markets."""

    PRE_MARKET = "pre_market"
    OPENING_RANGE = "opening_range"
    ACTIVE_TRADING = "active_trading"
    AFTERNOON = "afternoon"
    EOD_FLATTEN = "eod_flatten"
    CLOSED = "closed"


class SetupType(str, Enum):
    """Type of ORB trade setup."""

    ORB_LONG = "orb_long"
    ORB_SHORT = "orb_short"


class KillSwitchStatus(str, Enum):
    """Kill switch status levels."""

    ACTIVE = "active"
    HALTED = "halted"
