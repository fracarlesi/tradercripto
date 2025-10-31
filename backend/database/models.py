"""SQLAlchemy async models for Bitcoin Trading System."""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""

    pass


class User(Base):
    """User model (scaffolding for future multi-user support).

    Current deployment: Single user only.
    Purpose: Database schema foundation for potential multi-tenant expansion.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    accounts: Mapped[list["Account"]] = relationship(
        "Account", back_populates="user", lazy="selectin"
    )
    auth_sessions: Mapped[list["UserAuthSession"]] = relationship(
        "UserAuthSession", back_populates="user", lazy="selectin"
    )

    __table_args__ = (
        Index("idx_users_username", "username"),
        Index("idx_users_email", "email"),
    )


class UserAuthSession(Base):
    """User authentication session for token-based auth.

    Tracks active user sessions with expiration.
    """

    __tablename__ = "user_auth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    session_token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)  # IPv6 max length
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="auth_sessions")

    __table_args__ = (
        Index("idx_user_auth_sessions_token", "session_token"),
        Index("idx_user_auth_sessions_user_id", "user_id"),
        Index("idx_user_auth_sessions_expires_at", "expires_at"),
    )


class Account(Base):
    """Trading account with DeepSeek AI model configuration.

    Synced fields from Hyperliquid:
    - current_cash: Available balance
    - frozen_cash: Margin used
    - initial_capital: Total equity

    Local only: AI model config, account metadata
    """

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    version: Mapped[str] = mapped_column(String(100), default="v1", nullable=False)

    # Account Identity
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    account_type: Mapped[str] = mapped_column(
        String(20), default="AI", nullable=False
    )  # "AI" or "MANUAL"
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # AI Model Configuration (for AI accounts)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    api_key: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Trading Account Balances (synced from Hyperliquid)
    initial_capital: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), default=Decimal("0"), nullable=False
    )
    current_cash: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), default=Decimal("0"), nullable=False
    )
    frozen_cash: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), default=Decimal("0"), nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="accounts", lazy="selectin")
    positions: Mapped[list["Position"]] = relationship(
        "Position", back_populates="account", lazy="selectin"
    )
    orders: Mapped[list["Order"]] = relationship("Order", back_populates="account", lazy="selectin")
    trades: Mapped[list["Trade"]] = relationship("Trade", back_populates="account", lazy="selectin")
    ai_decisions: Mapped[list["AIDecisionLog"]] = relationship(
        "AIDecisionLog", back_populates="account", lazy="selectin"
    )

    __table_args__ = (
        Index("idx_accounts_user_active", "user_id", "is_active"),
        Index("idx_accounts_type", "account_type"),
    )


class Position(Base):
    """Current open trading position.

    Synced from Hyperliquid: ENTIRE position list (cleared and recreated each sync)
    Sync strategy: DELETE all local positions for account → INSERT fresh from Hyperliquid assetPositions
    """

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)

    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    available_quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    average_cost: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    account: Mapped["Account"] = relationship(
        "Account", back_populates="positions", lazy="selectin"
    )

    __table_args__ = (
        Index("idx_positions_account", "account_id"),
        Index("idx_positions_account_symbol", "account_id", "symbol", unique=True),
    )


class Order(Base):
    """Trading order (historical and active).

    Synced from Hyperliquid: Historical fills converted to FILLED orders
    Note: Hyperliquid is source of truth - local orders are historical records
    Status values: PENDING, FILLED, CANCELLED, REJECTED
    """

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)

    order_no: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)  # "buy" or "sell"
    order_type: Mapped[str] = mapped_column(String(20), nullable=False)  # "market", "limit"
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    filled_quantity: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), default=Decimal("0"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), default="PENDING", nullable=False
    )  # PENDING, FILLED, CANCELLED, REJECTED

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    account: Mapped["Account"] = relationship("Account", back_populates="orders", lazy="selectin")
    trades: Mapped[list["Trade"]] = relationship("Trade", back_populates="order", lazy="selectin")

    __table_args__ = (
        Index("idx_orders_account", "account_id"),
        Index("idx_orders_status", "status"),
        Index("idx_orders_created", "created_at"),
        Index("idx_orders_order_no", "order_no", unique=True),
    )


class Trade(Base):
    """Executed trade (fill).

    Synced from Hyperliquid: Last 100 fills fetched each sync
    Deduplication: Use timestamp+symbol+side+quantity as unique identifier
    """

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True)

    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)  # "buy" or "sell"
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    commission: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), default=Decimal("0"), nullable=False
    )
    trade_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Relationships
    account: Mapped["Account"] = relationship("Account", back_populates="trades", lazy="selectin")
    order: Mapped[Optional["Order"]] = relationship(
        "Order", back_populates="trades", lazy="selectin"
    )

    __table_args__ = (
        Index("idx_trades_account", "account_id"),
        Index("idx_trades_order", "order_id"),
        Index("idx_trades_time", "trade_time"),
        Index("idx_trades_dedup", "trade_time", "symbol", "quantity", "price", unique=True),
    )


class AIDecisionLog(Base):
    """AI trading decision log (LOCAL ONLY).

    NOT synced from Hyperliquid - purely local audit trail
    Purpose: Track what AI decided, why, and whether order was successfully executed
    """

    __tablename__ = "ai_decision_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)

    decision_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)  # AI explanation
    operation: Mapped[str] = mapped_column(String(10), nullable=False)  # "buy", "sell", "hold"
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    prev_portion: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    target_portion: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    total_balance: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    executed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True)

    # Relationships
    account: Mapped["Account"] = relationship(
        "Account", back_populates="ai_decisions", lazy="selectin"
    )

    __table_args__ = (
        Index("idx_ai_logs_account", "account_id"),
        Index("idx_ai_logs_time", "decision_time"),
    )


class CryptoKline(Base):
    """OHLCV candlestick data cache (LOCAL ONLY).

    Purpose: Historical price data for chart display and technical analysis
    Source: Market data APIs (CCXT or similar), not Hyperliquid
    """

    __tablename__ = "crypto_klines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    period: Mapped[str] = mapped_column(String(10), nullable=False)  # "1m", "5m", "15m", "1h", "1d"
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False)

    __table_args__ = (
        UniqueConstraint("symbol", "period", "timestamp", name="uq_kline_symbol_period_time"),
        Index("idx_klines_unique", "symbol", "period", "timestamp", unique=True),
        Index("idx_klines_lookup", "symbol", "period"),
    )


class CryptoPrice(Base):
    """Daily price snapshot cache (LOCAL ONLY).

    Purpose: Simplified historical pricing for portfolio valuation
    """

    __tablename__ = "crypto_prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    price_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("symbol", "price_date", name="uq_price_symbol_date"),
        Index("idx_prices_unique", "symbol", "price_date", unique=True),
        Index("idx_prices_lookup", "symbol"),
    )
