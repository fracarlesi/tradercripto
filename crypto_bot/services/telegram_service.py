"""
HLQuantBot Telegram Notification Service
=========================================

Sends trading alerts via Telegram Bot API.

Subscribes to:
- ORDERS: Trade executions, rejections, errors
- FILLS: Fills, position closures, P&L
- RISK_ALERTS: Kill switch events (highest priority)

Features:
- Rate limiting to avoid Telegram API limits
- Message batching for fills
- Emoji-based severity indicators
- Async HTTP client (aiohttp)

Configuration (trading.yaml):
    monitoring:
      telegram:
        enabled: true
        token: ${TELEGRAM_BOT_TOKEN}
        chat_id: ${TELEGRAM_CHAT_ID}
        alert_on:
          - "trade_open"
          - "trade_close"
          - "kill_switch_trigger"
          - "error"

Environment Variables:
    TELEGRAM_BOT_TOKEN: Bot token from @BotFather
    TELEGRAM_CHAT_ID: Chat ID to send messages to

Author: HLQuantBot
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import aiohttp

from .base import BaseService
from .message_bus import Message, MessageBus, Topic

logger = logging.getLogger(__name__)


class TelegramService(BaseService):
    """
    Telegram notification service for HLQuantBot.

    Listens to message bus events and sends formatted alerts to Telegram.
    """

    # Emoji mappings for different event types
    EMOJI = {
        "trade_open_long": "🟢",
        "trade_open_short": "🔴",
        "trade_close_profit": "💰",
        "trade_close_loss": "📉",
        "kill_switch": "🚨",
        "warning": "⚠️",
        "error": "❌",
        "info": "ℹ️",
        "startup": "🚀",
        "shutdown": "🛑",
        "fill": "✅",
        "order_rejected": "🚫",
        "daily_summary": "📊",
    }

    # Rate limiting
    MAX_MESSAGES_PER_MINUTE = 20
    BATCH_DELAY_SECONDS = 3.0

    def __init__(
        self,
        bus: MessageBus,
        config: Dict[str, Any],
        **kwargs: Any,
    ) -> None:
        """
        Initialize Telegram service.

        Args:
            bus: MessageBus for pub/sub
            config: Full trading config dict
        """
        super().__init__(
            name="telegram",
            bus=bus,
            config=config,
            loop_interval_seconds=60.0,  # Check for batched messages
            **kwargs,
        )

        # Load Telegram config
        tg_config = config.get("monitoring", {}).get("telegram", {})
        self._enabled = tg_config.get("enabled", False)
        self._token = tg_config.get("token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = tg_config.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID", "")
        self._alert_on = set(tg_config.get("alert_on", [
            "trade_open", "trade_close", "kill_switch_trigger", "error"
        ]))

        # Rate limiting state
        self._message_timestamps: List[datetime] = []
        self._pending_messages: List[str] = []
        self._pending_lock = asyncio.Lock()

        # HTTP session
        self._session: Optional[aiohttp.ClientSession] = None

        # Stats
        self._messages_sent = 0
        self._messages_failed = 0

        # Validate config
        if self._enabled and (not self._token or not self._chat_id):
            self._logger.warning(
                "Telegram enabled but missing token or chat_id. "
                "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars."
            )
            self._enabled = False

    async def _on_start(self) -> None:
        """Initialize HTTP session and subscribe to topics."""
        if not self._enabled:
            self._logger.info("Telegram notifications disabled")
            return

        # Create aiohttp session
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )

        # Subscribe to relevant topics
        await self.subscribe(Topic.ORDERS, self._on_order_event)
        await self.subscribe(Topic.FILLS, self._on_fill_event)
        await self.subscribe(Topic.RISK_ALERTS, self._on_risk_alert)

        self._logger.info("Telegram service started, chat_id=%s", self._chat_id[-4:])

        # Send startup message
        await self._send_message(
            f"{self.EMOJI['startup']} *HLQuantBot Started*\n"
            f"Trading bot is now active.\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )

    async def _on_stop(self) -> None:
        """Cleanup HTTP session."""
        if self._enabled:
            # Send shutdown message
            await self._send_message(
                f"{self.EMOJI['shutdown']} *HLQuantBot Stopped*\n"
                f"Trading bot has been shut down.\n"
                f"Messages sent: {self._messages_sent}"
            )

        if self._session:
            await self._session.close()
            self._session = None

        self._logger.info(
            "Telegram service stopped. Sent: %d, Failed: %d",
            self._messages_sent,
            self._messages_failed
        )

    async def _run_iteration(self) -> None:
        """Flush any pending batched messages."""
        await self._flush_pending_messages()

    # =========================================================================
    # Event Handlers
    # =========================================================================

    async def _on_order_event(self, message: Message) -> None:
        """Handle order events (submitted, filled, rejected, error)."""
        if not self._enabled:
            return

        payload = message.payload
        event_type = payload.get("event", "")

        # Order submitted (trade open)
        if event_type == "order_submitted" and "trade_open" in self._alert_on:
            signal = payload.get("signal", {})
            order = payload.get("order", {})

            side = signal.get("direction", "unknown")
            emoji = self.EMOJI["trade_open_long"] if side == "long" else self.EMOJI["trade_open_short"]

            text = (
                f"{emoji} *Trade Opened*\n"
                f"Symbol: `{signal.get('symbol', 'N/A')}`\n"
                f"Side: {side.upper()}\n"
                f"Size: {order.get('size', 'N/A')}\n"
                f"Entry: ${order.get('price', 'N/A')}\n"
                f"Strategy: {signal.get('strategy', 'N/A')}\n"
                f"TP: ${signal.get('tp_price', 'N/A')} | SL: ${signal.get('sl_price', 'N/A')}"
            )
            await self._send_message(text)

        # Order error
        elif event_type == "order_error" and "error" in self._alert_on:
            error = payload.get("error", "Unknown error")
            signal = payload.get("signal", {})

            text = (
                f"{self.EMOJI['error']} *Order Error*\n"
                f"Symbol: `{signal.get('symbol', 'N/A')}`\n"
                f"Error: {error}"
            )
            await self._send_message(text, priority=True)

        # Order rejected
        elif event_type == "order_rejected" and "error" in self._alert_on:
            reason = payload.get("reason", "Unknown")
            symbol = payload.get("symbol", "N/A")

            text = (
                f"{self.EMOJI['order_rejected']} *Order Rejected*\n"
                f"Symbol: `{symbol}`\n"
                f"Reason: {reason}"
            )
            await self._send_message(text)

    async def _on_fill_event(self, message: Message) -> None:
        """Handle fill events (position closed, P&L)."""
        if not self._enabled:
            return

        payload = message.payload
        event_type = payload.get("event", "")

        # Position closed
        if event_type == "position_closed" and "trade_close" in self._alert_on:
            symbol = payload.get("symbol", "N/A")
            pnl = payload.get("realized_pnl", 0)
            pnl_pct = payload.get("pnl_pct", 0)
            side = payload.get("side", "unknown")
            entry = payload.get("entry_price", 0)
            exit_price = payload.get("exit_price", 0)

            # Choose emoji based on P&L
            if isinstance(pnl, (int, float, Decimal)):
                emoji = self.EMOJI["trade_close_profit"] if pnl >= 0 else self.EMOJI["trade_close_loss"]
                pnl_sign = "+" if pnl >= 0 else ""
            else:
                emoji = self.EMOJI["fill"]
                pnl_sign = ""

            text = (
                f"{emoji} *Position Closed*\n"
                f"Symbol: `{symbol}`\n"
                f"Side: {side.upper()}\n"
                f"Entry: ${entry:.2f} → Exit: ${exit_price:.2f}\n"
                f"P&L: {pnl_sign}${pnl:.2f} ({pnl_sign}{pnl_pct:.2f}%)"
            )
            await self._send_message(text)

        # Regular fill (batch these)
        elif event_type == "fill" and "fill" in self._alert_on:
            symbol = payload.get("symbol", "N/A")
            size = payload.get("size", 0)
            price = payload.get("price", 0)

            text = f"{self.EMOJI['fill']} Fill: {symbol} {size} @ ${price}"
            await self._queue_message(text)

    async def _on_risk_alert(self, message: Message) -> None:
        """Handle risk alerts (kill switch, scan errors)."""
        if not self._enabled:
            return

        payload = message.payload
        alert_type = payload.get("type") or payload.get("alert_type", "")

        if alert_type == "kill_switch" and "kill_switch_trigger" in self._alert_on:
            trigger = payload.get("trigger_type", "unknown")
            trigger_value = payload.get("trigger_value", 0)
            threshold = payload.get("threshold", 0)
            action = payload.get("action", "unknown")
            equity = payload.get("equity", 0)
            msg = payload.get("message", "")

            # High priority - always send immediately
            text = (
                f"{self.EMOJI['kill_switch']} *KILL SWITCH TRIGGERED*\n\n"
                f"Trigger: {trigger.upper()}\n"
                f"Value: {trigger_value:.2f}% (Threshold: {threshold:.2f}%)\n"
                f"Action: {action}\n"
                f"Equity: ${equity:,.2f}\n\n"
                f"_{msg}_"
            )
            await self._send_message(text, priority=True)

    # =========================================================================
    # Telegram API
    # =========================================================================

    async def _send_message(
        self,
        text: str,
        priority: bool = False,
        parse_mode: str = "Markdown",
    ) -> bool:
        """
        Send a message to Telegram.

        Args:
            text: Message text (supports Markdown)
            priority: If True, bypass rate limiting
            parse_mode: Telegram parse mode

        Returns:
            True if sent successfully
        """
        if not self._enabled or not self._session:
            return False

        # Rate limiting (skip for priority messages)
        if not priority:
            if not await self._check_rate_limit():
                await self._queue_message(text)
                return False

        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }

        try:
            async with self._session.post(url, json=payload) as resp:
                if resp.status == 200:
                    self._messages_sent += 1
                    self._message_timestamps.append(datetime.now(timezone.utc))
                    self._logger.debug("Telegram message sent")
                    return True
                else:
                    error_text = await resp.text()
                    self._logger.error(
                        "Telegram API error: %d - %s",
                        resp.status,
                        error_text
                    )
                    self._messages_failed += 1
                    return False

        except asyncio.TimeoutError:
            self._logger.error("Telegram request timed out")
            self._messages_failed += 1
            return False
        except Exception as e:
            self._logger.error("Telegram send error: %s", e)
            self._messages_failed += 1
            return False

    async def _check_rate_limit(self) -> bool:
        """Check if we're within rate limits."""
        now = datetime.now(timezone.utc)

        # Remove timestamps older than 1 minute
        cutoff = now.timestamp() - 60
        self._message_timestamps = [
            ts for ts in self._message_timestamps
            if ts.timestamp() > cutoff
        ]

        return len(self._message_timestamps) < self.MAX_MESSAGES_PER_MINUTE

    async def _queue_message(self, text: str) -> None:
        """Queue a message for batched sending."""
        async with self._pending_lock:
            self._pending_messages.append(text)

    async def _flush_pending_messages(self) -> None:
        """Send all pending batched messages."""
        async with self._pending_lock:
            if not self._pending_messages:
                return

            # Combine messages
            if len(self._pending_messages) == 1:
                combined = self._pending_messages[0]
            else:
                combined = (
                    f"{self.EMOJI['info']} *Batched Updates ({len(self._pending_messages)})*\n\n"
                    + "\n".join(self._pending_messages[-10:])  # Last 10 only
                )

            self._pending_messages.clear()

        await self._send_message(combined)

    # =========================================================================
    # Public Methods
    # =========================================================================

    async def send_daily_summary(
        self,
        trades: int,
        pnl: float,
        pnl_pct: float,
        equity: float,
        win_rate: float,
    ) -> None:
        """
        Send daily trading summary.

        Args:
            trades: Number of trades today
            pnl: Realized P&L in USD
            pnl_pct: P&L as percentage
            equity: Current equity
            win_rate: Win rate percentage
        """
        if not self._enabled or "daily_summary" not in self._alert_on:
            return

        pnl_sign = "+" if pnl >= 0 else ""
        pnl_emoji = "📈" if pnl >= 0 else "📉"

        text = (
            f"{self.EMOJI['daily_summary']} *Daily Summary*\n\n"
            f"Trades: {trades}\n"
            f"P&L: {pnl_sign}${pnl:.2f} ({pnl_sign}{pnl_pct:.2f}%) {pnl_emoji}\n"
            f"Win Rate: {win_rate:.1f}%\n"
            f"Equity: ${equity:,.2f}\n\n"
            f"_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}_"
        )
        await self._send_message(text)

    async def send_custom_alert(self, message: str, emoji: str = "info") -> None:
        """
        Send a custom alert message.

        Args:
            message: Alert text
            emoji: Emoji key from EMOJI dict
        """
        if not self._enabled:
            return

        e = self.EMOJI.get(emoji, self.EMOJI["info"])
        await self._send_message(f"{e} {message}")

    async def _health_check_impl(self) -> bool:
        """Check Telegram API connectivity."""
        if not self._enabled:
            return True

        if not self._session:
            return False

        # Simple check - verify bot token
        url = f"https://api.telegram.org/bot{self._token}/getMe"
        try:
            async with self._session.get(url) as resp:
                return resp.status == 200
        except Exception:
            return False

    @property
    def stats(self) -> Dict[str, Any]:
        """Get service statistics."""
        base_stats = super().stats
        base_stats.update({
            "enabled": self._enabled,
            "messages_sent": self._messages_sent,
            "messages_failed": self._messages_failed,
            "pending_messages": len(self._pending_messages),
        })
        return base_stats
