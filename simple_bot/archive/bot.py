#!/usr/bin/env python3
"""
Simple Hyperliquid Trading Bot
==============================

A minimal, readable trading bot for Hyperliquid DEX.

Features:
- Single symbol trading
- Three strategies: Momentum, Mean Reversion, Breakout
- REST API for orders (reliable)
- WebSocket for real-time prices (fast)
- File-based logging (simple debugging)

Usage:
    python simple_bot/bot.py
    python simple_bot/bot.py --config simple_bot/config.yaml
    python simple_bot/bot.py --dry-run  # No real orders

Author: Francesco Carlesi
"""

import os
import sys
import time
import yaml
import logging
import asyncio
import argparse
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional, List, Any
from dataclasses import dataclass, field

from dotenv import load_dotenv
load_dotenv()

# Hyperliquid SDK
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from eth_account import Account

# Local imports
from strategies import Signal, create_strategy

# Database import (parent directory)
sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from database.db import Database
    DB_AVAILABLE = True
except ImportError:
    Database = None
    DB_AVAILABLE = False

# =============================================================================
# Configuration
# =============================================================================

@dataclass
class BotConfig:
    """Bot configuration loaded from YAML."""
    symbol: str = "ETH"
    strategy: str = "momentum"
    leverage: int = 5
    position_size_usd: float = 100.0
    tp_pct: float = 0.005  # 0.5%
    sl_pct: float = 0.003  # 0.3%
    trailing_stop_pct: float = 0.005  # 0.5% - trailing stop activates after breakeven
    min_order_interval_seconds: int = 60
    max_positions: int = 1
    log_level: str = "INFO"
    log_file: str = "simple_bot/bot.log"
    testnet: bool = False
    
    # Strategy configs (nested dicts)
    momentum: dict = field(default_factory=dict)
    mean_reversion: dict = field(default_factory=dict)
    breakout: dict = field(default_factory=dict)
    
    @classmethod
    def from_yaml(cls, path: str) -> "BotConfig":
        """Load config from YAML file."""
        with open(path, "r") as f:
            data = yaml.safe_load(f)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# =============================================================================
# Logging Setup
# =============================================================================

def setup_logging(config: BotConfig) -> logging.Logger:
    """Configure logging to file and console."""
    logger = logging.getLogger("simple_bot")
    logger.setLevel(getattr(logging, config.log_level.upper()))
    
    # Clear existing handlers
    logger.handlers.clear()
    
    # Format
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)
    
    # File handler
    log_path = Path(config.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    
    return logger


# =============================================================================
# Position & Order Tracking
# =============================================================================

@dataclass
class Position:
    """Tracks an open position."""
    symbol: str
    side: str  # "long" or "short"
    size: float
    entry_price: float
    entry_time: datetime
    tp_price: float
    sl_price: float
    trailing_stop_pct: float = 0.005  # Default 0.5%
    best_price: float = None  # Tracks highest (long) or lowest (short) price since entry
    trailing_stop_active: bool = False  # Only activates after position is in profit
    
    def __post_init__(self):
        """Initialize best_price to entry_price if not set."""
        if self.best_price is None:
            self.best_price = self.entry_price
    
    def pnl(self, current_price: float) -> float:
        """Calculate unrealized PnL."""
        if self.side == "long":
            return (current_price - self.entry_price) / self.entry_price * 100
        else:
            return (self.entry_price - current_price) / self.entry_price * 100
    
    def update_trailing_stop(self, current_price: float) -> None:
        """Update trailing stop level based on current price.
        
        For LONG: track highest price, SL moves up to (highest - trailing%)
        For SHORT: track lowest price, SL moves down to (lowest + trailing%)
        Trailing stop only activates after position is in profit (breakeven first)
        """
        if self.side == "long":
            # Check if we're in profit (price > entry)
            if current_price > self.entry_price:
                self.trailing_stop_active = True
            
            # Update best price if we have a new high
            if current_price > self.best_price:
                self.best_price = current_price
            
            # Calculate trailing stop level if active
            if self.trailing_stop_active:
                trailing_sl = self.best_price * (1 - self.trailing_stop_pct)
                # Only move SL up, never down (also ensure it's above original SL)
                if trailing_sl > self.sl_price:
                    self.sl_price = trailing_sl
        else:  # short
            # Check if we're in profit (price < entry)
            if current_price < self.entry_price:
                self.trailing_stop_active = True
            
            # Update best price if we have a new low
            if current_price < self.best_price:
                self.best_price = current_price
            
            # Calculate trailing stop level if active
            if self.trailing_stop_active:
                trailing_sl = self.best_price * (1 + self.trailing_stop_pct)
                # Only move SL down, never up (also ensure it's below original SL)
                if trailing_sl < self.sl_price:
                    self.sl_price = trailing_sl
    
    def should_close(self, current_price: float) -> Optional[str]:
        """Check if position should be closed. Returns reason or None."""
        if self.side == "long":
            if current_price >= self.tp_price:
                return "take_profit"
            if current_price <= self.sl_price:
                # Distinguish between regular SL and trailing stop
                if self.trailing_stop_active and self.sl_price > self.entry_price * (1 - self.trailing_stop_pct):
                    return "trailing_stop"
                return "stop_loss"
        else:  # short
            if current_price <= self.tp_price:
                return "take_profit"
            if current_price >= self.sl_price:
                # Distinguish between regular SL and trailing stop
                if self.trailing_stop_active and self.sl_price < self.entry_price * (1 + self.trailing_stop_pct):
                    return "trailing_stop"
                return "stop_loss"
        return None


# =============================================================================
# Simple Bot
# =============================================================================

class SimpleBot:
    """
    Simple trading bot for Hyperliquid.
    
    Lifecycle:
    1. __init__: Load config, create clients
    2. start(): Connect WebSocket, start main loop
    3. Main loop: fetch prices -> check exits -> check entries
    4. stop(): Clean shutdown
    """
    
    def __init__(self, config: BotConfig, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self.logger = setup_logging(config)
        
        # State
        self.position: Optional[Position] = None
        self.prices: List[float] = []  # Price history for indicators
        self.last_order_time: Optional[datetime] = None
        self.running = False
        
        # Stats
        self.trades_count = 0
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        
        # Hyperliquid clients (initialized in start())
        self.exchange: Optional[Exchange] = None
        self.info: Optional[Info] = None
        
        # Database (optional - bot works without it)
        self.db: Optional[Database] = None
        self.current_trade_id: Optional[Any] = None  # UUID del trade corrente
        
        # Strategy
        self.strategy = create_strategy(config.strategy, {
            "momentum": config.momentum,
            "mean_reversion": config.mean_reversion,
            "breakout": config.breakout,
        })
        
        self.logger.info("=" * 60)
        self.logger.info("Simple Hyperliquid Trading Bot")
        self.logger.info("=" * 60)
        self.logger.info(f"Symbol: {config.symbol}")
        self.logger.info(f"Strategy: {config.strategy}")
        self.logger.info(f"Position size: ${config.position_size_usd}")
        self.logger.info(f"Leverage: {config.leverage}x")
        self.logger.info(f"TP: {config.tp_pct*100:.2f}% | SL: {config.sl_pct*100:.2f}%")
        self.logger.info(f"Dry run: {dry_run}")
        self.logger.info("=" * 60)
    
    # =========================================================================
    # Initialization
    # =========================================================================
    
    def _init_clients(self):
        """Initialize Hyperliquid clients."""
        # Load private key from environment (accept both names)
        private_key = os.getenv("HYPERLIQUID_PRIVATE_KEY") or os.getenv("PRIVATE_KEY")
        if not private_key:
            raise ValueError("PRIVATE_KEY not found in environment")
        
        # Determine testnet/mainnet
        testnet = os.getenv("HYPERLIQUID_TESTNET", "false").lower() == "true"
        if self.config.testnet:
            testnet = True
        
        base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
        
        self.logger.info(f"Connecting to {'TESTNET' if testnet else 'MAINNET'}: {base_url}")
        
        # Create account from private key
        account = Account.from_key(private_key)
        self.logger.info(f"Wallet address: {account.address}")
        
        # Initialize clients
        self.info = Info(base_url, skip_ws=True)
        self.exchange = Exchange(account, base_url)
        
        # Verify connection by fetching account state
        try:
            user_state = self.info.user_state(account.address)
            margin = float(user_state.get("marginSummary", {}).get("accountValue", 0))
            self.logger.info(f"Account value: ${margin:.2f}")
        except Exception as e:
            self.logger.error(f"Failed to connect: {e}")
            raise
    
    def _get_symbol_info(self) -> dict:
        """Get symbol metadata (decimals, min size, etc.)."""
        meta = self.info.meta()
        for asset in meta.get("universe", []):
            if asset.get("name") == self.config.symbol:
                return asset
        raise ValueError(f"Symbol {self.config.symbol} not found")
    
    # =========================================================================
    # Price Data
    # =========================================================================
    
    async def fetch_prices(self):
        """Fetch recent price data for indicator calculation."""
        try:
            # Get 1-minute candles (last 100)
            # Using REST for reliability
            end_time = int(time.time() * 1000)
            start_time = end_time - (100 * 60 * 1000)  # 100 minutes ago
            
            candles = self.info.candles_snapshot(
                self.config.symbol,  # coin (positional)
                "1m",                # interval
                start_time,
                end_time
            )
            
            if candles:
                # Extract close prices
                self.prices = [float(c["c"]) for c in candles]
                current_price = self.prices[-1] if self.prices else 0
                self.logger.debug(f"Fetched {len(self.prices)} candles, current price: ${current_price:.2f}")
            else:
                self.logger.warning("No candles returned")
                
        except Exception as e:
            self.logger.error(f"Failed to fetch prices: {e}")
    
    def get_current_price(self) -> Optional[float]:
        """Get the most recent price."""
        return self.prices[-1] if self.prices else None
    
    # =========================================================================
    # Position Management
    # =========================================================================
    
    async def sync_position(self):
        """Sync position state from exchange."""
        try:
            private_key = os.getenv("HYPERLIQUID_PRIVATE_KEY") or os.getenv("PRIVATE_KEY")
            account = Account.from_key(private_key)
            
            user_state = self.info.user_state(account.address)
            positions = user_state.get("assetPositions", [])
            
            for pos in positions:
                pos_info = pos.get("position", {})
                coin = pos_info.get("coin")
                
                if coin == self.config.symbol:
                    size = float(pos_info.get("szi", 0))
                    entry_px = float(pos_info.get("entryPx", 0))
                    
                    if abs(size) > 0.0001:
                        # We have a position
                        side = "long" if size > 0 else "short"
                        
                        if self.position is None:
                            # Position opened externally or bot restarted
                            self.logger.info(f"Detected existing position: {side} {abs(size)} @ ${entry_px:.2f}")
                            
                            # Calculate TP/SL
                            if side == "long":
                                tp_price = entry_px * (1 + self.config.tp_pct)
                                sl_price = entry_px * (1 - self.config.sl_pct)
                            else:
                                tp_price = entry_px * (1 - self.config.tp_pct)
                                sl_price = entry_px * (1 + self.config.sl_pct)
                            
                            self.position = Position(
                                symbol=coin,
                                side=side,
                                size=abs(size),
                                entry_price=entry_px,
                                entry_time=datetime.now(),
                                tp_price=tp_price,
                                sl_price=sl_price,
                                trailing_stop_pct=self.config.trailing_stop_pct
                            )
                        return
            
            # No position found
            if self.position is not None:
                self.logger.info("Position closed externally")
                self.position = None
                
        except Exception as e:
            self.logger.error(f"Failed to sync position: {e}")

    async def sync_to_database(self):
        """
        Sync current state to database.
        Fetches account state, positions, orders from Hyperliquid and saves to DB.
        """
        if not self.db:
            return

        try:
            private_key = os.getenv("HYPERLIQUID_PRIVATE_KEY") or os.getenv("PRIVATE_KEY")
            account = Account.from_key(private_key)
            
            # Get user state from Hyperliquid
            user_state = self.info.user_state(account.address)
            
            # === 1. Update Account ===
            margin_summary = user_state.get("marginSummary", {})
            equity = Decimal(str(margin_summary.get("accountValue", 0)))
            margin_used = Decimal(str(margin_summary.get("totalMarginUsed", 0)))
            available_balance = equity - margin_used
            
            # Calculate unrealized PnL from positions
            unrealized_pnl = Decimal("0")
            asset_positions = user_state.get("assetPositions", [])
            for pos in asset_positions:
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
            
            # === 2. Update Positions ===
            positions_data = []
            for pos in asset_positions:
                pos_info = pos.get("position", {})
                size = float(pos_info.get("szi", 0))
                
                if abs(size) > 0.0001:
                    positions_data.append({
                        "symbol": pos_info.get("coin"),
                        "side": "LONG" if size > 0 else "SHORT",  # DB expects uppercase
                        "size": Decimal(str(abs(size))),
                        "entry_price": Decimal(str(pos_info.get("entryPx", 0))),
                        "mark_price": Decimal(str(pos_info.get("positionValue", 0))) / Decimal(str(abs(size))) if abs(size) > 0 else Decimal("0"),
                        "unrealized_pnl": Decimal(str(pos_info.get("unrealizedPnl", 0))),
                        "leverage": int(pos_info.get("leverage", {}).get("value", 1)),
                        "liquidation_price": Decimal(str(pos_info.get("liquidationPx", 0))) if pos_info.get("liquidationPx") else None,
                        "margin_used": Decimal(str(pos_info.get("marginUsed", 0)))
                    })
            
            await self.db.upsert_positions(positions_data)
            
            # === 3. Update Open Orders ===
            open_orders = self.info.open_orders(account.address)
            orders_data = []
            for order in open_orders:
                # Hyperliquid returns 'B' for buy, 'A' for sell
                raw_side = order.get("side", "").upper()
                db_side = "BUY" if raw_side == "B" else "SELL"
                orders_data.append({
                    "order_id": int(order.get("oid", 0)),
                    "symbol": order.get("coin"),
                    "side": db_side,  # DB expects BUY/SELL
                    "size": Decimal(str(order.get("sz", 0))),
                    "price": Decimal(str(order.get("limitPx", 0))),
                    "order_type": order.get("orderType", "limit"),
                    "reduce_only": order.get("reduceOnly", False),
                    "created_at": datetime.fromtimestamp(order.get("timestamp", 0) / 1000) if order.get("timestamp") else datetime.now()
                })
            
            await self.db.upsert_orders(orders_data)
            
            self.logger.debug(f"[DB] Synced: equity=${equity:.2f}, {len(positions_data)} positions, {len(orders_data)} orders")
            
        except Exception as e:
            self.logger.warning(f"[DB] Failed to sync to database: {e}")
    
    # =========================================================================
    # Order Execution
    # =========================================================================
    
    async def open_position(self, side: str):
        """Open a new position."""
        if self.position is not None:
            self.logger.warning(f"Already have a position, cannot open {side}")
            return
        
        # Check order interval
        if self.last_order_time:
            elapsed = (datetime.now() - self.last_order_time).total_seconds()
            if elapsed < self.config.min_order_interval_seconds:
                self.logger.debug(f"Order cooldown: {elapsed:.0f}s < {self.config.min_order_interval_seconds}s")
                return
        
        current_price = self.get_current_price()
        if not current_price:
            self.logger.warning("No price available, cannot open position")
            return
        
        # Calculate position size
        size = self.config.position_size_usd / current_price
        
        # Get symbol info for decimals
        try:
            symbol_info = self._get_symbol_info()
            sz_decimals = symbol_info.get("szDecimals", 4)
            size = round(size, sz_decimals)
        except Exception as e:
            self.logger.warning(f"Could not get symbol info: {e}, using default decimals")
            size = round(size, 4)
        
        # Calculate TP/SL prices
        if side == "long":
            tp_price = current_price * (1 + self.config.tp_pct)
            sl_price = current_price * (1 - self.config.sl_pct)
            is_buy = True
        else:
            tp_price = current_price * (1 - self.config.tp_pct)
            sl_price = current_price * (1 + self.config.sl_pct)
            is_buy = False
        
        self.logger.info(
            f"Opening {side.upper()} position: {size} {self.config.symbol} @ ~${current_price:.2f}"
        )
        self.logger.info(f"TP: ${tp_price:.2f} | SL: ${sl_price:.2f}")
        
        # Insert signal to database
        signal_id = None
        if self.db:
            try:
                signal_id = await self.db.insert_signal({
                    "symbol": self.config.symbol,
                    "strategy": self.config.strategy,
                    "side": "BUY" if side == "long" else "SELL",  # DB expects BUY/SELL
                    "signal_type": "ENTRY",
                    "confidence": None,
                    "reason": f"Strategy: {self.config.strategy}"
                })
                self.logger.debug(f"[DB] Signal inserted: {signal_id}")
            except Exception as e:
                self.logger.warning(f"[DB] Failed to insert signal: {e}")
        
        if self.dry_run:
            self.logger.info("[DRY RUN] Order would be placed")
            # Simulate position for testing
            self.position = Position(
                symbol=self.config.symbol,
                side=side,
                size=size,
                entry_price=current_price,
                entry_time=datetime.now(),
                tp_price=tp_price,
                sl_price=sl_price,
                trailing_stop_pct=self.config.trailing_stop_pct
            )
            self.last_order_time = datetime.now()
            
            # Create trade in database (dry run)
            if self.db:
                try:
                    self.current_trade_id = await self.db.create_trade({
                        "symbol": self.config.symbol,
                        "side": side.upper(),  # DB expects LONG/SHORT uppercase
                        "size": Decimal(str(size)),
                        "entry_price": Decimal(str(current_price)),
                        "entry_time": datetime.now(),
                        "entry_fill_ids": [],
                        "strategy": self.config.strategy
                    })
                    self.logger.debug(f"[DB] Trade created: {self.current_trade_id}")
                except Exception as e:
                    self.logger.warning(f"[DB] Failed to create trade: {e}")
            return
        
        try:
            # Place market order (positional args: coin, is_buy, size, px, slippage)
            result = self.exchange.market_open(
                self.config.symbol,  # coin
                is_buy,              # is_buy
                size,                # size
                None,                # px (None = market price)
                0.01                 # slippage (1%)
            )
            
            if result.get("status") == "ok":
                # Get fill price from response
                fill_statuses = result.get("response", {}).get("data", {}).get("statuses", [])
                fill_price = current_price  # Default
                
                for status in fill_statuses:
                    if "filled" in status:
                        fill_price = float(status["filled"]["avgPx"])
                        break
                
                # Recalculate TP/SL with actual fill price
                if side == "long":
                    tp_price = fill_price * (1 + self.config.tp_pct)
                    sl_price = fill_price * (1 - self.config.sl_pct)
                else:
                    tp_price = fill_price * (1 - self.config.tp_pct)
                    sl_price = fill_price * (1 + self.config.sl_pct)
                
                self.position = Position(
                    symbol=self.config.symbol,
                    side=side,
                    size=size,
                    entry_price=fill_price,
                    entry_time=datetime.now(),
                    tp_price=tp_price,
                    sl_price=sl_price,
                    trailing_stop_pct=self.config.trailing_stop_pct
                )
                
                self.last_order_time = datetime.now()
                self.logger.info(f"Position opened @ ${fill_price:.2f}")
                
                # Create trade in database
                if self.db:
                    try:
                        self.current_trade_id = await self.db.create_trade({
                            "symbol": self.config.symbol,
                            "side": side.upper(),  # DB expects LONG/SHORT uppercase
                            "size": Decimal(str(size)),
                            "entry_price": Decimal(str(fill_price)),
                            "entry_time": datetime.now(),
                            "entry_fill_ids": [],
                            "strategy": self.config.strategy
                        })
                        self.logger.debug(f"[DB] Trade created: {self.current_trade_id}")
                        
                        # Mark signal as executed
                        if signal_id:
                            await self.db.mark_signal_executed(
                                signal_id=signal_id,
                                order_id=0,  # We don't have order_id from market_open
                                execution_price=Decimal(str(fill_price))
                            )
                    except Exception as e:
                        self.logger.warning(f"[DB] Failed to create trade: {e}")
                
            else:
                error = result.get("response", {}).get("data", {}).get("statuses", [])
                self.logger.error(f"Order failed: {error}")
                
                # Mark signal as rejected
                if self.db and signal_id:
                    try:
                        await self.db.mark_signal_rejected(signal_id, f"Order failed: {error}")
                    except Exception as e:
                        self.logger.warning(f"[DB] Failed to mark signal rejected: {e}")
                
        except Exception as e:
            self.logger.error(f"Failed to open position: {e}")
    
    async def close_position(self, reason: str):
        """Close current position."""
        if self.position is None:
            return
        
        current_price = self.get_current_price()
        pnl = self.position.pnl(current_price) if current_price else 0
        
        self.logger.info(
            f"Closing {self.position.side.upper()} position ({reason}): "
            f"PnL: {pnl:+.2f}%"
        )
        
        if self.dry_run:
            self.logger.info("[DRY RUN] Position would be closed")
            # Update stats
            self.trades_count += 1
            self.total_pnl += pnl
            if pnl > 0:
                self.wins += 1
            else:
                self.losses += 1
            
            # Close trade in database
            if self.db and self.current_trade_id:
                try:
                    entry_time = self.position.entry_time
                    exit_time = datetime.now()
                    duration_seconds = int((exit_time - entry_time).total_seconds())
                    
                    # Calculate PnL in USD
                    if self.position.side == "long":
                        gross_pnl = Decimal(str((current_price - self.position.entry_price) * self.position.size))
                    else:
                        gross_pnl = Decimal(str((self.position.entry_price - current_price) * self.position.size))
                    
                    fees = Decimal("0")  # No fees in dry run
                    net_pnl = gross_pnl - fees
                    
                    await self.db.close_trade(
                        trade_id=self.current_trade_id,
                        exit_price=Decimal(str(current_price)),
                        exit_time=exit_time,
                        exit_fill_ids=[],
                        gross_pnl=gross_pnl,
                        fees=fees,
                        net_pnl=net_pnl,
                        duration_seconds=duration_seconds
                    )
                    self.logger.debug(f"[DB] Trade closed: {self.current_trade_id}")
                    self.current_trade_id = None
                except Exception as e:
                    self.logger.warning(f"[DB] Failed to close trade: {e}")
            
            self.position = None
            return
        
        try:
            # Close position with market order
            is_buy = self.position.side == "short"  # Opposite side to close
            
            result = self.exchange.market_close(
                coin=self.config.symbol,
                sz=self.position.size,
                slippage=0.01
            )
            
            if result.get("status") == "ok":
                # Get exit price from response
                fill_statuses = result.get("response", {}).get("data", {}).get("statuses", [])
                exit_price = current_price  # Default
                
                for status in fill_statuses:
                    if "filled" in status:
                        exit_price = float(status["filled"]["avgPx"])
                        break
                
                self.logger.info(f"Position closed successfully @ ${exit_price:.2f}")
                
                # Update stats
                self.trades_count += 1
                self.total_pnl += pnl
                if pnl > 0:
                    self.wins += 1
                else:
                    self.losses += 1
                
                # Close trade in database
                if self.db and self.current_trade_id:
                    try:
                        entry_time = self.position.entry_time
                        exit_time = datetime.now()
                        duration_seconds = int((exit_time - entry_time).total_seconds())
                        
                        # Calculate PnL in USD
                        if self.position.side == "long":
                            gross_pnl = Decimal(str((exit_price - self.position.entry_price) * self.position.size))
                        else:
                            gross_pnl = Decimal(str((self.position.entry_price - exit_price) * self.position.size))
                        
                        # Estimate fees (0.05% maker/taker average)
                        fees = Decimal(str(self.position.size * exit_price * 0.0005))
                        net_pnl = gross_pnl - fees
                        
                        await self.db.close_trade(
                            trade_id=self.current_trade_id,
                            exit_price=Decimal(str(exit_price)),
                            exit_time=exit_time,
                            exit_fill_ids=[],
                            gross_pnl=gross_pnl,
                            fees=fees,
                            net_pnl=net_pnl,
                            duration_seconds=duration_seconds
                        )
                        self.logger.debug(f"[DB] Trade closed: {self.current_trade_id}")
                        self.current_trade_id = None
                    except Exception as e:
                        self.logger.warning(f"[DB] Failed to close trade: {e}")
                    
                self.position = None
            else:
                error = result.get("response", {}).get("data", {}).get("statuses", [])
                self.logger.error(f"Close order failed: {error}")
                
        except Exception as e:
            self.logger.error(f"Failed to close position: {e}")
    
    # =========================================================================
    # Main Trading Logic
    # =========================================================================
    
    async def check_exits(self):
        """Check if current position should be closed."""
        if self.position is None:
            return
        
        current_price = self.get_current_price()
        if not current_price:
            return
        
        # Update trailing stop level each cycle
        self.position.update_trailing_stop(current_price)
        
        # Log trailing stop status periodically (when active and SL has moved)
        if self.position.trailing_stop_active:
            self.logger.debug(
                f"Trailing stop active - Best: ${self.position.best_price:.2f}, "
                f"SL: ${self.position.sl_price:.2f}"
            )
        
        # Check TP/SL/Trailing Stop
        close_reason = self.position.should_close(current_price)
        if close_reason:
            await self.close_position(close_reason)
    
    async def check_entries(self):
        """Check if we should open a new position."""
        # Don't enter if we have a position
        if self.position is not None:
            return
        
        # Don't enter if not enough price data
        if len(self.prices) < 50:
            self.logger.debug(f"Waiting for more price data: {len(self.prices)}/50")
            return
        
        # Evaluate strategy
        signal = self.strategy.evaluate(self.prices)
        
        if signal == Signal.LONG:
            await self.open_position("long")
        elif signal == Signal.SHORT:
            await self.open_position("short")
    
    async def run_cycle(self):
        """Run one trading cycle."""
        try:
            # 1. Fetch latest prices
            await self.fetch_prices()
            
            # 2. Sync position state
            await self.sync_position()
            
            # 3. Sync to database
            await self.sync_to_database()
            
            # 4. Check exits first
            await self.check_exits()
            
            # 5. Check for new entries
            await self.check_entries()
            
            # 6. Log status
            current_price = self.get_current_price()
            if current_price:
                status = f"Price: ${current_price:.2f}"
                if self.position:
                    pnl = self.position.pnl(current_price)
                    status += f" | Position: {self.position.side.upper()} @ ${self.position.entry_price:.2f} | PnL: {pnl:+.2f}%"
                else:
                    status += " | No position"
                self.logger.info(status)
                
        except Exception as e:
            self.logger.error(f"Error in trading cycle: {e}", exc_info=True)
    
    # =========================================================================
    # Start / Stop
    # =========================================================================
    
    async def start(self):
        """Start the bot."""
        self.logger.info("Starting bot...")
        
        # Initialize clients
        self._init_clients()
        
        # Connect to database (optional)
        if DB_AVAILABLE:
            try:
                self.db = Database()
                await self.db.connect(min_size=1, max_size=3)
                self.logger.info("[DB] Connected to PostgreSQL")
            except Exception as e:
                self.logger.warning(f"[DB] Failed to connect to database: {e}")
                self.logger.warning("[DB] Bot will continue without database persistence")
                self.db = None
        
        # Initial sync
        await self.sync_position()
        
        # Initial database sync
        await self.sync_to_database()
        
        self.running = True
        self.logger.info("Bot started. Press Ctrl+C to stop.")
        
        # Main loop
        cycle_interval = 5  # seconds
        while self.running:
            await self.run_cycle()
            await asyncio.sleep(cycle_interval)
    
    def stop(self):
        """Stop the bot gracefully."""
        self.logger.info("Stopping bot...")
        self.running = False
        
        # Print final stats
        self.logger.info("=" * 60)
        self.logger.info("Final Statistics")
        self.logger.info("=" * 60)
        self.logger.info(f"Total trades: {self.trades_count}")
        self.logger.info(f"Wins: {self.wins} | Losses: {self.losses}")
        if self.trades_count > 0:
            win_rate = self.wins / self.trades_count * 100
            self.logger.info(f"Win rate: {win_rate:.1f}%")
        self.logger.info(f"Total PnL: {self.total_pnl:+.2f}%")
        self.logger.info("=" * 60)
        
        # Disconnect from database (in a new event loop since we're called sync)
        if self.db:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(self.db.disconnect())
                loop.close()
                self.logger.info("[DB] Disconnected from PostgreSQL")
            except Exception as e:
                self.logger.warning(f"[DB] Failed to disconnect: {e}")


# =============================================================================
# Main Entry Point
# =============================================================================

def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Simple Hyperliquid Trading Bot")
    parser.add_argument(
        "--config", 
        default="simple_bot/config.yaml",
        help="Path to config file"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without placing real orders"
    )
    parser.add_argument(
        "--symbol",
        help="Override symbol from config"
    )
    parser.add_argument(
        "--strategy",
        choices=["momentum", "mean_reversion", "breakout"],
        help="Override strategy from config"
    )
    
    args = parser.parse_args()

    # Load config
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config file not found: {config_path}")
        sys.exit(1)
    
    config = BotConfig.from_yaml(str(config_path))
    
    # Apply command line overrides
    if args.symbol:
        config.symbol = args.symbol
    if args.strategy:
        config.strategy = args.strategy
    
    # Create and run bot
    bot = SimpleBot(config, dry_run=args.dry_run)
    
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        bot.stop()
    except Exception as e:
        logging.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
