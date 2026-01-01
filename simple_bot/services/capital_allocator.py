"""
Capital Allocator Service
=========================

Determines position sizing for trading signals based on:
- Kelly Criterion for optimal sizing
- ATR-based sizing for volatility-adjusted positions
- Risk parity for equal risk contribution
- Portfolio constraints (max positions, correlation limits, reserves)

Subscribes to Topic.SIGNALS and publishes to Topic.SIZED_SIGNALS.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from simple_bot.config.loader import CapitalAllocatorConfig, RiskConfig, get_config
from simple_bot.services.base import BaseService
from simple_bot.services.message_bus import Message, MessageBus, Topic

# Try to import Database and HyperliquidClient
try:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from database.db import Database
    DB_AVAILABLE = True
except ImportError:
    Database = None  # type: ignore
    DB_AVAILABLE = False

try:
    from simple_bot.api.hyperliquid import HyperliquidClient
    CLIENT_AVAILABLE = True
except ImportError:
    HyperliquidClient = None  # type: ignore
    CLIENT_AVAILABLE = False


logger = logging.getLogger(__name__)


# =============================================================================
# Data Models
# =============================================================================


@dataclass
class Position:
    """Current position information."""
    
    symbol: str
    side: str  # "long" or "short"
    size: float
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    leverage: int
    margin_used: float
    
    @property
    def notional_value(self) -> float:
        """Position notional value."""
        return self.size * self.mark_price


@dataclass
class AccountState:
    """Current account state."""
    
    equity: float
    available_balance: float
    margin_used: float
    unrealized_pnl: float
    positions: List[Position] = field(default_factory=list)
    
    @property
    def position_count(self) -> int:
        """Number of open positions."""
        return len(self.positions)
    
    @property
    def total_exposure(self) -> float:
        """Total notional exposure across all positions."""
        return sum(p.notional_value for p in self.positions)
    
    def get_position(self, symbol: str) -> Optional[Position]:
        """Get position by symbol."""
        for p in self.positions:
            if p.symbol == symbol:
                return p
        return None


@dataclass
class SizedSignal:
    """Signal with position sizing applied."""
    
    # Original signal fields
    symbol: str
    strategy: str
    direction: str  # "long" or "short"
    entry_price: float
    confidence: float
    timestamp: datetime
    
    # Sizing fields
    size: float  # Position size in base currency
    size_usd: float  # Position size in USD
    leverage: int
    risk_amount: float  # USD at risk
    
    # Risk levels
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    
    # Sizing metadata
    sizing_method: str = "default"
    sizing_factors: Dict[str, float] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary for message bus."""
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "confidence": self.confidence,
            "timestamp": self.timestamp.isoformat(),
            "size": self.size,
            "size_usd": self.size_usd,
            "leverage": self.leverage,
            "risk_amount": self.risk_amount,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "sizing_method": self.sizing_method,
            "sizing_factors": self.sizing_factors,
        }


# =============================================================================
# Position Sizing Algorithms
# =============================================================================


def kelly_size(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """
    Calculate optimal position size using Kelly Criterion.
    
    Kelly formula: f* = p - (1-p)/b
    where:
        p = probability of winning (win_rate)
        b = win/loss ratio (avg_win / avg_loss)
    
    Args:
        win_rate: Historical win rate (0-1)
        avg_win: Average winning trade return
        avg_loss: Average losing trade return (positive value)
    
    Returns:
        Optimal position size as fraction of capital (0-0.25)
        Uses 50% fractional Kelly for safety
    """
    if avg_loss <= 0:
        return 0.0
    
    if win_rate <= 0 or win_rate >= 1:
        return 0.0
    
    win_loss_ratio = abs(avg_win) / abs(avg_loss)
    
    # Kelly formula
    kelly = win_rate - ((1 - win_rate) / win_loss_ratio)
    
    # Use fractional Kelly (50%) for safety
    fractional_kelly = kelly * 0.5
    
    # Cap at 25% max position size
    return max(0.0, min(fractional_kelly, 0.25))


def atr_size(
    account_value: float,
    atr: float,
    risk_per_trade: float,
    atr_multiplier: float = 2.0
) -> float:
    """
    Calculate position size based on ATR (volatility-adjusted sizing).
    
    Uses ATR to determine stop loss distance, then sizes position
    so that a stop loss hit equals the desired risk amount.
    
    Args:
        account_value: Total account equity
        atr: Average True Range for the symbol
        risk_per_trade: Maximum risk per trade as fraction of account (e.g., 0.01 = 1%)
        atr_multiplier: Number of ATRs for stop loss distance (default: 2)
    
    Returns:
        Position size in base currency
    """
    if atr <= 0 or account_value <= 0:
        return 0.0
    
    # Risk amount in USD
    risk_amount = account_value * risk_per_trade
    
    # Stop loss distance = ATR * multiplier
    stop_distance = atr * atr_multiplier
    
    # Position size = risk_amount / stop_distance
    position_size = risk_amount / stop_distance
    
    return position_size


def risk_parity_weight(
    symbol_volatility: float,
    volatilities: Dict[str, float],
    target_risk_contribution: float = None
) -> float:
    """
    Calculate position weight using risk parity approach.
    
    Equal risk contribution: weight inversely proportional to volatility.
    
    Args:
        symbol_volatility: Volatility (e.g., ATR%) for the target symbol
        volatilities: Dict of symbol -> volatility for all symbols
        target_risk_contribution: Target risk per position (default: equal weight)
    
    Returns:
        Weight as fraction (0-1)
    """
    if symbol_volatility <= 0 or not volatilities:
        return 0.0
    
    # Filter out zero volatilities
    valid_vols = {s: v for s, v in volatilities.items() if v > 0}
    
    if not valid_vols:
        return 0.0
    
    # Inverse volatility weights
    inverse_vols = {s: 1.0 / v for s, v in valid_vols.items()}
    total_inverse = sum(inverse_vols.values())
    
    if total_inverse <= 0:
        return 0.0
    
    # Normalize to get weights
    symbol_inverse = 1.0 / symbol_volatility
    weight = symbol_inverse / total_inverse
    
    return weight


# =============================================================================
# Capital Allocator Service
# =============================================================================


class CapitalAllocatorService(BaseService):
    """
    Service that determines position sizing for trading signals.
    
    Subscribes to Topic.SIGNALS for incoming trade signals.
    Publishes sized signals to Topic.SIZED_SIGNALS for execution.
    
    Constraints enforced:
    - Maximum position size percentage
    - Maximum exposure per strategy
    - Maximum correlation with existing positions
    - Minimum reserve capital
    - Maximum number of concurrent positions
    
    Sizing methods:
    - Kelly Criterion (when historical stats available)
    - ATR-based (volatility-adjusted)
    - Fixed percentage (fallback)
    """
    
    # Correlation threshold for rejection
    MAX_CORRELATION = 0.7
    
    # Minimum position size in USD
    MIN_POSITION_USD = 10.0
    
    def __init__(
        self,
        bus: MessageBus,
        db: Optional["Database"] = None,
        client: Optional["HyperliquidClient"] = None,
        config: Optional[CapitalAllocatorConfig] = None,
        risk_config: Optional[RiskConfig] = None,
    ) -> None:
        """
        Initialize CapitalAllocatorService.
        
        Args:
            bus: MessageBus instance for pub/sub communication
            db: Optional Database instance for persistence and stats lookup
            client: Optional HyperliquidClient for live account data
            config: Optional configuration (loads from global config if None)
            risk_config: Optional risk configuration (loads from global if None)
        """
        # Load config from global config if not provided
        if config is None:
            global_config = get_config()
            config = global_config.services.capital_allocator
        
        if risk_config is None:
            global_config = get_config()
            risk_config = global_config.risk
        
        super().__init__(
            name="capital_allocator",
            bus=bus,
            db=db,
            loop_interval_seconds=1.0,  # Event-driven, not timer-based
        )
        
        self._allocator_config = config
        self._risk_config = risk_config
        self._client = client
        
        # Current state tracking
        self._current_positions: Dict[str, Position] = {}
        self._strategy_exposure: Dict[str, float] = {}
        self._last_account_state: Optional[AccountState] = None
        self._last_account_refresh: Optional[datetime] = None
        
        # Correlation cache (symbol_pair -> correlation)
        self._correlation_cache: Dict[str, float] = {}
        self._correlation_cache_time: Optional[datetime] = None
        
        # Strategy performance cache (for Kelly sizing)
        self._strategy_stats: Dict[str, Dict[str, float]] = {}
        
        # Statistics
        self._signals_received: int = 0
        self._signals_sized: int = 0
        self._signals_rejected: int = 0
        self._rejection_reasons: Dict[str, int] = {}
    
    # =========================================================================
    # Lifecycle Methods
    # =========================================================================
    
    async def _on_start(self) -> None:
        """Subscribe to signals topic on service start."""
        self._logger.info(
            "Starting CapitalAllocatorService with config: "
            "max_positions=%d, max_position_pct=%.0f%%, reserve_pct=%.0f%%",
            self._allocator_config.max_positions,
            self._allocator_config.max_position_pct * 100,
            self._allocator_config.reserve_pct * 100,
        )
        
        # Subscribe to signals
        await self.subscribe(Topic.SIGNALS, self._on_signal)
        
        # Initial account state refresh
        await self._refresh_account_state()
        
        # Load strategy statistics for Kelly sizing
        await self._load_strategy_stats()
    
    async def _on_stop(self) -> None:
        """Cleanup on service stop."""
        self._logger.info(
            "CapitalAllocatorService stopped. "
            "Received %d signals, sized %d, rejected %d",
            self._signals_received,
            self._signals_sized,
            self._signals_rejected,
        )
    
    async def _run_iteration(self) -> None:
        """
        Periodic maintenance tasks.
        
        - Refresh account state every 30 seconds
        - Refresh correlation cache every 5 minutes
        """
        now = datetime.utcnow()
        
        # Refresh account state
        if (
            self._last_account_refresh is None or
            (now - self._last_account_refresh).total_seconds() > 30
        ):
            await self._refresh_account_state()
        
        # Refresh correlation cache
        if (
            self._correlation_cache_time is None or
            (now - self._correlation_cache_time).total_seconds() > 300
        ):
            await self._refresh_correlation_cache()
    
    async def _health_check_impl(self) -> bool:
        """Check service-specific health."""
        # Consider unhealthy if we haven't refreshed account in 2 minutes
        if self._last_account_refresh:
            age = (datetime.utcnow() - self._last_account_refresh).total_seconds()
            if age > 120:
                self._logger.warning(
                    "Account state stale: %.0f seconds old",
                    age
                )
                return False
        return True
    
    # =========================================================================
    # Signal Handling
    # =========================================================================
    
    async def _on_signal(self, message: Message) -> None:
        """
        Handle incoming trading signal.
        
        Expected payload format:
        {
            "symbol": "ETH",
            "strategy": "momentum",
            "direction": "long",
            "entry_price": 3500.0,
            "confidence": 0.85,
            "stop_loss": 3400.0,  # optional
            "take_profit": 3700.0,  # optional
            "atr": 50.0,  # optional, for ATR-based sizing
            "timestamp": datetime or ISO string
        }
        """
        try:
            self._signals_received += 1
            payload = message.payload
            
            if not isinstance(payload, dict):
                self._logger.warning("Invalid signal payload type: %s", type(payload))
                return
            
            symbol = payload.get("symbol")
            strategy = payload.get("strategy")
            direction = payload.get("direction")
            entry_price = payload.get("entry_price")
            
            if not all([symbol, strategy, direction, entry_price]):
                self._logger.warning("Signal missing required fields: %s", payload)
                self._reject_signal("missing_fields")
                return
            
            self._logger.debug(
                "Processing signal: %s %s %s @ %.2f",
                direction, symbol, strategy, entry_price
            )
            
            # Refresh account state if stale
            if (
                self._last_account_refresh is None or
                (datetime.utcnow() - self._last_account_refresh).total_seconds() > 30
            ):
                await self._refresh_account_state()
            
            if self._last_account_state is None:
                self._logger.error("Cannot size signal: no account state available")
                self._reject_signal("no_account_state")
                return
            
            # Check constraints
            rejection_reason = await self._check_constraints(payload)
            if rejection_reason:
                self._logger.info(
                    "Signal %s %s rejected: %s",
                    direction, symbol, rejection_reason
                )
                self._reject_signal(rejection_reason)
                return
            
            # Calculate position size
            sized_signal = await self._calculate_size(payload)
            
            if sized_signal is None or sized_signal.size <= 0:
                self._logger.info(
                    "Signal %s %s rejected: calculated size <= 0",
                    direction, symbol
                )
                self._reject_signal("size_zero")
                return
            
            # Check minimum position size
            if sized_signal.size_usd < self.MIN_POSITION_USD:
                self._logger.info(
                    "Signal %s %s rejected: size_usd %.2f < minimum %.2f",
                    direction, symbol, sized_signal.size_usd, self.MIN_POSITION_USD
                )
                self._reject_signal("below_minimum")
                return
            
            # Publish sized signal
            await self.publish(Topic.SIZED_SIGNALS, sized_signal.to_dict())
            
            self._signals_sized += 1
            self._logger.info(
                "Sized signal published: %s %s %.4f @ %.2f (%.2f USD, %dx leverage)",
                sized_signal.direction,
                sized_signal.symbol,
                sized_signal.size,
                sized_signal.entry_price,
                sized_signal.size_usd,
                sized_signal.leverage,
            )
            
        except Exception as e:
            self._logger.error(
                "Error processing signal: %s",
                e,
                exc_info=True
            )
            self._reject_signal("error")
    
    def _reject_signal(self, reason: str) -> None:
        """Track signal rejection."""
        self._signals_rejected += 1
        self._rejection_reasons[reason] = self._rejection_reasons.get(reason, 0) + 1
    
    # =========================================================================
    # Constraint Checking
    # =========================================================================
    
    async def _check_constraints(self, signal: Dict[str, Any]) -> Optional[str]:
        """
        Check if signal passes all constraints.
        
        Returns:
            None if all constraints pass, otherwise rejection reason string
        """
        symbol = signal["symbol"]
        strategy = signal.get("strategy", "unknown")
        account = self._last_account_state
        
        if account is None:
            return "no_account_state"
        
        # 1. Check max positions
        if account.position_count >= self._allocator_config.max_positions:
            # Allow if we already have a position in this symbol (scaling)
            if account.get_position(symbol) is None:
                return "max_positions_reached"
        
        # 2. Check reserve requirement
        available_ratio = account.available_balance / account.equity if account.equity > 0 else 0
        if available_ratio < self._allocator_config.reserve_pct:
            return "insufficient_reserve"
        
        # 3. Check correlation with existing positions
        if not await self._check_correlation(symbol):
            return "high_correlation"
        
        # 4. Check strategy exposure limit
        max_strategy_pct = 0.40  # 40% max per strategy
        current_strategy_exposure = self._strategy_exposure.get(strategy, 0.0)
        if current_strategy_exposure / account.equity > max_strategy_pct if account.equity > 0 else False:
            return "strategy_exposure_limit"
        
        return None
    
    async def _check_correlation(self, symbol: str) -> bool:
        """
        Check if new symbol is too correlated with existing positions.
        
        Args:
            symbol: Symbol to check
            
        Returns:
            True if correlation is acceptable, False if too correlated
        """
        if not self._current_positions:
            return True
        
        for pos_symbol in self._current_positions:
            if pos_symbol == symbol:
                continue  # Same symbol is allowed (scaling)
            
            # Get correlation from cache or database
            correlation = await self._get_correlation(symbol, pos_symbol)
            
            if correlation is not None and abs(correlation) > self.MAX_CORRELATION:
                self._logger.debug(
                    "Symbol %s too correlated with %s: %.2f > %.2f",
                    symbol, pos_symbol, abs(correlation), self.MAX_CORRELATION
                )
                return False
        
        return True
    
    async def _get_correlation(self, symbol1: str, symbol2: str) -> Optional[float]:
        """Get correlation between two symbols from cache or database."""
        # Create cache key (sorted for consistency)
        cache_key = "_".join(sorted([symbol1, symbol2]))
        
        # Check cache
        if cache_key in self._correlation_cache:
            return self._correlation_cache[cache_key]
        
        # Query database if available
        if self.db is not None:
            try:
                correlation = await self.db.get_correlation(symbol1, symbol2)
                if correlation is not None:
                    self._correlation_cache[cache_key] = correlation
                    return correlation
            except Exception as e:
                self._logger.warning(
                    "Failed to get correlation from database: %s",
                    e
                )
        
        # Default: assume moderate correlation for crypto assets
        return 0.5
    
    # =========================================================================
    # Position Sizing
    # =========================================================================
    
    async def _calculate_size(self, signal: Dict[str, Any]) -> Optional[SizedSignal]:
        """
        Calculate position size for a signal.
        
        Uses multiple sizing methods in priority order:
        1. Kelly Criterion (if strategy stats available)
        2. ATR-based (if ATR provided in signal)
        3. Fixed percentage (fallback)
        """
        symbol = signal["symbol"]
        strategy = signal.get("strategy", "unknown")
        direction = signal["direction"]
        entry_price = float(signal["entry_price"])
        confidence = float(signal.get("confidence", 0.5))
        atr = signal.get("atr")
        stop_loss = signal.get("stop_loss")
        take_profit = signal.get("take_profit")
        
        account = self._last_account_state
        if account is None:
            return None
        
        # Available capital (respecting reserve)
        available_capital = account.equity * (1 - self._allocator_config.reserve_pct)
        
        # Max position value based on config
        max_position_value = account.equity * self._allocator_config.max_position_pct
        
        # Determine sizing method and calculate
        sizing_method = "fixed_pct"
        sizing_factors: Dict[str, float] = {}
        position_pct = self._risk_config.position_size_pct / 100.0  # Default
        
        # Try Kelly sizing first
        strategy_stats = self._strategy_stats.get(strategy)
        if strategy_stats and strategy_stats.get("trade_count", 0) >= 10:
            kelly_pct = kelly_size(
                win_rate=strategy_stats.get("win_rate", 0.5),
                avg_win=strategy_stats.get("avg_win", 0.01),
                avg_loss=strategy_stats.get("avg_loss", 0.01),
            )
            if kelly_pct > 0:
                position_pct = kelly_pct
                sizing_method = "kelly"
                sizing_factors = {
                    "win_rate": strategy_stats.get("win_rate", 0.5),
                    "avg_win": strategy_stats.get("avg_win", 0.01),
                    "avg_loss": strategy_stats.get("avg_loss", 0.01),
                    "kelly_raw": kelly_pct * 2,  # Before fractional
                }
        
        # Try ATR sizing if ATR provided and no Kelly
        elif atr is not None and atr > 0:
            risk_per_trade = self._risk_config.position_size_pct / 100.0 * 0.5  # 50% of default as risk
            atr_position = atr_size(
                account_value=account.equity,
                atr=float(atr),
                risk_per_trade=risk_per_trade,
                atr_multiplier=2.0,
            )
            if atr_position > 0:
                position_value = atr_position * entry_price
                position_pct = position_value / account.equity if account.equity > 0 else 0
                sizing_method = "atr"
                sizing_factors = {
                    "atr": float(atr),
                    "risk_per_trade": risk_per_trade,
                    "atr_multiplier": 2.0,
                }
        
        # Adjust by confidence
        confidence_factor = 0.5 + (confidence * 0.5)  # Range: 0.5 - 1.0
        position_pct *= confidence_factor
        sizing_factors["confidence_factor"] = confidence_factor
        
        # Calculate position value
        position_value = account.equity * position_pct
        
        # Apply caps
        position_value = min(position_value, max_position_value)
        position_value = min(position_value, available_capital)
        
        # Convert to position size
        size = position_value / entry_price if entry_price > 0 else 0
        
        # Calculate risk amount
        risk_amount = account.equity * (self._risk_config.stop_loss_pct / 100.0) * position_pct
        
        # Determine stop loss and take profit if not provided
        if stop_loss is None:
            if direction == "long":
                stop_loss = entry_price * (1 - self._risk_config.stop_loss_pct / 100.0)
            else:
                stop_loss = entry_price * (1 + self._risk_config.stop_loss_pct / 100.0)
        
        if take_profit is None:
            if direction == "long":
                take_profit = entry_price * (1 + self._risk_config.take_profit_pct / 100.0)
            else:
                take_profit = entry_price * (1 - self._risk_config.take_profit_pct / 100.0)
        
        # Parse timestamp
        ts = signal.get("timestamp")
        if isinstance(ts, str):
            try:
                timestamp = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                timestamp = datetime.utcnow()
        elif isinstance(ts, datetime):
            timestamp = ts
        else:
            timestamp = datetime.utcnow()
        
        return SizedSignal(
            symbol=symbol,
            strategy=strategy,
            direction=direction,
            entry_price=entry_price,
            confidence=confidence,
            timestamp=timestamp,
            size=size,
            size_usd=position_value,
            leverage=self._risk_config.leverage,
            risk_amount=risk_amount,
            stop_loss=stop_loss,
            take_profit=take_profit,
            sizing_method=sizing_method,
            sizing_factors=sizing_factors,
        )
    
    # =========================================================================
    # State Management
    # =========================================================================
    
    async def _refresh_account_state(self) -> None:
        """Refresh account state from exchange or database."""
        try:
            # Try to get from Hyperliquid client first
            if self._client is not None and self._client.is_connected:
                state = await self._client.get_account_state()
                
                positions = [
                    Position(
                        symbol=p["symbol"],
                        side=p["side"],
                        size=p["size"],
                        entry_price=p["entryPrice"],
                        mark_price=p.get("markPrice", p["entryPrice"]),
                        unrealized_pnl=p.get("unrealizedPnl", 0.0),
                        leverage=p.get("leverage", 1),
                        margin_used=p.get("marginUsed", 0.0),
                    )
                    for p in state.get("positions", [])
                ]
                
                self._last_account_state = AccountState(
                    equity=state.get("equity", 0.0),
                    available_balance=state.get("availableBalance", 0.0),
                    margin_used=state.get("marginUsed", 0.0),
                    unrealized_pnl=state.get("unrealizedPnl", 0.0),
                    positions=positions,
                )
                
            # Fallback to database
            elif self.db is not None:
                account = await self.db.get_account()
                positions_data = await self.db.get_positions()
                
                if account:
                    positions = [
                        Position(
                            symbol=p["symbol"],
                            side=p["side"],
                            size=float(p["size"]),
                            entry_price=float(p["entry_price"]),
                            mark_price=float(p.get("mark_price", p["entry_price"])),
                            unrealized_pnl=float(p.get("unrealized_pnl", 0)),
                            leverage=int(p.get("leverage", 1)),
                            margin_used=float(p.get("margin_used", 0)),
                        )
                        for p in positions_data
                    ]
                    
                    self._last_account_state = AccountState(
                        equity=float(account.get("equity", 0)),
                        available_balance=float(account.get("available_balance", 0)),
                        margin_used=float(account.get("margin_used", 0)),
                        unrealized_pnl=float(account.get("unrealized_pnl", 0)),
                        positions=positions,
                    )
            
            if self._last_account_state:
                # Update position tracking
                self._current_positions = {
                    p.symbol: p for p in self._last_account_state.positions
                }
                
                # Update strategy exposure (requires strategy info in positions)
                # For now, track by symbol
                self._strategy_exposure = {}
                for p in self._last_account_state.positions:
                    strategy = "unknown"  # Would need strategy info from database
                    self._strategy_exposure[strategy] = (
                        self._strategy_exposure.get(strategy, 0.0) + p.notional_value
                    )
                
                self._last_account_refresh = datetime.utcnow()
                
                self._logger.debug(
                    "Account state refreshed: equity=%.2f, positions=%d",
                    self._last_account_state.equity,
                    self._last_account_state.position_count,
                )
                
        except Exception as e:
            self._logger.error(
                "Failed to refresh account state: %s",
                e,
                exc_info=True
            )
    
    async def _refresh_correlation_cache(self) -> None:
        """Refresh correlation cache from database."""
        # Correlation cache is populated on-demand in _get_correlation
        # This method just clears stale entries
        self._correlation_cache.clear()
        self._correlation_cache_time = datetime.utcnow()
        self._logger.debug("Correlation cache cleared")
    
    async def _load_strategy_stats(self) -> None:
        """Load strategy performance statistics for Kelly sizing."""
        if self.db is None:
            return
        
        try:
            # Get closed trades grouped by strategy
            trades = await self.db.get_trades(is_closed=True, limit=1000)
            
            # Calculate stats per strategy
            strategy_trades: Dict[str, List[Dict]] = {}
            for trade in trades:
                strategy = trade.get("strategy", "unknown")
                if strategy not in strategy_trades:
                    strategy_trades[strategy] = []
                strategy_trades[strategy].append(trade)
            
            for strategy, strades in strategy_trades.items():
                if len(strades) < 10:
                    continue
                
                wins = [t for t in strades if float(t.get("net_pnl", 0)) > 0]
                losses = [t for t in strades if float(t.get("net_pnl", 0)) <= 0]
                
                win_rate = len(wins) / len(strades) if strades else 0.5
                avg_win = (
                    sum(float(t.get("net_pnl", 0)) for t in wins) / len(wins)
                    if wins else 0.01
                )
                avg_loss = (
                    abs(sum(float(t.get("net_pnl", 0)) for t in losses)) / len(losses)
                    if losses else 0.01
                )
                
                self._strategy_stats[strategy] = {
                    "trade_count": len(strades),
                    "win_rate": win_rate,
                    "avg_win": avg_win,
                    "avg_loss": avg_loss,
                }
                
                self._logger.info(
                    "Strategy stats loaded for %s: trades=%d, win_rate=%.1f%%, "
                    "avg_win=%.2f, avg_loss=%.2f",
                    strategy,
                    len(strades),
                    win_rate * 100,
                    avg_win,
                    avg_loss,
                )
                
        except Exception as e:
            self._logger.warning(
                "Failed to load strategy stats: %s",
                e
            )
    
    # =========================================================================
    # Properties & Stats
    # =========================================================================
    
    @property
    def stats(self) -> Dict[str, Any]:
        """Extended service statistics."""
        base_stats = super().stats
        base_stats.update({
            "signals_received": self._signals_received,
            "signals_sized": self._signals_sized,
            "signals_rejected": self._signals_rejected,
            "rejection_reasons": self._rejection_reasons.copy(),
            "current_positions": len(self._current_positions),
            "account_equity": (
                self._last_account_state.equity
                if self._last_account_state else None
            ),
            "available_balance": (
                self._last_account_state.available_balance
                if self._last_account_state else None
            ),
            "last_account_refresh": (
                self._last_account_refresh.isoformat()
                if self._last_account_refresh else None
            ),
            "strategies_with_stats": list(self._strategy_stats.keys()),
        })
        return base_stats
    
    @property
    def current_positions(self) -> Dict[str, Position]:
        """Get current positions."""
        return self._current_positions.copy()
    
    @property
    def account_state(self) -> Optional[AccountState]:
        """Get current account state."""
        return self._last_account_state


# =============================================================================
# Factory Function
# =============================================================================


def create_capital_allocator(
    bus: MessageBus,
    db: Optional["Database"] = None,
    client: Optional["HyperliquidClient"] = None,
) -> CapitalAllocatorService:
    """
    Factory function to create a CapitalAllocatorService.
    
    Args:
        bus: MessageBus instance
        db: Optional Database instance
        client: Optional HyperliquidClient instance
    
    Returns:
        Configured CapitalAllocatorService instance
    """
    return CapitalAllocatorService(
        bus=bus,
        db=db,
        client=client,
    )
