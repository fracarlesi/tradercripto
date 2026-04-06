"""Enums for HLQuantBot conservative refactor."""

from enum import Enum, auto


class Topic(str, Enum):
    """Message bus topics - simplified from original 8 to 8 focused topics."""

    MARKET_STATE = "market_state"      # OHLCV + indicators per asset
    REGIME = "regime"                   # TREND/RANGE/CHAOS detection
    SETUPS = "setups"                   # Trade setup candidates
    TRADE_INTENT = "trade_intent"       # Sized and approved trade
    ORDERS = "orders"                   # Orders sent to exchange
    FILLS = "fills"                     # Executed orders
    RISK_ALERTS = "risk_alerts"         # Kill-switch, warnings
    METRICS = "metrics"                 # Performance metrics

    # Legacy topics (kept for backward compatibility with older tests/services)
    MARKET_DATA = "market_data"
    OPPORTUNITIES = "opportunities"
    SIGNALS = "signals"
    SIZED_SIGNALS = "sized_signals"
    CONFIG_UPDATES = "config_updates"

    def __str__(self) -> str:
        return self.value


class OrderType(str, Enum):
    """Order types supported by execution engine."""

    LIMIT_POST_ONLY = "limit_post_only"  # Preferred for entry
    MARKET = "market"                     # Fallback with slippage check
    STOP_MARKET = "stop_market"           # Server-side stop loss
    TRAILING_STOP = "trailing_stop"       # Bot-managed trailing


class OrderStatus(str, Enum):
    """Order lifecycle status."""

    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class TimeFrame(str, Enum):
    """Supported timeframes."""

    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
