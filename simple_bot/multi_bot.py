#!/usr/bin/env python3
"""
Multi-Strategy Trading Bot
===========================

Runs 3 strategies in parallel, each selecting its own best symbol.

Strategies:
- Momentum: Follows trends (EMA crossover + RSI)
- Mean Reversion: Fades extremes (RSI + Bollinger Bands)
- Breakout: Trades range breaks (high/low breakouts)

Each strategy:
1. Scans top symbols to find the best opportunity
2. Operates on ONE symbol only (no overlap)
3. Tracks its own P&L

Usage:
    python simple_bot/multi_bot.py
    python simple_bot/multi_bot.py --dry-run

Author: Francesco Carlesi
"""

import os
import sys
import time
import yaml
import logging
import asyncio
import argparse
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional, Dict, List, Any, Set
from dataclasses import dataclass, field

# Hyperliquid SDK
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from eth_account import Account

# Local imports
from strategies import Signal, MomentumStrategy, MeanReversionStrategy, BreakoutStrategy, calculate_atr

# Database import (parent directory)
sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from database.db import Database
    DB_AVAILABLE = True
except ImportError:
    Database = None
    DB_AVAILABLE = False

# Optimization module import
try:
    from optimization import (
        HotReloadConfigManager,
        OptimizationOrchestrator,
    )
    OPTIMIZATION_AVAILABLE = True
except ImportError:
    HotReloadConfigManager = None
    OptimizationOrchestrator = None
    OPTIMIZATION_AVAILABLE = False


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class StrategyConfig:
    """Configuration for a single strategy."""
    name: str
    enabled: bool = True
    position_size_usd: float = 100.0
    leverage: int = 5
    tp_pct: float = 0.005  # 0.5%
    sl_pct: float = 0.003  # 0.3% (fallback if ATR unavailable)
    use_atr_sl: bool = True  # Use ATR-based stop loss
    atr_period: int = 14  # ATR calculation period
    atr_multiplier: float = 2.0  # SL = Entry +/- (ATR * multiplier)
    params: dict = field(default_factory=dict)


@dataclass
class MultiConfig:
    """Multi-strategy bot configuration."""
    # Symbol scanning
    top_symbols_count: int = 10  # Scan top N symbols by volume
    min_volume_24h: float = 1_000_000  # Minimum 24h volume in USD

    # Global settings
    min_order_interval_seconds: int = 60
    log_level: str = "INFO"
    log_file: str = "simple_bot/multi_bot.log"
    testnet: bool = True

    # Risk management - volatility-based position sizing
    risk_per_trade: float = 0.01  # 1% of account equity per trade
    max_position_size_usd: float = 500.0  # Maximum position size cap
    atr_period: int = 14  # ATR calculation period
    atr_sl_multiplier: float = 2.0  # SL distance = ATR * multiplier

    # Strategy configs
    strategies: Dict[str, StrategyConfig] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: str) -> "MultiConfig":
        """Load config from YAML file."""
        with open(path, "r") as f:
            data = yaml.safe_load(f)

        # Global ATR settings (can be overridden per-strategy)
        global_atr_period = data.get("atr_period", 14)
        global_atr_multiplier = data.get("atr_sl_multiplier", 2.0)
        global_use_atr_sl = data.get("use_atr_sl", True)

        # Parse strategy configs
        strategies = {}
        for name in ["momentum", "mean_reversion", "breakout"]:
            strat_data = data.get(name, {})
            strategies[name] = StrategyConfig(
                name=name,
                enabled=strat_data.get("enabled", True),
                position_size_usd=strat_data.get("position_size_usd", data.get("position_size_usd", 100)),
                leverage=strat_data.get("leverage", data.get("leverage", 5)),
                tp_pct=strat_data.get("tp_pct", data.get("tp_pct", 0.005)),
                sl_pct=strat_data.get("sl_pct", data.get("sl_pct", 0.003)),
                use_atr_sl=strat_data.get("use_atr_sl", global_use_atr_sl),
                atr_period=strat_data.get("atr_period", global_atr_period),
                atr_multiplier=strat_data.get("atr_multiplier", global_atr_multiplier),
                params=strat_data
            )

        return cls(
            top_symbols_count=data.get("top_symbols_count", 10),
            min_volume_24h=data.get("min_volume_24h", 1_000_000),
            min_order_interval_seconds=data.get("min_order_interval_seconds", 60),
            log_level=data.get("log_level", "INFO"),
            log_file=data.get("log_file", "simple_bot/multi_bot.log"),
            testnet=data.get("testnet", True),
            risk_per_trade=data.get("risk_per_trade", 0.01),
            max_position_size_usd=data.get("max_position_size_usd", 500.0),
            atr_period=global_atr_period,
            atr_sl_multiplier=global_atr_multiplier,
            strategies=strategies
        )


# =============================================================================
# Logging Setup
# =============================================================================

def setup_logging(config: MultiConfig) -> logging.Logger:
    """Configure logging to file and console."""
    logger = logging.getLogger("multi_bot")
    logger.setLevel(getattr(logging, config.log_level.upper()))
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    log_path = Path(config.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger


# =============================================================================
# Position Tracking
# =============================================================================

@dataclass
class Position:
    """Tracks an open position."""
    symbol: str
    strategy: str
    side: str  # "long" or "short"
    size: float
    entry_price: float
    entry_time: datetime
    tp_price: float
    sl_price: float
    atr_value: Optional[float] = None  # ATR at position entry

    def pnl(self, current_price: float) -> float:
        """Calculate unrealized PnL percentage."""
        if self.side == "long":
            return (current_price - self.entry_price) / self.entry_price * 100
        else:
            return (self.entry_price - current_price) / self.entry_price * 100

    def pnl_usd(self, current_price: float) -> float:
        """Calculate unrealized PnL in USD."""
        if self.side == "long":
            return (current_price - self.entry_price) * self.size
        else:
            return (self.entry_price - current_price) * self.size

    def should_close(self, current_price: float) -> Optional[str]:
        """Check if position should be closed. Returns reason or None."""
        if self.side == "long":
            if current_price >= self.tp_price:
                return "take_profit"
            if current_price <= self.sl_price:
                return "stop_loss"
        else:
            if current_price <= self.tp_price:
                return "take_profit"
            if current_price >= self.sl_price:
                return "stop_loss"
        return None


# =============================================================================
# Symbol Scanner
# =============================================================================

class SymbolScanner:
    """
    Scans available symbols and scores them for each strategy.
    Each strategy gets a different "best" symbol based on market conditions.
    """

    def __init__(self, info: Info, logger: logging.Logger, config: MultiConfig):
        self.info = info
        self.logger = logger
        self.config = config

        # Cache for prices and volumes
        self.symbol_data: Dict[str, Dict] = {}
        self.last_scan_time: Optional[datetime] = None

    def get_available_symbols(self) -> List[str]:
        """Get list of available perpetual symbols."""
        try:
            meta = self.info.meta()
            symbols = []
            for asset in meta.get("universe", []):
                name = asset.get("name")
                if name:
                    symbols.append(name)
            return symbols
        except Exception as e:
            self.logger.error(f"Failed to get symbols: {e}")
            return []

    async def scan_symbols(self, excluded_symbols: Set[str] = None) -> Dict[str, Dict]:
        """
        Scan top symbols and collect price data.
        Returns dict of symbol -> {prices, volume, current_price}
        """
        excluded = excluded_symbols or set()
        all_symbols = self.get_available_symbols()

        self.logger.info(f"Scanning {len(all_symbols)} symbols...")

        # Filter by volume (would need API call for each - simplified for now)
        # For testnet, use a predefined list of popular symbols
        popular_symbols = ["ETH", "BTC", "SOL", "AVAX", "MATIC", "ARB", "OP", "DOGE", "LINK", "UNI"]
        symbols_to_scan = [s for s in popular_symbols if s in all_symbols and s not in excluded][:self.config.top_symbols_count]

        symbol_data = {}

        for symbol in symbols_to_scan:
            try:
                # Fetch 1-minute candles (last 100)
                end_time = int(time.time() * 1000)
                start_time = end_time - (100 * 60 * 1000)

                candles = self.info.candles_snapshot(symbol, "1m", start_time, end_time)

                if candles and len(candles) >= 50:
                    prices = [float(c["c"]) for c in candles]
                    volumes = [float(c["v"]) for c in candles]

                    symbol_data[symbol] = {
                        "prices": prices,
                        "volume_24h": sum(volumes[-24*60:]) if len(volumes) >= 24*60 else sum(volumes),
                        "current_price": prices[-1],
                        "volatility": self._calculate_volatility(prices)
                    }

            except Exception as e:
                self.logger.debug(f"Failed to fetch {symbol}: {e}")

        self.symbol_data = symbol_data
        self.last_scan_time = datetime.now()

        self.logger.info(f"Scanned {len(symbol_data)} symbols successfully")
        return symbol_data

    def _calculate_volatility(self, prices: List[float]) -> float:
        """Calculate price volatility (standard deviation of returns)."""
        if len(prices) < 2:
            return 0

        returns = [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, len(prices))]
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        return variance ** 0.5

    def score_for_momentum(self, symbol: str, data: Dict) -> float:
        """
        Score symbol for momentum strategy.
        Prefers: Strong trend, high volume, moderate volatility
        """
        prices = data["prices"]

        # Trend strength (price vs 20-bar SMA)
        sma20 = sum(prices[-20:]) / 20 if len(prices) >= 20 else prices[-1]
        trend_strength = abs(prices[-1] - sma20) / sma20

        # Volume factor
        volume_factor = min(data.get("volume_24h", 0) / 1_000_000, 10)  # Cap at 10

        # Volatility (want moderate)
        volatility = data.get("volatility", 0)
        vol_score = 1 - abs(volatility - 0.01)  # Ideal around 1%

        return trend_strength * 100 + volume_factor + vol_score * 10

    def score_for_mean_reversion(self, symbol: str, data: Dict) -> float:
        """
        Score symbol for mean reversion strategy.
        Prefers: Extreme RSI, price at Bollinger Band, lower volatility
        """
        prices = data["prices"]

        # Calculate RSI extremity
        rsi = self._calculate_rsi(prices)
        rsi_extremity = max(abs(rsi - 50) - 20, 0) if rsi else 0  # Score > 0 when RSI < 30 or > 70

        # Calculate Bollinger Band distance
        bb = self._calculate_bb(prices)
        if bb:
            lower, middle, upper = bb
            current = prices[-1]
            if current < lower:
                bb_score = (lower - current) / lower * 100
            elif current > upper:
                bb_score = (current - upper) / upper * 100
            else:
                bb_score = 0
        else:
            bb_score = 0

        # Lower volatility is better for mean reversion
        vol_penalty = data.get("volatility", 0) * 100

        return rsi_extremity + bb_score - vol_penalty

    def score_for_breakout(self, symbol: str, data: Dict) -> float:
        """
        Score symbol for breakout strategy.
        Prefers: Price near highs/lows, high volatility, good volume
        """
        prices = data["prices"]
        lookback = min(20, len(prices) - 1)

        if lookback < 5:
            return 0

        lookback_prices = prices[-lookback-1:-1]
        current = prices[-1]
        high = max(lookback_prices)
        low = min(lookback_prices)

        # Distance to breakout level
        dist_to_high = (high - current) / high
        dist_to_low = (current - low) / low
        proximity_score = max(1 - min(dist_to_high, dist_to_low) * 100, 0)

        # High volatility is good for breakouts
        vol_bonus = data.get("volatility", 0) * 500

        # Volume
        volume_factor = min(data.get("volume_24h", 0) / 1_000_000, 10)

        return proximity_score + vol_bonus + volume_factor

    def _calculate_rsi(self, prices: List[float], period: int = 14) -> Optional[float]:
        """Calculate RSI."""
        if len(prices) < period + 1:
            return None

        changes = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        gains = [max(0, c) for c in changes[-period:]]
        losses = [abs(min(0, c)) for c in changes[-period:]]

        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def _calculate_bb(self, prices: List[float], period: int = 20, std_dev: float = 2.0) -> Optional[tuple]:
        """Calculate Bollinger Bands."""
        if len(prices) < period:
            return None

        recent = prices[-period:]
        middle = sum(recent) / period
        variance = sum((p - middle) ** 2 for p in recent) / period
        std = variance ** 0.5

        return (middle - std_dev * std, middle, middle + std_dev * std)

    def find_best_symbols(self, excluded: Set[str] = None) -> Dict[str, str]:
        """
        Find best symbol for each strategy.
        Returns: {"momentum": "ETH", "mean_reversion": "BTC", "breakout": "SOL"}
        """
        excluded = excluded or set()
        results = {}
        used_symbols = set(excluded)

        # Score all symbols for each strategy
        strategy_scores = {
            "momentum": {},
            "mean_reversion": {},
            "breakout": {}
        }

        for symbol, data in self.symbol_data.items():
            if symbol in excluded:
                continue
            strategy_scores["momentum"][symbol] = self.score_for_momentum(symbol, data)
            strategy_scores["mean_reversion"][symbol] = self.score_for_mean_reversion(symbol, data)
            strategy_scores["breakout"][symbol] = self.score_for_breakout(symbol, data)

        # Assign best symbol to each strategy (no overlap)
        for strategy in ["momentum", "mean_reversion", "breakout"]:
            scores = strategy_scores[strategy]
            # Sort by score descending
            sorted_symbols = sorted(scores.items(), key=lambda x: x[1], reverse=True)

            for symbol, score in sorted_symbols:
                if symbol not in used_symbols:
                    results[strategy] = symbol
                    used_symbols.add(symbol)
                    self.logger.info(f"  {strategy}: {symbol} (score: {score:.2f})")
                    break

        return results


# =============================================================================
# Strategy Runner
# =============================================================================

class StrategyRunner:
    """
    Manages a single strategy - handles entries, exits, and P&L tracking.
    """

    def __init__(
        self,
        name: str,
        config: StrategyConfig,
        global_config: "MultiConfig",
        info: Info,
        exchange: Exchange,
        logger: logging.Logger,
        db: Optional[Database],
        dry_run: bool = False
    ):
        self.name = name
        self.config = config
        self.global_config = global_config
        self.info = info
        self.exchange = exchange
        self.logger = logger
        self.db = db
        self.dry_run = dry_run

        # State
        self.symbol: Optional[str] = None
        self.prices: List[float] = []
        self.position: Optional[Position] = None
        self.last_order_time: Optional[datetime] = None
        self.current_trade_id = None

        # Stats
        self.trades_count = 0
        self.wins = 0
        self.losses = 0
        self.total_pnl = Decimal("0")  # USD

        # Create strategy instance
        self.strategy = self._create_strategy()

        self.logger.info(f"[{name.upper()}] Strategy runner initialized")

    def _create_strategy(self):
        """Create the strategy instance."""
        strategies = {
            "momentum": MomentumStrategy,
            "mean_reversion": MeanReversionStrategy,
            "breakout": BreakoutStrategy,
        }
        return strategies[self.name](self.config.params)

    def set_symbol(self, symbol: str):
        """Set the symbol to trade."""
        if self.symbol != symbol:
            self.logger.info(f"[{self.name.upper()}] Symbol changed: {self.symbol} -> {symbol}")
            self.symbol = symbol
            self.prices = []  # Reset price cache

    async def fetch_prices(self):
        """Fetch recent price data for the current symbol."""
        if not self.symbol:
            return

        try:
            end_time = int(time.time() * 1000)
            start_time = end_time - (100 * 60 * 1000)

            candles = self.info.candles_snapshot(self.symbol, "1m", start_time, end_time)

            if candles:
                self.prices = [float(c["c"]) for c in candles]
        except Exception as e:
            self.logger.error(f"[{self.name.upper()}] Failed to fetch prices: {e}")

    def get_current_price(self) -> Optional[float]:
        """Get the most recent price."""
        return self.prices[-1] if self.prices else None

    def calculate_position_size(self, current_price: float) -> float:
        """
        Calculate position size based on ATR volatility and risk parameters.
        
        Formula:
        - Risk Amount = Account Equity * risk_per_trade
        - SL Distance = ATR * atr_sl_multiplier (in price terms)
        - Position Size USD = Risk Amount / (SL Distance / current_price)
        
        Falls back to config.position_size_usd if ATR unavailable.
        
        Returns:
            Position size in USD, capped at max_position_size_usd
        """
        # Calculate ATR for volatility-based sizing
        atr = calculate_atr(self.prices, self.global_config.atr_period)
        
        if atr is None or atr <= 0:
            # Fallback to static position size if ATR unavailable
            self.logger.debug(
                f"[{self.name.upper()}] ATR unavailable, using static size: "
                f"${self.config.position_size_usd:.2f}"
            )
            return min(self.config.position_size_usd, self.global_config.max_position_size_usd)
        
        # Get account equity (estimate from position size as fallback)
        # In production, this should come from actual account balance
        try:
            user_state = self.info.user_state(self.exchange.wallet.address)
            account_equity = float(user_state.get("marginSummary", {}).get("accountValue", 0))
            if account_equity <= 0:
                account_equity = self.config.position_size_usd * 10  # Fallback estimate
        except Exception as e:
            self.logger.warning(f"[{self.name.upper()}] Failed to get account equity: {e}")
            account_equity = self.config.position_size_usd * 10  # Fallback estimate
        
        # Calculate risk amount (e.g., 1% of account)
        risk_amount = account_equity * self.global_config.risk_per_trade
        
        # Calculate SL distance in price terms
        sl_distance = atr * self.global_config.atr_sl_multiplier
        
        # Calculate position size in USD
        # Position Size = Risk Amount / (SL Distance / Price)
        # This ensures that if price moves by SL distance, loss = risk_amount
        if sl_distance > 0:
            position_size_usd = risk_amount / (sl_distance / current_price)
        else:
            position_size_usd = self.config.position_size_usd
        
        # Apply maximum cap
        position_size_usd = min(position_size_usd, self.global_config.max_position_size_usd)
        
        # Also respect the strategy-specific position size as a minimum bound
        # (but not exceeding the max cap)
        position_size_usd = max(position_size_usd, self.config.position_size_usd * 0.5)
        position_size_usd = min(position_size_usd, self.global_config.max_position_size_usd)
        
        self.logger.info(
            f"[{self.name.upper()}] ATR-based sizing: ATR=${atr:.4f}, "
            f"SL_dist=${sl_distance:.4f}, Risk=${risk_amount:.2f}, "
            f"Size=${position_size_usd:.2f}"
        )
        
        return position_size_usd

    async def sync_position(self):
        """Sync position state from exchange."""
        if not self.symbol:
            return

        # In dry-run mode, don't sync from exchange (we manage positions locally)
        if self.dry_run:
            return

        try:
            private_key = os.getenv("HYPERLIQUID_PRIVATE_KEY") or os.getenv("PRIVATE_KEY")
            account = Account.from_key(private_key)

            user_state = self.info.user_state(account.address)
            positions = user_state.get("assetPositions", [])

            for pos in positions:
                pos_info = pos.get("position", {})
                coin = pos_info.get("coin")

                if coin == self.symbol:
                    size = float(pos_info.get("szi", 0))
                    entry_px = float(pos_info.get("entryPx", 0))

                    if abs(size) > 0.0001:
                        side = "long" if size > 0 else "short"

                        if self.position is None:
                            self.logger.info(f"[{self.name.upper()}] Detected position: {side} {abs(size)} {self.symbol} @ ${entry_px:.2f}")

                            if side == "long":
                                tp_price = entry_px * (1 + self.config.tp_pct)
                                sl_price = entry_px * (1 - self.config.sl_pct)
                            else:
                                tp_price = entry_px * (1 - self.config.tp_pct)
                                sl_price = entry_px * (1 + self.config.sl_pct)

                            self.position = Position(
                                symbol=coin,
                                strategy=self.name,
                                side=side,
                                size=abs(size),
                                entry_price=entry_px,
                                entry_time=datetime.now(),
                                tp_price=tp_price,
                                sl_price=sl_price
                            )
                        return

            if self.position is not None and self.position.symbol == self.symbol:
                self.logger.info(f"[{self.name.upper()}] Position closed externally")
                self.position = None

        except Exception as e:
            self.logger.error(f"[{self.name.upper()}] Failed to sync position: {e}")

    async def open_position(self, side: str):
        """Open a new position."""
        if not self.symbol:
            return

        if self.position is not None:
            self.logger.debug(f"[{self.name.upper()}] Already have a position")
            return

        if self.last_order_time:
            elapsed = (datetime.now() - self.last_order_time).total_seconds()
            if elapsed < 60:  # Hardcoded cooldown
                return

        current_price = self.get_current_price()
        if not current_price:
            return

        # Calculate ATR for dynamic stop loss
        atr_value = None
        if self.config.use_atr_sl and len(self.prices) >= self.config.atr_period + 1:
            atr_value = calculate_atr(self.prices, self.config.atr_period)
            if atr_value:
                self.logger.debug(
                    f"[{self.name.upper()}] ATR({self.config.atr_period}): ${atr_value:.4f} "
                    f"({atr_value/current_price*100:.2f}% of price)"
                )

        # Calculate position size using ATR-based volatility sizing
        position_size_usd = self.calculate_position_size(current_price)
        size = position_size_usd / current_price

        try:
            meta = self.info.meta()
            for asset in meta.get("universe", []):
                if asset.get("name") == self.symbol:
                    sz_decimals = asset.get("szDecimals", 4)
                    size = round(size, sz_decimals)
                    break
        except:
            size = round(size, 4)

        # Calculate TP/SL - use ATR-based SL if available, otherwise fall back to percentage
        if side == "long":
            tp_price = current_price * (1 + self.config.tp_pct)
            if atr_value and self.config.use_atr_sl:
                # ATR-based SL: Entry - (ATR * multiplier)
                sl_price = current_price - (atr_value * self.config.atr_multiplier)
            else:
                sl_price = current_price * (1 - self.config.sl_pct)
            is_buy = True
        else:
            tp_price = current_price * (1 - self.config.tp_pct)
            if atr_value and self.config.use_atr_sl:
                # ATR-based SL: Entry + (ATR * multiplier)
                sl_price = current_price + (atr_value * self.config.atr_multiplier)
            else:
                sl_price = current_price * (1 + self.config.sl_pct)
            is_buy = False

        # Log SL method used
        sl_method = "ATR" if (atr_value and self.config.use_atr_sl) else "PCT"
        sl_distance_pct = abs(current_price - sl_price) / current_price * 100

        self.logger.info(
            f"[{self.name.upper()}] Opening {side.upper()}: {size} {self.symbol} @ ~${current_price:.2f} "
            f"(TP: ${tp_price:.2f}, SL: ${sl_price:.2f} [{sl_method}, {sl_distance_pct:.2f}%])"
        )

        # Insert signal to database
        signal_id = None
        if self.db:
            try:
                signal_id = await self.db.insert_signal({
                    "symbol": self.symbol,
                    "strategy": self.name,
                    "side": "BUY" if side == "long" else "SELL",
                    "signal_type": "ENTRY",
                    "confidence": None,
                    "reason": f"Strategy: {self.name}"
                })
            except Exception as e:
                self.logger.warning(f"[{self.name.upper()}] Failed to insert signal: {e}")

        if self.dry_run:
            self.logger.info(f"[{self.name.upper()}] [DRY RUN] Order would be placed")
            self.position = Position(
                symbol=self.symbol,
                strategy=self.name,
                side=side,
                size=size,
                entry_price=current_price,
                entry_time=datetime.now(),
                tp_price=tp_price,
                sl_price=sl_price,
                atr_value=atr_value
            )
            self.last_order_time = datetime.now()

            if self.db:
                try:
                    self.current_trade_id = await self.db.create_trade({
                        "symbol": self.symbol,
                        "side": side.upper(),
                        "size": Decimal(str(size)),
                        "entry_price": Decimal(str(current_price)),
                        "entry_time": datetime.now(),
                        "entry_fill_ids": [],
                        "strategy": self.name
                    })
                except Exception as e:
                    self.logger.warning(f"[{self.name.upper()}] Failed to create trade: {e}")
            return

        try:
            result = self.exchange.market_open(self.symbol, is_buy, size, None, 0.01)

            if result.get("status") == "ok":
                fill_statuses = result.get("response", {}).get("data", {}).get("statuses", [])
                fill_price = current_price

                for status in fill_statuses:
                    if "filled" in status:
                        fill_price = float(status["filled"]["avgPx"])
                        break

                # Recalculate TP/SL based on actual fill price
                if side == "long":
                    tp_price = fill_price * (1 + self.config.tp_pct)
                    if atr_value and self.config.use_atr_sl:
                        sl_price = fill_price - (atr_value * self.config.atr_multiplier)
                    else:
                        sl_price = fill_price * (1 - self.config.sl_pct)
                else:
                    tp_price = fill_price * (1 - self.config.tp_pct)
                    if atr_value and self.config.use_atr_sl:
                        sl_price = fill_price + (atr_value * self.config.atr_multiplier)
                    else:
                        sl_price = fill_price * (1 + self.config.sl_pct)

                self.position = Position(
                    symbol=self.symbol,
                    strategy=self.name,
                    side=side,
                    size=size,
                    entry_price=fill_price,
                    entry_time=datetime.now(),
                    tp_price=tp_price,
                    sl_price=sl_price,
                    atr_value=atr_value
                )

                self.last_order_time = datetime.now()
                self.logger.info(f"[{self.name.upper()}] Position opened @ ${fill_price:.2f}")

                if self.db:
                    try:
                        self.current_trade_id = await self.db.create_trade({
                            "symbol": self.symbol,
                            "side": side.upper(),
                            "size": Decimal(str(size)),
                            "entry_price": Decimal(str(fill_price)),
                            "entry_time": datetime.now(),
                            "entry_fill_ids": [],
                            "strategy": self.name
                        })

                        if signal_id:
                            await self.db.mark_signal_executed(signal_id, 0, Decimal(str(fill_price)))
                    except Exception as e:
                        self.logger.warning(f"[{self.name.upper()}] Failed to create trade: {e}")
            else:
                error = result.get("response", {}).get("data", {}).get("statuses", [])
                self.logger.error(f"[{self.name.upper()}] Order failed: {error}")

        except Exception as e:
            self.logger.error(f"[{self.name.upper()}] Failed to open position: {e}")

    async def close_position(self, reason: str):
        """Close current position."""
        if self.position is None:
            return

        current_price = self.get_current_price()
        pnl_pct = self.position.pnl(current_price) if current_price else 0
        pnl_usd = self.position.pnl_usd(current_price) if current_price else 0

        self.logger.info(
            f"[{self.name.upper()}] Closing {self.position.side.upper()} ({reason}): "
            f"PnL: {pnl_pct:+.2f}% (${pnl_usd:+.2f})"
        )

        if self.dry_run:
            self.trades_count += 1
            self.total_pnl += Decimal(str(pnl_usd))
            if pnl_usd > 0:
                self.wins += 1
            else:
                self.losses += 1

            if self.db and self.current_trade_id:
                try:
                    entry_time = self.position.entry_time
                    exit_time = datetime.now()
                    duration_seconds = int((exit_time - entry_time).total_seconds())

                    await self.db.close_trade(
                        trade_id=self.current_trade_id,
                        exit_price=Decimal(str(current_price)),
                        exit_time=exit_time,
                        exit_fill_ids=[],
                        gross_pnl=Decimal(str(pnl_usd)),
                        fees=Decimal("0"),
                        net_pnl=Decimal(str(pnl_usd)),
                        duration_seconds=duration_seconds
                    )
                    self.current_trade_id = None
                except Exception as e:
                    self.logger.warning(f"[{self.name.upper()}] Failed to close trade in DB: {e}")

            self.position = None
            return

        try:
            result = self.exchange.market_close(
                coin=self.position.symbol,
                sz=self.position.size,
                slippage=0.01
            )

            if result.get("status") == "ok":
                fill_statuses = result.get("response", {}).get("data", {}).get("statuses", [])
                exit_price = current_price

                for status in fill_statuses:
                    if "filled" in status:
                        exit_price = float(status["filled"]["avgPx"])
                        break

                # Recalculate PnL with actual exit price
                if self.position.side == "long":
                    pnl_usd = (exit_price - self.position.entry_price) * self.position.size
                else:
                    pnl_usd = (self.position.entry_price - exit_price) * self.position.size

                self.trades_count += 1
                self.total_pnl += Decimal(str(pnl_usd))
                if pnl_usd > 0:
                    self.wins += 1
                else:
                    self.losses += 1

                self.logger.info(f"[{self.name.upper()}] Position closed @ ${exit_price:.2f}")

                if self.db and self.current_trade_id:
                    try:
                        entry_time = self.position.entry_time
                        exit_time = datetime.now()
                        duration_seconds = int((exit_time - entry_time).total_seconds())
                        fees = Decimal(str(self.position.size * exit_price * 0.0005))

                        await self.db.close_trade(
                            trade_id=self.current_trade_id,
                            exit_price=Decimal(str(exit_price)),
                            exit_time=exit_time,
                            exit_fill_ids=[],
                            gross_pnl=Decimal(str(pnl_usd)),
                            fees=fees,
                            net_pnl=Decimal(str(pnl_usd)) - fees,
                            duration_seconds=duration_seconds
                        )
                        self.current_trade_id = None
                    except Exception as e:
                        self.logger.warning(f"[{self.name.upper()}] Failed to close trade in DB: {e}")

                self.position = None
            else:
                error = result.get("response", {}).get("data", {}).get("statuses", [])
                self.logger.error(f"[{self.name.upper()}] Close order failed: {error}")

        except Exception as e:
            self.logger.error(f"[{self.name.upper()}] Failed to close position: {e}")

    async def check_exits(self):
        """Check if current position should be closed."""
        if self.position is None:
            return

        current_price = self.get_current_price()
        if not current_price:
            return

        close_reason = self.position.should_close(current_price)
        if close_reason:
            await self.close_position(close_reason)

    async def check_entries(self):
        """Check if we should open a new position."""
        if self.position is not None:
            return

        if len(self.prices) < 50:
            return

        signal = self.strategy.evaluate(self.prices)

        if signal == Signal.LONG:
            await self.open_position("long")
        elif signal == Signal.SHORT:
            await self.open_position("short")

    async def run_cycle(self):
        """Run one trading cycle."""
        if not self.symbol:
            return

        try:
            await self.fetch_prices()
            await self.sync_position()
            await self.check_exits()
            await self.check_entries()
        except Exception as e:
            self.logger.error(f"[{self.name.upper()}] Error in cycle: {e}")

    def get_stats(self) -> Dict:
        """Get strategy statistics."""
        current_price = self.get_current_price()
        unrealized = Decimal("0")

        if self.position and current_price:
            unrealized = Decimal(str(self.position.pnl_usd(current_price)))

        return {
            "strategy": self.name,
            "symbol": self.symbol,
            "trades": self.trades_count,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.wins / self.trades_count * 100, 1) if self.trades_count > 0 else 0,
            "realized_pnl": float(self.total_pnl),
            "unrealized_pnl": float(unrealized),
            "total_pnl": float(self.total_pnl + unrealized),
            "has_position": self.position is not None,
            "position": {
                "side": self.position.side,
                "size": self.position.size,
                "entry_price": self.position.entry_price,
                "pnl_pct": self.position.pnl(current_price) if current_price else 0
            } if self.position else None
        }


# =============================================================================
# Multi-Strategy Bot
# =============================================================================

class MultiStrategyBot:
    """
    Main bot that coordinates multiple strategy runners.
    """

    def __init__(self, config: MultiConfig, dry_run: bool = False, enable_optimization: bool = True):
        self.config = config
        self.dry_run = dry_run
        self.enable_optimization = enable_optimization and OPTIMIZATION_AVAILABLE
        self.logger = setup_logging(config)

        # Hyperliquid clients
        self.exchange: Optional[Exchange] = None
        self.info: Optional[Info] = None

        # Database
        self.db: Optional[Database] = None

        # Components
        self.scanner: Optional[SymbolScanner] = None
        self.runners: Dict[str, StrategyRunner] = {}

        # Optimization components
        self.config_manager: Optional[HotReloadConfigManager] = None
        self.optimizer: Optional[OptimizationOrchestrator] = None

        # State
        self.running = False
        self.last_scan_time: Optional[datetime] = None
        self.scan_interval = 300  # Rescan symbols every 5 minutes

        self._print_banner()

    def _print_banner(self):
        """Print startup banner."""
        self.logger.info("=" * 60)
        self.logger.info("Multi-Strategy Trading Bot")
        self.logger.info("=" * 60)
        self.logger.info(f"Strategies: momentum, mean_reversion, breakout")
        self.logger.info(f"Dry run: {self.dry_run}")
        self.logger.info(f"Auto-optimization: {self.enable_optimization}")
        self.logger.info("=" * 60)

    async def _on_config_update(self, new_config: Dict):
        """
        Handle hot-reload of configuration from optimizer.
        Updates strategy parameters without stopping trades.
        """
        self.logger.info("[HOT-RELOAD] Received new configuration")

        # Update each strategy runner
        for name, runner in self.runners.items():
            strategy_config = new_config.get(name, {})

            # Update global parameters
            runner.config.tp_pct = new_config.get('tp_pct', runner.config.tp_pct)
            runner.config.sl_pct = new_config.get('sl_pct', runner.config.sl_pct)
            runner.config.position_size_usd = strategy_config.get(
                'position_size_usd',
                new_config.get('position_size_usd', runner.config.position_size_usd)
            )

            # Check if strategy should be disabled
            if not strategy_config.get('enabled', True):
                self.logger.warning(f"[HOT-RELOAD] {name.upper()} strategy DISABLED by optimizer")
                runner.config.enabled = False
            else:
                runner.config.enabled = True

            # Update strategy-specific parameters
            runner.config.params = strategy_config

            # Recreate strategy instance with new parameters
            runner.strategy = runner._create_strategy()

            self.logger.info(
                f"[HOT-RELOAD] {name.upper()} updated: "
                f"TP={runner.config.tp_pct*100:.2f}%, "
                f"SL={runner.config.sl_pct*100:.2f}%, "
                f"enabled={runner.config.enabled}"
            )

            # Update TP/SL for open positions (if any)
            if runner.position is not None:
                current_price = runner.get_current_price()
                if current_price:
                    if runner.position.side == "long":
                        runner.position.tp_price = runner.position.entry_price * (1 + runner.config.tp_pct)
                        runner.position.sl_price = runner.position.entry_price * (1 - runner.config.sl_pct)
                    else:
                        runner.position.tp_price = runner.position.entry_price * (1 - runner.config.tp_pct)
                        runner.position.sl_price = runner.position.entry_price * (1 + runner.config.sl_pct)

                    self.logger.info(
                        f"[HOT-RELOAD] {name.upper()} position TP/SL updated: "
                        f"TP=${runner.position.tp_price:.2f}, SL=${runner.position.sl_price:.2f}"
                    )

        self.logger.info("[HOT-RELOAD] Configuration update complete")

    def _init_clients(self):
        """Initialize Hyperliquid clients."""
        private_key = os.getenv("HYPERLIQUID_PRIVATE_KEY") or os.getenv("PRIVATE_KEY")
        if not private_key:
            raise ValueError("PRIVATE_KEY not found in environment")

        base_url = constants.TESTNET_API_URL if self.config.testnet else constants.MAINNET_API_URL

        self.logger.info(f"Connecting to {'TESTNET' if self.config.testnet else 'MAINNET'}: {base_url}")

        account = Account.from_key(private_key)
        self.logger.info(f"Wallet address: {account.address}")

        self.info = Info(base_url, skip_ws=True)
        self.exchange = Exchange(account, base_url)

        try:
            user_state = self.info.user_state(account.address)
            margin = float(user_state.get("marginSummary", {}).get("accountValue", 0))
            self.logger.info(f"Account value: ${margin:.2f}")
        except Exception as e:
            self.logger.error(f"Failed to connect: {e}")
            raise

    async def _init_strategies(self):
        """Initialize strategy runners."""
        for name, strat_config in self.config.strategies.items():
            if strat_config.enabled:
                self.runners[name] = StrategyRunner(
                    name=name,
                    config=strat_config,
                    global_config=self.config,
                    info=self.info,
                    exchange=self.exchange,
                    logger=self.logger,
                    db=self.db,
                    dry_run=self.dry_run
                )

        self.logger.info(f"Initialized {len(self.runners)} strategy runners")

    async def scan_and_assign_symbols(self):
        """Scan symbols and assign best one to each strategy."""
        self.logger.info("Scanning symbols...")

        # Get currently assigned symbols (to exclude from reassignment if they have positions)
        excluded = set()
        for name, runner in self.runners.items():
            if runner.position is not None:
                excluded.add(runner.symbol)

        # Scan symbols
        await self.scanner.scan_symbols(excluded)

        # Find best symbols
        best_symbols = self.scanner.find_best_symbols(excluded)

        # Assign to runners (only if they don't have a position)
        for name, runner in self.runners.items():
            if runner.position is None:
                symbol = best_symbols.get(name)
                if symbol:
                    runner.set_symbol(symbol)

        self.last_scan_time = datetime.now()

    async def sync_to_database(self):
        """Sync current state to database."""
        if not self.db:
            return

        try:
            private_key = os.getenv("HYPERLIQUID_PRIVATE_KEY") or os.getenv("PRIVATE_KEY")
            account = Account.from_key(private_key)

            user_state = self.info.user_state(account.address)

            # Update account
            margin_summary = user_state.get("marginSummary", {})
            equity = Decimal(str(margin_summary.get("accountValue", 0)))
            margin_used = Decimal(str(margin_summary.get("totalMarginUsed", 0)))
            available_balance = equity - margin_used

            unrealized_pnl = Decimal("0")
            for pos in user_state.get("assetPositions", []):
                pos_info = pos.get("position", {})
                upnl = pos_info.get("unrealizedPnl", 0)
                if upnl:
                    unrealized_pnl += Decimal(str(upnl))

            await self.db.update_account(
                equity=equity,
                available_balance=available_balance,
                margin_used=margin_used,
                unrealized_pnl=unrealized_pnl
            )

            # Update positions
            positions_data = []
            for pos in user_state.get("assetPositions", []):
                pos_info = pos.get("position", {})
                size = float(pos_info.get("szi", 0))

                if abs(size) > 0.0001:
                    positions_data.append({
                        "symbol": pos_info.get("coin"),
                        "side": "LONG" if size > 0 else "SHORT",
                        "size": Decimal(str(abs(size))),
                        "entry_price": Decimal(str(pos_info.get("entryPx", 0))),
                        "mark_price": Decimal(str(pos_info.get("positionValue", 0))) / Decimal(str(abs(size))) if abs(size) > 0 else Decimal("0"),
                        "unrealized_pnl": Decimal(str(pos_info.get("unrealizedPnl", 0))),
                        "leverage": int(pos_info.get("leverage", {}).get("value", 1)),
                        "liquidation_price": Decimal(str(pos_info.get("liquidationPx", 0))) if pos_info.get("liquidationPx") else None,
                        "margin_used": Decimal(str(pos_info.get("marginUsed", 0)))
                    })

            await self.db.upsert_positions(positions_data)

            # Update open orders
            open_orders = self.info.open_orders(account.address)
            orders_data = []
            for order in open_orders:
                raw_side = order.get("side", "").upper()
                db_side = "BUY" if raw_side == "B" else "SELL"
                orders_data.append({
                    "order_id": int(order.get("oid", 0)),
                    "symbol": order.get("coin"),
                    "side": db_side,
                    "size": Decimal(str(order.get("sz", 0))),
                    "price": Decimal(str(order.get("limitPx", 0))),
                    "order_type": order.get("orderType", "limit"),
                    "reduce_only": order.get("reduceOnly", False),
                    "created_at": datetime.fromtimestamp(order.get("timestamp", 0) / 1000) if order.get("timestamp") else datetime.now()
                })

            await self.db.upsert_orders(orders_data)

        except Exception as e:
            self.logger.warning(f"Failed to sync to database: {e}")

    async def run_cycle(self):
        """Run one trading cycle for all strategies."""
        try:
            # Rescan symbols periodically
            if self.last_scan_time is None or (datetime.now() - self.last_scan_time).total_seconds() > self.scan_interval:
                await self.scan_and_assign_symbols()

            # Run all strategy cycles in parallel
            await asyncio.gather(*[runner.run_cycle() for runner in self.runners.values()])

            # Sync to database
            await self.sync_to_database()

            # Log status
            self._log_status()

        except Exception as e:
            self.logger.error(f"Error in trading cycle: {e}", exc_info=True)

    def _log_status(self):
        """Log current status of all strategies."""
        status_lines = []
        total_pnl = Decimal("0")

        for name, runner in self.runners.items():
            stats = runner.get_stats()
            total_pnl += Decimal(str(stats["total_pnl"]))

            if stats["has_position"]:
                pos = stats["position"]
                status_lines.append(
                    f"  {name.upper()}: {stats['symbol']} | {pos['side'].upper()} @ ${pos['entry_price']:.2f} | "
                    f"PnL: {pos['pnl_pct']:+.2f}% | Trades: {stats['trades']} | Total: ${stats['total_pnl']:+.2f}"
                )
            else:
                status_lines.append(
                    f"  {name.upper()}: {stats['symbol'] or 'None'} | No position | "
                    f"Trades: {stats['trades']} | Total: ${stats['total_pnl']:+.2f}"
                )

        self.logger.info("-" * 60)
        for line in status_lines:
            self.logger.info(line)
        self.logger.info(f"  TOTAL P&L: ${float(total_pnl):+.2f}")
        self.logger.info("-" * 60)

    async def start(self):
        """Start the bot."""
        self.logger.info("Starting multi-strategy bot...")

        # Initialize clients
        self._init_clients()

        # Connect to database
        if DB_AVAILABLE:
            try:
                self.db = Database()
                await self.db.connect(min_size=1, max_size=5)
                self.logger.info("[DB] Connected to PostgreSQL")
            except Exception as e:
                self.logger.warning(f"[DB] Failed to connect to database: {e}")
                self.db = None

        # Initialize scanner
        self.scanner = SymbolScanner(self.info, self.logger, self.config)

        # Initialize strategies
        await self._init_strategies()

        # Initialize optimization system (if enabled and DB available)
        if self.enable_optimization and self.db:
            try:
                config_path = Path(__file__).parent / "multi_config.yaml"

                # Initialize config manager
                self.config_manager = HotReloadConfigManager(str(config_path), self.db.pool)
                await self.config_manager.initialize()

                # Register callback for config updates
                self.config_manager.register_listener(self._on_config_update)

                # Initialize optimizer orchestrator
                self.optimizer = OptimizationOrchestrator(
                    db=self.db.pool,
                    info_client=self.info,
                    config_manager=self.config_manager,
                    logger_instance=self.logger,
                    min_confidence=0.6,
                    min_hours_between_optimizations=1
                )

                # Start optimizer in background
                await self.optimizer.start()

                self.logger.info("[OPTIMIZER] Auto-optimization service started")
                self.logger.info(f"[OPTIMIZER] Current parameter version: {self.config_manager.current_version}")

            except Exception as e:
                self.logger.warning(f"[OPTIMIZER] Failed to initialize optimization: {e}")
                self.config_manager = None
                self.optimizer = None

        # Initial symbol scan
        await self.scan_and_assign_symbols()

        self.running = True
        self.logger.info("Bot started. Press Ctrl+C to stop.")

        # Main loop
        cycle_interval = 5
        while self.running:
            await self.run_cycle()
            await asyncio.sleep(cycle_interval)

    async def stop_async(self):
        """Stop the bot gracefully (async version)."""
        self.logger.info("Stopping bot...")
        self.running = False

        # Stop optimizer first
        if self.optimizer:
            try:
                await self.optimizer.stop()
                self.logger.info("[OPTIMIZER] Stopped")
            except Exception as e:
                self.logger.warning(f"Error stopping optimizer: {e}")

        # Print final stats
        self.logger.info("=" * 60)
        self.logger.info("Final Statistics")
        self.logger.info("=" * 60)

        total_pnl = Decimal("0")
        for name, runner in self.runners.items():
            stats = runner.get_stats()
            total_pnl += Decimal(str(stats["total_pnl"]))
            self.logger.info(
                f"  {name.upper()}: Trades={stats['trades']}, "
                f"Win rate={stats['win_rate']:.1f}%, "
                f"PnL=${stats['total_pnl']:+.2f}"
            )

        self.logger.info(f"  TOTAL P&L: ${float(total_pnl):+.2f}")

        if self.config_manager:
            self.logger.info(f"  Final parameter version: {self.config_manager.current_version}")

        self.logger.info("=" * 60)

        # Disconnect database
        if self.db:
            try:
                await self.db.disconnect()
                self.logger.info("[DB] Disconnected")
            except Exception as e:
                self.logger.warning(f"Error disconnecting DB: {e}")

    def stop(self):
        """Stop the bot gracefully (sync wrapper)."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(self.stop_async())
            else:
                loop.run_until_complete(self.stop_async())
        except Exception:
            # Fallback for simpler shutdown
            self.running = False
            if self.db:
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(self.db.disconnect())
                    loop.close()
                except Exception:
                    pass


# =============================================================================
# Main Entry Point
# =============================================================================

def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Multi-Strategy Trading Bot")
    parser.add_argument(
        "--config",
        default="simple_bot/multi_config.yaml",
        help="Path to config file"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without placing real orders"
    )
    parser.add_argument(
        "--no-optimize",
        action="store_true",
        help="Disable auto-optimization with DeepSeek"
    )

    args = parser.parse_args()

    # Load .env
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ[key.strip()] = value.strip()

    # Load config
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config file not found: {config_path}")
        print("Creating default config...")

        # Create default config
        default_config = """# Multi-Strategy Bot Configuration
# =================================

# Symbol scanning
top_symbols_count: 10
min_volume_24h: 1000000

# Global settings
min_order_interval_seconds: 60
log_level: INFO
log_file: simple_bot/multi_bot.log
testnet: true

# Default position settings (can be overridden per strategy)
position_size_usd: 100
leverage: 5
tp_pct: 0.005   # 0.5%
sl_pct: 0.003   # 0.3%

# Strategy configurations
momentum:
  enabled: true
  position_size_usd: 100
  ema_fast: 20
  ema_slow: 50
  rsi_period: 14
  rsi_long_threshold: 55
  rsi_short_threshold: 45

mean_reversion:
  enabled: true
  position_size_usd: 100
  rsi_period: 14
  rsi_oversold: 30
  rsi_overbought: 70
  bb_period: 20
  bb_std: 2.0

breakout:
  enabled: true
  position_size_usd: 100
  lookback_bars: 20
  min_breakout_pct: 0.002
"""
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as f:
            f.write(default_config)
        print(f"Created {config_path}")

    config = MultiConfig.from_yaml(str(config_path))

    # Create and run bot
    bot = MultiStrategyBot(
        config,
        dry_run=args.dry_run,
        enable_optimization=not args.no_optimize
    )

    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        bot.stop()
    except Exception as e:
        logging.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
