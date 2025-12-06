"""Data models for HLQuantBot."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional, List, Dict, Any

from .enums import (
    Side,
    OrderType,
    OrderStatus,
    PositionStatus,
    StrategyId,
    MarketRegime,
    ExitReason,
    TimeFrame,
)


@dataclass
class Tick:
    """Real-time price tick."""
    symbol: str
    timestamp: datetime
    mid_price: Decimal
    best_bid: Decimal
    best_ask: Decimal
    last_price: Optional[Decimal] = None
    volume_24h: Optional[Decimal] = None

    @property
    def spread(self) -> Decimal:
        return self.best_ask - self.best_bid

    @property
    def spread_bps(self) -> Decimal:
        """Spread in basis points."""
        if self.mid_price == 0:
            return Decimal(0)
        return (self.spread / self.mid_price) * 10000


@dataclass
class Bar:
    """OHLCV candle bar."""
    symbol: str
    timeframe: TimeFrame
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trades_count: Optional[int] = None

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def body_size(self) -> Decimal:
        return abs(self.close - self.open)

    @property
    def range(self) -> Decimal:
        return self.high - self.low

    @property
    def body_ratio(self) -> Decimal:
        """Body size relative to total range."""
        if self.range == 0:
            return Decimal(0)
        return self.body_size / self.range


@dataclass
class MarketContext:
    """Aggregated market data for a symbol."""
    symbol: str
    timestamp: datetime

    # Prices
    mark_price: Decimal
    index_price: Decimal
    mid_price: Decimal

    # Funding
    funding_rate: Decimal

    # Open Interest
    open_interest: Decimal

    # Volume
    volume_24h: Decimal

    # Optional fields (must come after required fields)
    predicted_funding: Optional[Decimal] = None
    open_interest_change_1h: Optional[Decimal] = None
    volume_1h: Optional[Decimal] = None

    # Order book
    bid_depth: Optional[Decimal] = None  # Total bid liquidity
    ask_depth: Optional[Decimal] = None  # Total ask liquidity

    # Volatility
    atr_14: Optional[Decimal] = None
    volatility_1h: Optional[Decimal] = None

    # Order book spread
    spread: Optional[Decimal] = None  # best_ask - best_bid

    @property
    def current_price(self) -> Decimal:
        """Alias for mid_price for backward compatibility with HFT strategies."""
        return self.mid_price


@dataclass
class ProposedTrade:
    """Trade proposal from a strategy."""
    strategy_id: StrategyId
    symbol: str
    side: Side

    # Entry
    entry_type: OrderType = OrderType.MARKET
    entry_price: Optional[Decimal] = None  # For limit orders

    # Risk parameters
    notional_usd: Decimal = Decimal(0)  # Desired position size in USD
    risk_per_trade: Decimal = Decimal(0)  # Amount willing to lose (USD)

    # Exit levels
    stop_loss_price: Optional[Decimal] = None
    take_profit_price: Optional[Decimal] = None
    trailing_stop_pct: Optional[Decimal] = None

    # Metadata
    confidence: Decimal = Decimal("0.5")  # 0-1
    timestamp: datetime = field(default_factory=datetime.utcnow)
    reason: str = ""

    # Context at time of signal
    market_context: Optional[MarketContext] = None

    def __post_init__(self):
        if isinstance(self.strategy_id, str):
            self.strategy_id = StrategyId(self.strategy_id)
        if isinstance(self.side, str):
            self.side = Side(self.side)
        if isinstance(self.entry_type, str):
            self.entry_type = OrderType(self.entry_type)


@dataclass
class ApprovedOrder:
    """Order approved by Risk Engine, ready for execution."""
    # Identity
    order_id: str
    strategy_id: StrategyId

    # Order details
    symbol: str
    side: Side
    size: Decimal  # In asset units
    order_type: OrderType
    price: Optional[Decimal] = None  # For limit orders

    # Risk management
    stop_loss_price: Optional[Decimal] = None
    take_profit_price: Optional[Decimal] = None

    # Lifecycle
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime = field(default_factory=datetime.utcnow)
    valid_until: Optional[datetime] = None  # TTL

    # Execution details (filled after execution)
    filled_size: Decimal = Decimal(0)
    filled_price: Optional[Decimal] = None
    fees: Decimal = Decimal(0)
    executed_at: Optional[datetime] = None

    # Risk context
    leverage_used: Decimal = Decimal(1)
    risk_amount: Decimal = Decimal(0)  # USD at risk

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order_id": self.order_id,
            "strategy_id": self.strategy_id.value,
            "symbol": self.symbol,
            "side": self.side.value,
            "size": str(self.size),
            "order_type": self.order_type.value,
            "price": str(self.price) if self.price else None,
            "stop_loss": str(self.stop_loss_price) if self.stop_loss_price else None,
            "take_profit": str(self.take_profit_price) if self.take_profit_price else None,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class Position:
    """Active position."""
    symbol: str
    side: Side
    size: Decimal  # Absolute size in asset units
    entry_price: Decimal

    # Current state
    current_price: Decimal = Decimal(0)
    unrealized_pnl: Decimal = Decimal(0)
    unrealized_pnl_pct: Decimal = Decimal(0)

    # Risk
    leverage: Decimal = Decimal(1)
    liquidation_price: Optional[Decimal] = None
    margin_used: Decimal = Decimal(0)

    # Exit levels
    stop_loss_price: Optional[Decimal] = None
    take_profit_price: Optional[Decimal] = None
    trailing_stop_price: Optional[Decimal] = None

    # Metadata
    strategy_id: Optional[StrategyId] = None
    status: PositionStatus = PositionStatus.OPEN
    opened_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def notional_value(self) -> Decimal:
        return self.size * self.current_price

    @property
    def is_long(self) -> bool:
        return self.side == Side.LONG

    def update_pnl(self, current_price: Decimal):
        self.current_price = current_price
        if self.is_long:
            self.unrealized_pnl = (current_price - self.entry_price) * self.size
        else:
            self.unrealized_pnl = (self.entry_price - current_price) * self.size

        entry_value = self.entry_price * self.size
        if entry_value > 0:
            self.unrealized_pnl_pct = self.unrealized_pnl / entry_value


@dataclass
class ClosedTrade:
    """Completed trade record."""
    trade_id: str
    symbol: str
    side: Side
    size: Decimal

    # Prices
    entry_price: Decimal
    exit_price: Decimal

    # P&L
    pnl: Decimal
    pnl_pct: Decimal
    fees: Decimal

    # Timing
    entry_time: datetime
    exit_time: datetime

    # Context
    strategy_id: StrategyId
    exit_reason: ExitReason

    # Optional with defaults (must come last)
    funding_paid: Decimal = Decimal(0)
    duration_seconds: int = 0

    @property
    def net_pnl(self) -> Decimal:
        return self.pnl - self.fees - self.funding_paid


@dataclass
class AccountState:
    """Current account state."""
    timestamp: datetime

    # Balance
    equity: Decimal
    available_balance: Decimal
    total_margin_used: Decimal

    # Positions
    positions: List[Position] = field(default_factory=list)
    total_unrealized_pnl: Decimal = Decimal(0)

    # Daily P&L
    daily_pnl: Decimal = Decimal(0)
    daily_pnl_pct: Decimal = Decimal(0)
    daily_starting_equity: Optional[Decimal] = None

    # Leverage
    total_position_value: Decimal = Decimal(0)
    current_leverage: Decimal = Decimal(0)

    @property
    def position_count(self) -> int:
        return len(self.positions)

    def get_position(self, symbol: str) -> Optional[Position]:
        for pos in self.positions:
            if pos.symbol == symbol:
                return pos
        return None

    def get_exposure_by_symbol(self) -> Dict[str, Decimal]:
        """Get notional exposure per symbol."""
        return {pos.symbol: pos.notional_value for pos in self.positions}

    def get_exposure_by_strategy(self) -> Dict[StrategyId, Decimal]:
        """Get notional exposure per strategy."""
        exposure = {}
        for pos in self.positions:
            if pos.strategy_id:
                exposure[pos.strategy_id] = exposure.get(pos.strategy_id, Decimal(0)) + pos.notional_value
        return exposure


@dataclass
class RiskLimits:
    """Risk configuration limits."""
    # Portfolio level
    max_portfolio_leverage: Decimal = Decimal("5.0")
    max_daily_loss_pct: Decimal = Decimal("0.08")  # 8%
    max_total_drawdown_pct: Decimal = Decimal("0.35")  # 35%

    # Per trade
    max_risk_per_trade_pct: Decimal = Decimal("0.02")  # 2% of equity
    max_position_leverage: Decimal = Decimal("4.0")

    # Per asset
    max_exposure_per_asset_pct: Decimal = Decimal("0.50")  # 50% of equity

    # Per strategy
    strategy_allocations: Dict[StrategyId, Decimal] = field(default_factory=lambda: {
        StrategyId.FUNDING_BIAS: Decimal("0.30"),
        StrategyId.LIQUIDATION_CLUSTER: Decimal("0.40"),
        StrategyId.VOLATILITY_EXPANSION: Decimal("0.30"),
    })

    # Correlation
    max_correlated_exposure_pct: Decimal = Decimal("0.70")


@dataclass
class StrategyMetrics:
    """Performance metrics for a strategy."""
    strategy_id: StrategyId
    period_start: datetime
    period_end: datetime

    # Trade counts
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0

    # P&L
    total_pnl: Decimal = Decimal(0)
    gross_profit: Decimal = Decimal(0)
    gross_loss: Decimal = Decimal(0)

    # Ratios
    win_rate: Decimal = Decimal(0)
    profit_factor: Decimal = Decimal(0)
    avg_win: Decimal = Decimal(0)
    avg_loss: Decimal = Decimal(0)
    avg_rr: Decimal = Decimal(0)  # Risk/Reward

    # Risk metrics
    max_drawdown: Decimal = Decimal(0)
    sharpe_ratio: Optional[Decimal] = None
    sortino_ratio: Optional[Decimal] = None

    # Timing
    avg_trade_duration_minutes: Decimal = Decimal(0)

    def calculate_ratios(self):
        if self.total_trades > 0:
            self.win_rate = Decimal(self.winning_trades) / Decimal(self.total_trades)

        if self.gross_loss != 0:
            self.profit_factor = abs(self.gross_profit / self.gross_loss)

        if self.winning_trades > 0:
            self.avg_win = self.gross_profit / Decimal(self.winning_trades)

        if self.losing_trades > 0:
            self.avg_loss = abs(self.gross_loss / Decimal(self.losing_trades))

        if self.avg_loss != 0:
            self.avg_rr = self.avg_win / self.avg_loss


@dataclass
class RegimeAnalysis:
    """Market regime analysis from AI."""
    timestamp: datetime
    regime: MarketRegime
    confidence: Decimal

    # Per-asset analysis
    asset_regimes: Dict[str, MarketRegime] = field(default_factory=dict)

    # Recommendations
    suggested_allocations: Optional[Dict[StrategyId, Decimal]] = None
    risk_adjustment: Decimal = Decimal("1.0")  # Multiplier for risk limits

    # Reasoning
    analysis: str = ""

    # Validity
    valid_until: Optional[datetime] = None
