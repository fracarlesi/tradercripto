"""Enumerations for HLQuantBot."""

from enum import Enum, auto


class Environment(str, Enum):
    """Trading environment."""
    TESTNET = "testnet"
    PRODUCTION = "production"


class Side(str, Enum):
    """Trade side."""
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"  # No position / close


class OrderType(str, Enum):
    """Order type."""
    MARKET = "market"
    LIMIT = "limit"
    LIMIT_IOC = "limit_ioc"  # Immediate or cancel
    LIMIT_GTX = "limit_gtx"  # Post-only (maker)


class OrderStatus(str, Enum):
    """Order lifecycle status."""
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class PositionStatus(str, Enum):
    """Position lifecycle status."""
    OPENING = "opening"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


class StrategyId(str, Enum):
    """Strategy identifiers."""
    # Legacy strategies (to be removed in Phase C)
    FUNDING_BIAS = "funding_bias"
    LIQUIDATION_CLUSTER = "liquidation_cluster"
    VOLATILITY_EXPANSION = "volatility_expansion"

    # HFT strategies (Phase C)
    MMR_HFT = "mmr_hft"                    # Micro Mean Reversion 1-5s
    MICRO_BREAKOUT = "micro_breakout"      # Micro-Breakout 3-15s
    PAIR_TRADING = "pair_trading"          # Long strongest / Short weakest
    LIQUIDATION_SNIPING = "liquidation_sniping"  # Liquidation cascade trading
    MOMENTUM_SCALPING = "momentum_scalping"  # Trend following scalping


class MarketRegime(str, Enum):
    """Market regime classification."""
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGE_BOUND = "range_bound"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"
    UNCERTAIN = "uncertain"


class AlertSeverity(str, Enum):
    """Alert severity levels."""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    EMERGENCY = "emergency"


class ExitReason(str, Enum):
    """Reason for closing a position."""
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    TRAILING_STOP = "trailing_stop"
    SIGNAL_EXIT = "signal_exit"
    RISK_LIMIT = "risk_limit"
    CIRCUIT_BREAKER = "circuit_breaker"
    MANUAL = "manual"
    LIQUIDATION = "liquidation"


class TimeFrame(str, Enum):
    """Candle timeframes."""
    # Sub-second (HFT)
    S1 = "1s"
    S3 = "3s"
    S5 = "5s"
    S15 = "15s"
    S30 = "30s"
    # Minutes
    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    # Hours
    H1 = "1h"
    H2 = "2h"
    H4 = "4h"
    H8 = "8h"
    H12 = "12h"
    # Days+
    D1 = "1d"
    D3 = "3d"
    W1 = "1w"
    MO1 = "1M"


# Mapping timeframe to seconds for easy conversion
TIMEFRAME_SECONDS = {
    TimeFrame.S1: 1,
    TimeFrame.S3: 3,
    TimeFrame.S5: 5,
    TimeFrame.S15: 15,
    TimeFrame.S30: 30,
    TimeFrame.M1: 60,
    TimeFrame.M3: 180,
    TimeFrame.M5: 300,
    TimeFrame.M15: 900,
    TimeFrame.M30: 1800,
    TimeFrame.H1: 3600,
    TimeFrame.H2: 7200,
    TimeFrame.H4: 14400,
    TimeFrame.H8: 28800,
    TimeFrame.H12: 43200,
    TimeFrame.D1: 86400,
    TimeFrame.D3: 259200,
    TimeFrame.W1: 604800,
    TimeFrame.MO1: 2592000,
}
