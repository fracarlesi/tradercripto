"""Telegram alerting for HLQuantBot."""

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import aiohttp

from ..core.enums import AlertSeverity
from ..core.models import AccountState, ClosedTrade, Position
from ..config.settings import Settings


logger = logging.getLogger(__name__)


class TelegramAlerter:
    """
    Sends alerts to Telegram.

    Handles:
    - Trade notifications
    - Error alerts
    - Daily summaries
    - Circuit breaker alerts
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.config = settings.telegram
        self._session: Optional[aiohttp.ClientSession] = None
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._rate_limit_delay = 0.5  # Seconds between messages

    @property
    def is_enabled(self) -> bool:
        return self.config.enabled and bool(self.config.bot_token) and bool(self.config.chat_id)

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        """Close the session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a message to Telegram."""
        if not self.is_enabled:
            return False

        try:
            session = await self._get_session()
            url = f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage"

            payload = {
                "chat_id": self.config.chat_id,
                "text": text,
                "parse_mode": parse_mode,
            }

            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    return True
                else:
                    error = await response.text()
                    logger.error(f"Telegram API error: {error}")
                    return False

        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False

    async def send_alert(self, message: str, severity: AlertSeverity):
        """Send an alert with severity indicator."""
        severity_icons = {
            AlertSeverity.INFO: "ℹ️",
            AlertSeverity.WARNING: "⚠️",
            AlertSeverity.CRITICAL: "🚨",
            AlertSeverity.EMERGENCY: "🆘",
        }

        icon = severity_icons.get(severity, "📢")
        env = "TESTNET" if self.settings.is_testnet else "PROD"
        formatted = f"{icon} <b>[{env}] {severity.value.upper()}</b>\n\n{message}"

        await self.send_message(formatted)

    async def send_trade_alert(self, trade: ClosedTrade):
        """Send trade notification."""
        if not self.config.alert_on_trade:
            return

        # Determine emoji based on P&L
        if trade.pnl > 0:
            emoji = "✅"
            pnl_str = f"+${trade.pnl:.2f}"
        else:
            emoji = "❌"
            pnl_str = f"-${abs(trade.pnl):.2f}"

        message = (
            f"{emoji} <b>Trade Closed</b>\n\n"
            f"Strategy: <code>{trade.strategy_id.value}</code>\n"
            f"Symbol: <b>{trade.symbol}</b>\n"
            f"Side: {trade.side.value.upper()}\n"
            f"Size: {trade.size}\n"
            f"Entry: ${trade.entry_price:.2f}\n"
            f"Exit: ${trade.exit_price:.2f}\n"
            f"P&L: <b>{pnl_str}</b> ({trade.pnl_pct:+.2%})\n"
            f"Duration: {trade.duration_seconds // 60}min\n"
            f"Reason: {trade.exit_reason.value}"
        )

        await self.send_message(message)

    async def send_position_alert(self, position: Position, action: str = "opened"):
        """Send position notification."""
        if not self.config.alert_on_trade:
            return

        emoji = "📈" if position.side.value == "long" else "📉"

        message = (
            f"{emoji} <b>Position {action.title()}</b>\n\n"
            f"Symbol: <b>{position.symbol}</b>\n"
            f"Side: {position.side.value.upper()}\n"
            f"Size: {position.size}\n"
            f"Entry: ${position.entry_price:.2f}\n"
            f"Leverage: {position.leverage}x"
        )

        if position.stop_loss_price:
            message += f"\nStop Loss: ${position.stop_loss_price:.2f}"
        if position.take_profit_price:
            message += f"\nTake Profit: ${position.take_profit_price:.2f}"

        await self.send_message(message)

    async def send_daily_summary(self, account: AccountState, daily_trades: int, daily_pnl: Decimal):
        """Send daily summary."""
        if not self.config.alert_on_daily_summary:
            return

        # Determine emoji based on daily P&L
        if daily_pnl >= 0:
            emoji = "🟢"
            pnl_str = f"+${daily_pnl:.2f}"
        else:
            emoji = "🔴"
            pnl_str = f"-${abs(daily_pnl):.2f}"

        env = "TESTNET" if self.settings.is_testnet else "PRODUCTION"

        message = (
            f"📊 <b>Daily Summary</b> [{env}]\n\n"
            f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n\n"
            f"<b>Account</b>\n"
            f"Equity: ${account.equity:.2f}\n"
            f"Positions: {account.position_count}\n"
            f"Leverage: {account.current_leverage:.2f}x\n\n"
            f"<b>Today's Performance</b>\n"
            f"Trades: {daily_trades}\n"
            f"P&L: {emoji} <b>{pnl_str}</b> ({account.daily_pnl_pct:+.2%})"
        )

        await self.send_message(message)

    async def send_error_alert(self, error: str, context: Optional[str] = None):
        """Send error notification."""
        if not self.config.alert_on_error:
            return

        message = f"<b>Error</b>\n\n<code>{error}</code>"
        if context:
            message += f"\n\nContext: {context}"

        await self.send_alert(message, AlertSeverity.WARNING)

    async def send_circuit_breaker_alert(self, reason: str, account: AccountState):
        """Send circuit breaker alert."""
        if not self.config.alert_on_circuit_breaker:
            return

        message = (
            f"<b>TRADING HALTED</b>\n\n"
            f"Reason: {reason}\n\n"
            f"<b>Account Status</b>\n"
            f"Equity: ${account.equity:.2f}\n"
            f"Daily P&L: {account.daily_pnl_pct:+.2%}\n"
            f"Positions: {account.position_count}\n\n"
            f"<i>Manual restart required.</i>"
        )

        await self.send_alert(message, AlertSeverity.EMERGENCY)

    async def send_startup_message(self):
        """Send startup notification."""
        env = "TESTNET" if self.settings.is_testnet else "PRODUCTION"
        symbols = ", ".join(self.settings.active_symbols)

        message = (
            f"🚀 <b>HLQuantBot Started</b>\n\n"
            f"Environment: <code>{env}</code>\n"
            f"Symbols: {symbols}\n"
            f"Strategies:\n"
            f"  - Funding Bias: {'✅' if self.settings.strategies.funding_bias.enabled else '❌'}\n"
            f"  - Liquidation Cluster: {'✅' if self.settings.strategies.liquidation_cluster.enabled else '❌'}\n"
            f"  - Volatility Expansion: {'✅' if self.settings.strategies.volatility_expansion.enabled else '❌'}"
        )

        await self.send_message(message)

    async def send_shutdown_message(self, reason: str = "Normal shutdown"):
        """Send shutdown notification."""
        message = f"🛑 <b>HLQuantBot Stopped</b>\n\nReason: {reason}"
        await self.send_message(message)
