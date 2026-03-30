"""
Risk Manager Service
====================

Tick-based position sizing for futures.
contracts = floor(max_risk_usd / (risk_ticks * tick_value))
"""

import logging
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from ..config.loader import RiskConfig
from ..core.contracts import CONTRACTS, FuturesSpec
from ..core.enums import Direction
from ..core.models import ORBSetup, TradeIntent

logger = logging.getLogger(__name__)


class RiskManager:
    """Tick-based risk management for futures trading."""

    def __init__(self, config: RiskConfig) -> None:
        self._config = config
        self._daily_loss_usd = Decimal("0")
        self._daily_trade_count: int = 0
        self._consecutive_stops: int = 0

    def size_trade(self, setup: ORBSetup) -> Optional[TradeIntent]:
        """Calculate position size and validate risk limits.

        Args:
            setup: ORB setup to size

        Returns:
            TradeIntent if trade passes all risk checks, None otherwise
        """
        # Check daily trade limit
        if self._daily_trade_count >= self._config.max_trades_per_day:
            logger.info(
                "Trade rejected: daily limit reached (%d/%d)",
                self._daily_trade_count, self._config.max_trades_per_day,
            )
            return None

        # Check consecutive stops halt
        if self._consecutive_stops >= self._config.consecutive_stops_halt:
            logger.info(
                "Trade rejected: %d consecutive stops (halt at %d)",
                self._consecutive_stops, self._config.consecutive_stops_halt,
            )
            return None

        # Check daily loss limit
        if self._daily_loss_usd >= self._config.max_daily_loss_usd:
            logger.info(
                "Trade rejected: daily loss $%.2f >= limit $%.2f",
                float(self._daily_loss_usd), float(self._config.max_daily_loss_usd),
            )
            return None

        # Get contract spec
        spec = CONTRACTS.get(setup.symbol)
        if not spec:
            logger.error("Unknown contract: %s", setup.symbol)
            return None

        # Calculate position size: floor(max_risk / (risk_ticks * tick_value))
        risk_per_tick = spec.tick_value
        total_risk_per_contract = Decimal(str(setup.risk_ticks)) * risk_per_tick

        if total_risk_per_contract <= 0:
            logger.error("Invalid risk calculation for %s", setup.symbol)
            return None

        contracts = int(
            (self._config.max_risk_per_trade_usd / total_risk_per_contract)
            .to_integral_value(rounding=ROUND_DOWN)
        )

        # Apply max contracts cap
        contracts = min(contracts, self._config.max_contracts_per_trade)

        if contracts < 1:
            logger.info(
                "Trade rejected: risk too high for 1 contract. "
                "Risk/contract=$%.2f > max=$%.2f",
                float(total_risk_per_contract),
                float(self._config.max_risk_per_trade_usd),
            )
            return None

        risk_usd = Decimal(str(contracts)) * total_risk_per_contract

        logger.info(
            "Sized trade: %s %s x%d, risk=$%.2f (%.0f ticks * $%.2f * %d)",
            setup.direction.value, setup.symbol, contracts,
            float(risk_usd), setup.risk_ticks,
            float(risk_per_tick), contracts,
        )

        from datetime import datetime, timezone

        return TradeIntent(
            setup=setup,
            contracts=contracts,
            risk_usd=risk_usd,
            timestamp=datetime.now(timezone.utc),
        )

    def size_stock_trade(
        self,
        entry_price: Decimal,
        stop_price: Decimal,
        max_risk_usd: Optional[Decimal] = None,
        max_shares: int = 500,
    ) -> int:
        """Calculate position size in shares for a stock/ETF trade.

        Formula: shares = floor(max_risk_usd / abs(entry_price - stop_price))
        Minimum 1 share, capped at max_shares.

        Args:
            entry_price: Expected entry price
            stop_price: Stop loss price
            max_risk_usd: Risk budget for this trade (defaults to config max_risk_per_trade_usd)
            max_shares: Maximum shares cap (default 500)

        Returns:
            Number of shares (>= 1), or 0 if trade is invalid
        """
        risk_per_share = abs(entry_price - stop_price)
        if risk_per_share <= 0:
            logger.error(
                "Invalid stock risk: entry=%.2f stop=%.2f (zero risk per share)",
                float(entry_price), float(stop_price),
            )
            return 0

        budget = max_risk_usd if max_risk_usd is not None else self._config.max_risk_per_trade_usd

        shares = int(
            (budget / risk_per_share).to_integral_value(rounding=ROUND_DOWN)
        )

        # Apply cap
        shares = min(shares, max_shares)

        if shares < 1:
            logger.info(
                "Stock trade rejected: risk/share=$%.2f > budget=$%.2f",
                float(risk_per_share), float(budget),
            )
            return 0

        actual_risk = Decimal(str(shares)) * risk_per_share
        logger.info(
            "Sized stock trade: %d shares, risk=$%.2f ($%.2f/share * %d)",
            shares, float(actual_risk), float(risk_per_share), shares,
        )
        return shares

    def record_fill(self, pnl_usd: Decimal, is_stop: bool) -> None:
        """Record a completed trade outcome.

        Args:
            pnl_usd: P&L in USD (negative for loss)
            is_stop: Whether the exit was a stop loss
        """
        self._daily_trade_count += 1

        if pnl_usd < 0:
            self._daily_loss_usd += abs(pnl_usd)

        if is_stop:
            self._consecutive_stops += 1
            logger.info(
                "Stop recorded: %d consecutive stops", self._consecutive_stops
            )
        else:
            self._consecutive_stops = 0

        logger.info(
            "Trade recorded: P&L=$%.2f, daily_loss=$%.2f, trades=%d/%d",
            float(pnl_usd), float(self._daily_loss_usd),
            self._daily_trade_count, self._config.max_trades_per_day,
        )

    def reset_daily(self) -> None:
        """Reset daily counters (called at start of new trading day)."""
        self._daily_loss_usd = Decimal("0")
        self._daily_trade_count = 0
        self._consecutive_stops = 0
        logger.info("Daily risk counters reset")

    @property
    def is_trading_allowed(self) -> bool:
        """Check if trading is currently allowed."""
        if self._daily_trade_count >= self._config.max_trades_per_day:
            return False
        if self._consecutive_stops >= self._config.consecutive_stops_halt:
            return False
        if self._daily_loss_usd >= self._config.max_daily_loss_usd:
            return False
        return True

    @property
    def stats(self) -> dict:
        return {
            "daily_trade_count": self._daily_trade_count,
            "max_trades_per_day": self._config.max_trades_per_day,
            "daily_loss_usd": float(self._daily_loss_usd),
            "max_daily_loss_usd": float(self._config.max_daily_loss_usd),
            "consecutive_stops": self._consecutive_stops,
            "halt_at": self._config.consecutive_stops_halt,
            "trading_allowed": self.is_trading_allowed,
        }
