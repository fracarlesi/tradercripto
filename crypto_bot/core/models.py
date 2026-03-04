"""Pydantic models for HLQuantBot conservative refactor.

These models define the data contracts between services:
- MarketState: OHLCV + indicators for an asset
- Setup: Trade setup candidate
- TradeIntent: Sized and approved trade ready for execution
- RiskParams: Risk management parameters
"""

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator


class Regime(str, Enum):
    """Market regime classification."""

    TREND = "trend"      # ADX > 25, clear directional movement
    RANGE = "range"      # ADX < 20, sideways/mean-reverting
    CHAOS = "chaos"      # Uncertain, no clear pattern - stay flat


class Direction(str, Enum):
    """Trade direction."""

    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class SetupType(str, Enum):
    """Type of trade setup."""

    TREND_BREAKOUT = "trend_breakout"      # Breakout in trend regime
    MEAN_REVERSION = "mean_reversion"      # Bounce in range regime
    MOMENTUM = "momentum"                   # Momentum continuation
    VOLUME_BREAKOUT = "volume_breakout"    # Volume spike + price momentum
    MOMENTUM_BURST = "momentum_burst"      # RSI acceleration + price momentum


class KillSwitchStatus(str, Enum):
    """Kill switch status levels."""

    OK = "ok"                    # Trading normally
    WARNING = "warning"          # Approaching limits
    DAILY_PAUSE = "daily_pause"  # Paused until tomorrow
    WEEKLY_PAUSE = "weekly_pause"  # Paused for 3 days
    STOPPED = "stopped"          # Max DD hit - manual intervention needed


# =============================================================================
# Core Data Models
# =============================================================================

class MarketState(BaseModel):
    """Market state for a single asset with OHLCV and indicators.

    Published on Topic.MARKET_STATE every 4h (or configured interval).
    """

    symbol: str = Field(..., description="Trading symbol (e.g., BTC, ETH)")
    timeframe: str = Field(default="4h", description="Candle timeframe")
    timestamp: datetime = Field(..., description="State timestamp")

    # OHLCV
    open: Decimal = Field(..., ge=0)
    high: Decimal = Field(..., ge=0)
    low: Decimal = Field(..., ge=0)
    close: Decimal = Field(..., ge=0)
    volume: Decimal = Field(..., ge=0)

    # Technical Indicators
    atr: Decimal = Field(..., ge=0, description="ATR(14)")
    atr_pct: Decimal = Field(..., ge=0, description="ATR as % of price")
    adx: Decimal = Field(..., ge=0, le=100, description="ADX(14)")
    rsi: Decimal = Field(..., ge=0, le=100, description="RSI(14)")
    ema50: Decimal = Field(..., ge=0, description="EMA(50)")
    ema200: Decimal = Field(..., ge=0, description="EMA(200)")
    ema200_slope: Decimal = Field(..., description="EMA200 slope (normalized)")

    # Simple Moving Averages for SMA crossover strategy
    sma20: Decimal = Field(..., ge=0, description="SMA(20)")
    sma50: Decimal = Field(..., ge=0, description="SMA(50)")

    # Fast EMAs for momentum scalper strategy
    ema9: Optional[Decimal] = Field(None, ge=0, description="EMA(9)")
    ema21: Optional[Decimal] = Field(None, ge=0, description="EMA(21)")

    # EMA slopes (4-bar lookback, as fractional change)
    ema9_slope: Decimal = Field(default=Decimal("0"), description="EMA9 slope: (ema9 - ema9_4bars_ago) / ema9_4bars_ago")
    ema21_slope: Decimal = Field(default=Decimal("0"), description="EMA21 slope: (ema21 - ema21_4bars_ago) / ema21_4bars_ago")

    # Previous candle data for candlestick pattern detection
    prev_open: Optional[Decimal] = Field(None, ge=0, description="Previous candle open")
    prev_high: Optional[Decimal] = Field(None, ge=0, description="Previous candle high")
    prev_low: Optional[Decimal] = Field(None, ge=0, description="Previous candle low")
    prev_close: Optional[Decimal] = Field(None, ge=0, description="Previous candle close")

    # Candlestick pattern signals for entry confirmation
    bullish_engulfing: bool = Field(default=False, description="Bullish engulfing pattern detected")
    bearish_engulfing: bool = Field(default=False, description="Bearish engulfing pattern detected")

    # Volume metrics
    volume_usd: Optional[Decimal] = Field(None, ge=0, description="Volume in USD (close * volume)")
    volume_sma20: Optional[Decimal] = Field(None, ge=0, description="20-period SMA of volume")
    volume_ratio: Optional[Decimal] = Field(None, ge=0, description="Current volume / SMA20 volume")

    # Optional indicators
    choppiness: Optional[Decimal] = Field(None, ge=0, le=100, description="Choppiness Index")
    bb_upper: Optional[Decimal] = Field(None, ge=0, description="Bollinger upper band")
    bb_lower: Optional[Decimal] = Field(None, ge=0, description="Bollinger lower band")
    bb_mid: Optional[Decimal] = Field(None, ge=0, description="Bollinger middle band")

    # RSI slope (2-bar lookback: RSI[current] - RSI[2 bars ago])
    rsi_slope: Decimal = Field(default=Decimal("0"), description="RSI slope: RSI[i] - RSI[i-2]")

    # Multi-timeframe indicators (1h-equivalent computed from 15m bars)
    rsi_1h: Optional[Decimal] = Field(None, ge=0, le=100, description="RSI(56) — 1h equivalent")
    adx_1h: Optional[Decimal] = Field(None, ge=0, le=100, description="ADX(56) — 1h equivalent")
    ema9_1h: Optional[Decimal] = Field(None, ge=0, description="EMA(36) — 1h EMA9 equivalent")
    ema21_1h: Optional[Decimal] = Field(None, ge=0, description="EMA(84) — 1h EMA21 equivalent")

    # Exchange data (live only — used as ML feature)
    funding_rate: Optional[Decimal] = Field(None, description="Current funding rate for symbol")
    open_interest: Optional[Decimal] = Field(None, description="Current open interest in USD")

    # Derived
    regime: Regime = Field(..., description="Detected market regime")
    trend_direction: Direction = Field(..., description="Current trend direction")

    # Metadata
    bars_count: int = Field(default=200, description="Number of bars used")

    class Config:
        json_encoders = {
            Decimal: lambda v: float(v),
            datetime: lambda v: v.isoformat(),
        }


class Setup(BaseModel):
    """Trade setup candidate generated by strategy.

    Published on Topic.SETUPS when a valid setup is detected.
    """

    id: str = Field(..., description="Unique setup ID")
    symbol: str = Field(..., description="Trading symbol")
    timestamp: datetime = Field(..., description="Setup generation time")

    # Setup details
    setup_type: SetupType = Field(..., description="Type of setup")
    direction: Direction = Field(..., description="Trade direction")
    regime: Regime = Field(..., description="Regime at setup time")

    # Price levels
    entry_price: Decimal = Field(..., ge=0, description="Target entry price")
    stop_price: Decimal = Field(..., ge=0, description="Initial stop loss")
    stop_distance_pct: Decimal = Field(..., description="Stop distance as %")

    # Indicators at setup time
    atr: Decimal = Field(..., ge=0)
    atr_pct: Optional[Decimal] = Field(None, ge=0, description="ATR as % of price")
    adx: Decimal = Field(..., ge=0, le=100)
    rsi: Decimal = Field(..., ge=0, le=100)

    # Quality metrics
    setup_quality: Decimal = Field(default=Decimal("0.5"), ge=0, le=1,
                                   description="Setup quality score 0-1")
    confidence: Decimal = Field(default=Decimal("0.5"), ge=0, le=1,
                                description="Strategy confidence 0-1")

    # LLM veto (filled after veto check)
    llm_approved: Optional[bool] = Field(None, description="LLM approval status")
    llm_confidence: Optional[Decimal] = Field(None, ge=0, le=1)
    llm_reason: Optional[str] = Field(None)

    class Config:
        json_encoders = {
            Decimal: lambda v: float(v),
            datetime: lambda v: v.isoformat(),
        }


class RiskParams(BaseModel):
    """Risk parameters for position sizing.

    Calculated by RiskManager based on setup and account state.
    """

    # Sizing
    risk_amount: Decimal = Field(..., ge=0, description="Max risk in USD")
    position_size: Decimal = Field(..., ge=0, description="Position size in base")
    notional_value: Decimal = Field(..., ge=0, description="Notional value in USD")

    # Stop levels
    stop_price: Decimal = Field(..., ge=0)
    stop_distance_pct: Decimal = Field(...)
    trailing_distance_atr: Decimal = Field(default=Decimal("2.5"))

    # Exposure checks (allow up to 1000% for high-risk strategies with leverage)
    exposure_pct: Decimal = Field(..., ge=0, le=1000,
                                  description="Position as % of equity")
    total_exposure_pct: Decimal = Field(..., ge=0,
                                        description="Total portfolio exposure %")
    leverage_used: Decimal = Field(default=Decimal("1"), ge=0)

    # Validation flags
    size_approved: bool = Field(default=True)
    rejection_reason: Optional[str] = Field(None)

    class Config:
        json_encoders = {
            Decimal: lambda v: float(v),
        }


class TradeIntent(BaseModel):
    """Trade intent ready for execution.

    Published on Topic.TRADE_INTENT after sizing and approval.
    Contains everything needed to execute the trade.
    """

    id: str = Field(..., description="Intent ID (matches Setup ID)")
    setup_id: str = Field(..., description="Original setup ID")
    symbol: str = Field(...)
    timestamp: datetime = Field(...)

    # Trade parameters
    direction: Direction = Field(...)
    setup_type: SetupType = Field(...)

    # Execution parameters
    entry_price: Decimal = Field(..., ge=0, description="Target entry")
    position_size: Decimal = Field(..., ge=0, description="Size to trade")
    notional_value: Decimal = Field(..., ge=0)

    # Stop parameters
    stop_price: Decimal = Field(..., ge=0)
    trailing_atr_mult: Decimal = Field(default=Decimal("2.5"))

    # Risk info
    risk_amount: Decimal = Field(..., ge=0)
    risk_pct: Decimal = Field(..., ge=0, le=100)

    # Market context (for ATR-adaptive stops in execution engine)
    atr_pct: Optional[Decimal] = Field(None, ge=0, description="ATR as % of price at entry")

    # Regime at entry (for regime-invalidation exits)
    regime: Optional[str] = Field(default=None, description="Regime when setup was created")

    # Order preferences
    prefer_limit: bool = Field(default=True)
    max_slippage_pct: Decimal = Field(default=Decimal("0.1"))

    class Config:
        json_encoders = {
            Decimal: lambda v: float(v),
            datetime: lambda v: v.isoformat(),
        }


class LLMDecision(BaseModel):
    """LLM veto decision for a setup.

    Stored for analysis and accuracy tracking.
    """

    setup_id: str = Field(...)
    timestamp: datetime = Field(...)

    # Decision
    decision: str = Field(..., pattern="^(ALLOW|DENY)$")
    confidence: Decimal = Field(..., ge=0, le=1)
    reason: str = Field(...)

    # Context at decision time
    symbol: str = Field(...)
    regime: Regime = Field(...)
    setup_type: SetupType = Field(...)

    # Outcome tracking (filled after trade closes)
    trade_pnl: Optional[Decimal] = Field(None)
    was_correct: Optional[bool] = Field(None)

    class Config:
        json_encoders = {
            Decimal: lambda v: float(v),
            datetime: lambda v: v.isoformat(),
        }


class EquitySnapshot(BaseModel):
    """Equity curve snapshot for risk monitoring."""

    timestamp: datetime = Field(...)
    equity: Decimal = Field(..., ge=0)

    # Drawdown tracking
    peak_equity: Decimal = Field(..., ge=0)
    drawdown_pct: Decimal = Field(..., ge=0, le=100)

    # Daily/Weekly P&L
    daily_pnl: Decimal = Field(default=Decimal("0"))
    daily_pnl_pct: Decimal = Field(default=Decimal("0"))
    weekly_pnl: Decimal = Field(default=Decimal("0"))
    weekly_pnl_pct: Decimal = Field(default=Decimal("0"))

    # Position info
    positions_count: int = Field(default=0, ge=0)
    total_exposure: Decimal = Field(default=Decimal("0"), ge=0)

    # Kill switch status
    kill_switch_status: KillSwitchStatus = Field(default=KillSwitchStatus.OK)

    class Config:
        json_encoders = {
            Decimal: lambda v: float(v),
            datetime: lambda v: v.isoformat(),
        }


# =============================================================================
# Order Models
# =============================================================================

class Order(BaseModel):
    """Order model for execution tracking."""

    id: str = Field(..., description="Local order ID")
    exchange_id: Optional[str] = Field(None, description="Exchange order ID")
    intent_id: str = Field(..., description="TradeIntent ID")

    symbol: str = Field(...)
    direction: Direction = Field(...)
    order_type: str = Field(...)  # OrderType as string

    # Order details
    size: Decimal = Field(..., ge=0)
    price: Optional[Decimal] = Field(None, ge=0)

    # Status
    status: str = Field(default="pending")  # OrderStatus as string
    filled_size: Decimal = Field(default=Decimal("0"), ge=0)
    filled_price: Optional[Decimal] = Field(None, ge=0)

    # Timestamps
    created_at: datetime = Field(...)
    submitted_at: Optional[datetime] = Field(None)
    filled_at: Optional[datetime] = Field(None)

    # Execution quality
    slippage_pct: Optional[Decimal] = Field(None)
    fees: Decimal = Field(default=Decimal("0"))

    class Config:
        json_encoders = {
            Decimal: lambda v: float(v),
            datetime: lambda v: v.isoformat(),
        }


class Position(BaseModel):
    """Open position model."""

    symbol: str = Field(...)
    direction: Direction = Field(...)

    # Size and prices
    size: Decimal = Field(..., ge=0)
    entry_price: Decimal = Field(..., ge=0)
    current_price: Decimal = Field(..., ge=0)

    # Stop management
    stop_price: Decimal = Field(..., ge=0)
    highest_price: Decimal = Field(..., ge=0)  # For trailing
    lowest_price: Decimal = Field(..., ge=0)   # For trailing shorts

    # P&L
    unrealized_pnl: Decimal = Field(...)
    unrealized_pnl_pct: Decimal = Field(...)

    # Metadata
    setup_id: str = Field(...)
    setup_type: SetupType = Field(...)
    opened_at: datetime = Field(...)

    class Config:
        json_encoders = {
            Decimal: lambda v: float(v),
            datetime: lambda v: v.isoformat(),
        }


class CooldownReason(str, Enum):
    """Reason for cooldown trigger."""

    STOPLOSS_STREAK = "StoplossStreak"       # 3+ consecutive stoplosses in 1h
    DAILY_DRAWDOWN = "DailyDrawdown"         # Daily DD > 5%
    LOW_PERFORMANCE = "LowPerformance"       # 5+ trades with win rate < 20%


class CooldownState(BaseModel):
    """Cooldown state for trading pause management.

    When triggered, trading is paused until cooldown_until.
    """

    active: bool = Field(default=False, description="Whether cooldown is active")
    reason: Optional[CooldownReason] = Field(None, description="Reason for cooldown")
    triggered_at: Optional[datetime] = Field(None, description="When cooldown was triggered")
    cooldown_until: Optional[datetime] = Field(None, description="When cooldown expires")
    trigger_details: dict = Field(default_factory=dict, description="Extra context about trigger")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat() if v else None,
        }

    def is_expired(self) -> bool:
        """Check if cooldown has expired."""
        if not self.active or not self.cooldown_until:
            return True
        return datetime.now(timezone.utc) >= self.cooldown_until

    def time_remaining(self) -> Optional[int]:
        """Return seconds remaining in cooldown, or None if expired."""
        if not self.active or not self.cooldown_until:
            return None
        remaining = (self.cooldown_until - datetime.now(timezone.utc)).total_seconds()
        return max(0, int(remaining))


# =============================================================================
# Performance Metrics Model
# =============================================================================

class PerformanceMetrics(BaseModel):
    """Risk-adjusted performance metrics calculated from trade history.

    These metrics are critical for understanding if the bot is profitable
    on a risk-adjusted basis.

    Published periodically and available via API for dashboard display.
    """

    timestamp: datetime = Field(..., description="Calculation timestamp")

    # Capital metrics
    equity: Decimal = Field(..., ge=0, description="Current equity")
    initial_equity: Decimal = Field(..., ge=0, description="Starting equity")
    total_pnl: Decimal = Field(..., description="Total realized PnL")
    total_pnl_pct: Decimal = Field(..., description="Total PnL as % of initial")

    # Risk-adjusted returns
    sharpe_ratio: Optional[Decimal] = Field(
        None, description="Annualized Sharpe ratio: (Mean Return - Risk Free) / Std Dev"
    )
    sortino_ratio: Optional[Decimal] = Field(
        None, description="Sortino ratio: uses only downside deviation"
    )
    calmar_ratio: Optional[Decimal] = Field(
        None, description="Calmar ratio: Annual Return / Max Drawdown"
    )

    # Drawdown metrics
    max_drawdown_pct: Decimal = Field(
        default=Decimal("0"), ge=0, description="Worst peak-to-trough decline %"
    )
    max_drawdown_abs: Decimal = Field(
        default=Decimal("0"), ge=0, description="Max drawdown in USD"
    )
    current_drawdown_pct: Decimal = Field(
        default=Decimal("0"), ge=0, description="Current DD from peak %"
    )

    # Trade quality
    profit_factor: Optional[Decimal] = Field(
        None, description="Gross profit / Gross loss"
    )
    win_rate: Decimal = Field(
        default=Decimal("0"), ge=0, le=1, description="% of profitable trades"
    )
    avg_win: Decimal = Field(
        default=Decimal("0"), ge=0, description="Average winning trade"
    )
    avg_loss: Decimal = Field(
        default=Decimal("0"), le=0, description="Average losing trade (negative)"
    )
    avg_win_loss_ratio: Optional[Decimal] = Field(
        None, description="Avg win / Avg loss (absolute)"
    )

    # Expectancy (Van Tharp)
    expectancy: Optional[Decimal] = Field(
        None, description="(AvgWin * WinRate) - (AvgLoss * LossRate)"
    )
    sqn: Optional[Decimal] = Field(
        None, description="System Quality Number (Van Tharp)"
    )

    # Trade statistics
    total_trades: int = Field(default=0, ge=0)
    winning_trades: int = Field(default=0, ge=0)
    losing_trades: int = Field(default=0, ge=0)

    # Additional stats
    total_fees: Decimal = Field(default=Decimal("0"), description="Total fees paid")
    avg_trade_duration_seconds: Optional[int] = Field(None, description="Average trade duration")
    largest_win: Decimal = Field(default=Decimal("0"), description="Largest winning trade")
    largest_loss: Decimal = Field(default=Decimal("0"), description="Largest losing trade")

    class Config:
        json_encoders = {
            Decimal: lambda v: float(v),
            datetime: lambda v: v.isoformat(),
        }

    @classmethod
    def empty_metrics(cls, equity: Decimal, initial_equity: Decimal) -> "PerformanceMetrics":
        """Create empty metrics when no trades exist."""
        return cls(
            timestamp=datetime.now(timezone.utc),
            equity=equity,
            initial_equity=initial_equity,
            total_pnl=Decimal("0"),
            total_pnl_pct=Decimal("0"),
            sharpe_ratio=None,
            sortino_ratio=None,
            calmar_ratio=None,
            max_drawdown_pct=Decimal("0"),
            max_drawdown_abs=Decimal("0"),
            current_drawdown_pct=Decimal("0"),
            profit_factor=None,
            win_rate=Decimal("0"),
            avg_win=Decimal("0"),
            avg_loss=Decimal("0"),
            avg_win_loss_ratio=None,
            expectancy=None,
            sqn=None,
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            total_fees=Decimal("0"),
            avg_trade_duration_seconds=None,
            largest_win=Decimal("0"),
            largest_loss=Decimal("0"),
        )

    @staticmethod
    def calculate_sharpe_ratio(
        returns: List[Decimal],
        risk_free_rate: Decimal = Decimal("0.03")
    ) -> Optional[Decimal]:
        """
        Calculate annualized Sharpe Ratio.

        Sharpe Ratio = (Mean Return - Risk Free Rate) / Std Dev of Returns
        Annualized assuming daily returns: multiply by sqrt(365)

        Args:
            returns: List of daily return percentages as Decimal
            risk_free_rate: Annual risk-free rate (default 3%)

        Returns:
            Annualized Sharpe ratio or None if insufficient data
        """
        import math
        import statistics

        if len(returns) < 2:
            return None

        float_returns = [float(r) for r in returns]
        mean_return = statistics.mean(float_returns)
        std_return = statistics.stdev(float_returns)

        if std_return == 0:
            return None

        daily_rf = float(risk_free_rate) / 365
        sharpe = (mean_return - daily_rf) / std_return

        # Annualize (assuming daily returns)
        annualized_sharpe = sharpe * math.sqrt(365)

        return Decimal(str(round(annualized_sharpe, 2)))

    @staticmethod
    def calculate_sortino_ratio(
        returns: List[Decimal],
        risk_free_rate: Decimal = Decimal("0.03")
    ) -> Optional[Decimal]:
        """
        Calculate annualized Sortino Ratio.

        Sortino Ratio = (Mean Return - Risk Free Rate) / Downside Deviation
        Uses only negative returns for standard deviation.

        Args:
            returns: List of daily return percentages as Decimal
            risk_free_rate: Annual risk-free rate (default 3%)

        Returns:
            Annualized Sortino ratio or None if insufficient data
        """
        import math
        import statistics

        if len(returns) < 2:
            return None

        float_returns = [float(r) for r in returns]
        mean_return = statistics.mean(float_returns)

        # Downside deviation (only negative returns)
        downside_returns = [r for r in float_returns if r < 0]
        if len(downside_returns) < 2:
            return None

        downside_std = statistics.stdev(downside_returns)
        if downside_std == 0:
            return None

        daily_rf = float(risk_free_rate) / 365
        sortino = (mean_return - daily_rf) / downside_std

        # Annualize
        annualized_sortino = sortino * math.sqrt(365)

        return Decimal(str(round(annualized_sortino, 2)))

    @staticmethod
    def calculate_max_drawdown(
        equity_curve: List[tuple[datetime, Decimal]]
    ) -> tuple[Decimal, Decimal, Decimal]:
        """
        Calculate max drawdown % and absolute value, plus current drawdown.

        Args:
            equity_curve: List of (timestamp, equity) tuples ordered by time

        Returns:
            Tuple of (max_dd_pct, max_dd_abs, current_dd_pct)
        """
        if not equity_curve:
            return Decimal("0"), Decimal("0"), Decimal("0")

        peak = equity_curve[0][1]
        max_dd_pct = Decimal("0")
        max_dd_abs = Decimal("0")

        for _timestamp, equity in equity_curve:
            if equity > peak:
                peak = equity

            dd_abs = peak - equity
            dd_pct = (dd_abs / peak) * 100 if peak > 0 else Decimal("0")

            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
                max_dd_abs = dd_abs

        # Current drawdown
        current_peak = max(eq for _, eq in equity_curve)
        current_equity = equity_curve[-1][1]
        current_dd_pct = Decimal("0")
        if current_peak > 0:
            current_dd_pct = ((current_peak - current_equity) / current_peak) * 100

        return max_dd_pct, max_dd_abs, current_dd_pct

    @staticmethod
    def calculate_profit_factor(
        gross_profit: Decimal,
        gross_loss: Decimal
    ) -> Optional[Decimal]:
        """
        Calculate Profit Factor = Gross Profit / |Gross Loss|

        Args:
            gross_profit: Sum of all profitable trades
            gross_loss: Sum of all losing trades (negative value)

        Returns:
            Profit factor or None if no losses
        """
        abs_loss = abs(gross_loss)
        if abs_loss == 0:
            return None

        pf = gross_profit / abs_loss
        return Decimal(str(round(float(pf), 2)))

    @staticmethod
    def calculate_expectancy(
        avg_win: Decimal,
        avg_loss: Decimal,
        win_rate: Decimal
    ) -> Optional[Decimal]:
        """
        Calculate trading expectancy (Van Tharp).

        Expectancy = (AvgWin * WinRate) - (|AvgLoss| * (1 - WinRate))
        Positive expectancy means profitable system over time.

        Args:
            avg_win: Average winning trade amount
            avg_loss: Average losing trade amount (should be negative)
            win_rate: Win rate as decimal (0-1)

        Returns:
            Expectancy per trade in USD
        """
        if avg_loss == 0:
            return None

        loss_rate = Decimal("1") - win_rate
        expectancy = (avg_win * win_rate) - (abs(avg_loss) * loss_rate)

        return Decimal(str(round(float(expectancy), 2)))

    @staticmethod
    def calculate_sqn(
        trade_pnls: List[Decimal],
        avg_risk_per_trade: Optional[Decimal] = None
    ) -> Optional[Decimal]:
        """
        Calculate System Quality Number (Van Tharp).

        SQN = (Mean R-multiple / StdDev R-multiple) * sqrt(N)

        For simplified calculation without explicit risk tracking:
        SQN = (Mean PnL / StdDev PnL) * sqrt(N)

        SQN interpretation:
        - < 1.6: Poor, difficult to trade
        - 1.6 - 2.0: Below average
        - 2.0 - 2.5: Average
        - 2.5 - 3.0: Good
        - 3.0 - 5.0: Excellent
        - 5.0 - 7.0: Superb
        - > 7.0: Holy Grail

        Args:
            trade_pnls: List of trade PnL amounts
            avg_risk_per_trade: Optional average risk per trade for R-multiple calculation

        Returns:
            SQN value or None if insufficient data
        """
        import math
        import statistics

        if len(trade_pnls) < 2:
            return None

        float_pnls = [float(p) for p in trade_pnls]

        try:
            mean_pnl = statistics.mean(float_pnls)
            std_pnl = statistics.stdev(float_pnls)

            if std_pnl == 0:
                return None

            sqn = (mean_pnl / std_pnl) * math.sqrt(len(float_pnls))
            return Decimal(str(round(sqn, 2)))
        except Exception:
            return None

    @staticmethod
    def calculate_calmar_ratio(
        annual_return_pct: Decimal,
        max_drawdown_pct: Decimal
    ) -> Optional[Decimal]:
        """
        Calculate Calmar Ratio = Annual Return % / Max Drawdown %

        Higher is better. Generally want > 1.0.

        Args:
            annual_return_pct: Annualized return percentage
            max_drawdown_pct: Maximum drawdown percentage

        Returns:
            Calmar ratio or None if max drawdown is 0
        """
        if max_drawdown_pct == 0:
            return None

        calmar = annual_return_pct / max_drawdown_pct
        return Decimal(str(round(float(calmar), 2)))
