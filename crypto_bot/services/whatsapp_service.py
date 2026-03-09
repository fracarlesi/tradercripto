"""
HLQuantBot Push Notification Service (ntfy.sh)
===============================================

Sends trading alerts as push notifications via ntfy.sh.

ntfy.sh is a free, open-source push notification service:
    - No account required
    - Simple HTTP POST: POST https://ntfy.sh/TOPIC -d "message"
    - Install ntfy app on phone, subscribe to your topic
    - Works instantly

Setup:
    1. Install ntfy app (iOS/Android) or use https://ntfy.sh in browser
    2. Subscribe to your chosen topic (e.g. "hlquantbot-xyz123")
    3. Set NTFY_TOPIC in .env (use a unique, hard-to-guess name)

Subscribes to:
- ORDERS: Trade executions, rejections, errors
- FILLS: Fills, position closures, P&L
- RISK_ALERTS: Kill switch events (highest priority)

Configuration (trading.yaml):
    monitoring:
      whatsapp:
        enabled: true
        topic: ${NTFY_TOPIC}
        server: https://ntfy.sh
        alert_on:
          - "trade_open"
          - "trade_close"
          - "kill_switch_trigger"
          - "error"

Environment Variables:
    NTFY_TOPIC: Your unique ntfy topic name (e.g. hlquantbot-abc123)
    NTFY_SERVER: (optional) Self-hosted server URL, defaults to https://ntfy.sh

Author: HLQuantBot
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

from .base import BaseService
from .message_bus import Message, MessageBus, Topic

logger = logging.getLogger(__name__)

# Persistent dedup store
_DEDUP_DIR = Path(os.environ.get("HLQUANTBOT_DATA_DIR", str(Path.home() / ".hlquantbot")))
_DEDUP_FILE = _DEDUP_DIR / "ntfy_dedup.json"
_DEDUP_TTL_SECONDS = 3600  # 1 hour

# Default ntfy server
DEFAULT_NTFY_SERVER = "https://ntfy.sh"


def _resolve_env(value: str, env_key: str) -> str:
    """Resolve a config value, falling back to env var.

    Handles YAML templates like ${VAR:} that aren't resolved by yaml.safe_load.
    """
    if not value or value.startswith("${"):
        return os.environ.get(env_key, "")
    return value


class WhatsAppService(BaseService):
    """
    Push notification service for HLQuantBot via ntfy.sh.

    Named WhatsAppService for backward compatibility with main.py registration.
    Listens to message bus events and sends push notifications.
    """

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
    }

    MAX_MESSAGES_PER_MINUTE = 20
    BATCH_DELAY_SECONDS = 3.0

    def __init__(
        self,
        bus: MessageBus,
        config: Dict[str, Any],
        **kwargs: Any,
    ) -> None:
        super().__init__(
            name="whatsapp",
            bus=bus,
            config=config,
            loop_interval_seconds=60.0,
            **kwargs,
        )

        # Load config from monitoring.whatsapp section
        wa_config = config.get("monitoring", {}).get("whatsapp", {})
        self._enabled = wa_config.get("enabled", False)
        self._topic = _resolve_env(wa_config.get("topic", ""), "NTFY_TOPIC")
        self._server = (
            _resolve_env(wa_config.get("server", ""), "NTFY_SERVER")
            or DEFAULT_NTFY_SERVER
        )
        self._alert_on = set(wa_config.get("alert_on", [
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

        # Persistent dedup: survives restarts to prevent duplicates during deploy overlap
        self._dedup_keys: Dict[str, float] = {}  # key -> expiry_timestamp
        self._load_dedup()

        # Validate config
        if self._enabled and not self._topic:
            self._logger.warning(
                "Push notifications enabled but missing topic. "
                "Set NTFY_TOPIC env var."
            )
            self._enabled = False

    async def _on_start(self) -> None:
        """Initialize HTTP session and subscribe to topics."""
        if not self._enabled:
            self._logger.info("Push notifications disabled")
            return

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )

        # Subscribe to relevant topics
        await self.subscribe(Topic.ORDERS, self._on_order_event)
        await self.subscribe(Topic.FILLS, self._on_fill_event)
        await self.subscribe(Topic.RISK_ALERTS, self._on_risk_alert)

        self._logger.info(
            "Push notification service started, topic=%s, server=%s",
            self._topic,
            self._server,
        )

        # Startup/shutdown notifications disabled to reduce noise.
        # Bot status is visible in Account Snapshot reports.

    async def _on_stop(self) -> None:
        """Cleanup HTTP session."""

        if self._session:
            await self._session.close()
            self._session = None

        self._logger.info(
            "Push notification service stopped. Sent: %d, Failed: %d",
            self._messages_sent,
            self._messages_failed,
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

        if event_type == "order_submitted" and "trade_open" in self._alert_on:
            signal = payload.get("signal", {})
            order = payload.get("order", {})

            side = signal.get("direction", "unknown")
            emoji = self.EMOJI["trade_open_long"] if side == "long" else self.EMOJI["trade_open_short"]
            sym = signal.get("symbol", "N/A")

            text = (
                f"{emoji} {sym} {side.upper()}\n"
                f"Entry: ${order.get('price', 'N/A')}\n"
                f"TP: ${signal.get('tp_price', 'N/A')} | SL: ${signal.get('sl_price', 'N/A')}\n"
                f"{signal.get('strategy', '')}"
            )
            await self._send_message(text, title=f"{emoji} {side.upper()} {sym}")

        elif event_type == "order_error" and "error" in self._alert_on:
            error = payload.get("error", "Unknown error")
            signal = payload.get("signal", {})

            text = (
                f"{self.EMOJI['error']} Order Error\n"
                f"Symbol: {signal.get('symbol', 'N/A')}\n"
                f"Error: {error}"
            )
            await self._send_message(text, title="Order Error", priority=True)

        elif event_type == "order_rejected" and "error" in self._alert_on:
            reason = payload.get("reason", "Unknown")
            symbol = payload.get("symbol", "N/A")

            text = (
                f"{self.EMOJI['order_rejected']} Order Rejected\n"
                f"Symbol: {symbol}\n"
                f"Reason: {reason}"
            )
            await self._send_message(text, title=f"Order Rejected: {symbol}")

    async def _on_fill_event(self, message: Message) -> None:
        """Handle fill events (position closed, P&L)."""
        if not self._enabled:
            return

        payload = message.payload
        event_type = payload.get("event", "")

        if event_type == "position_closed" and "trade_close" in self._alert_on:
            symbol = payload.get("symbol", "N/A")
            pnl = payload.get("realized_pnl", 0)
            pnl_pct = payload.get("pnl_pct", 0)
            side = payload.get("side", "unknown")
            entry = payload.get("entry_price", 0)
            exit_price = payload.get("exit_price", 0)

            # Dedup: stable key from trade identity (survives restarts)
            pnl_f = float(pnl) if isinstance(pnl, Decimal) else pnl
            dedup_key = f"close_{symbol}_{side}_{entry}_{pnl_f:.2f}"
            if self._is_duplicate(dedup_key):
                self._logger.debug("Skipping duplicate close notification for %s", symbol)
                return

            pnl_f = float(pnl) if isinstance(pnl, Decimal) else pnl
            emoji = self.EMOJI["trade_close_profit"] if pnl_f >= 0 else self.EMOJI["trade_close_loss"]
            pnl_sign = "+" if pnl_f >= 0 else ""

            # Daily stats
            daily_wins = payload.get("daily_wins", 0) or 0
            daily_trades = payload.get("daily_trades", 0) or 0
            daily_losses = daily_trades - daily_wins
            daily_pnl = payload.get("daily_pnl", 0) or 0
            dp_sign = "+" if daily_pnl >= 0 else ""

            equity = payload.get("equity", 0) or 0

            text = (
                f"{emoji} {symbol} {side.upper()} closed\n"
                f"${entry:.2f} -> ${exit_price:.2f}\n"
                f"P&L: {pnl_sign}${pnl_f:.2f} ({pnl_sign}{float(pnl_pct):.2f}%)\n"
                f"---\n"
                f"Today: {daily_wins}W/{daily_losses}L | {dp_sign}${daily_pnl:.2f}\n"
                f"Balance: ${equity:.2f}"
            )
            await self._send_message(text, title=f"{emoji} {symbol} {pnl_sign}${pnl_f:.2f}")

    async def _on_risk_alert(self, message: Message) -> None:
        """Handle risk alerts (kill switch, scan errors, LLM failures)."""
        if not self._enabled:
            return

        payload = message.payload
        alert_type = payload.get("type") or payload.get("alert_type", "")

        # --- Kill switch events (from KillSwitchService) ---
        if alert_type == "kill_switch" and "kill_switch_trigger" in self._alert_on:
            trigger = payload.get("trigger_type", "unknown")
            trigger_value = payload.get("trigger_value", 0)
            threshold = payload.get("threshold", 0)
            action = payload.get("action", "unknown")
            equity = payload.get("equity", 0)
            msg = payload.get("message", "")

            text = (
                f"{self.EMOJI['kill_switch']} KILL SWITCH TRIGGERED\n\n"
                f"Trigger: {trigger.upper()}\n"
                f"Value: {trigger_value:.2f}% (Threshold: {threshold:.2f}%)\n"
                f"Action: {action}\n"
                f"Equity: ${equity:,.2f}\n\n"
                f"{msg}"
            )
            await self._send_message(
                text,
                title="KILL SWITCH TRIGGERED",
                priority=True,
                tags="warning,skull",
            )

        # --- Consecutive scan errors ---
        elif alert_type == "scan_errors":
            n_errors = payload.get("consecutive_errors", 0)
            msg = payload.get("message", "Unknown scan error")

            text = (
                f"{self.EMOJI['error']} SCAN ERRORS\n\n"
                f"Consecutive failures: {n_errors}\n"
                f"{msg}\n\n"
                f"Bot is retrying every 60s."
            )
            await self._send_message(
                text,
                title=f"Scan Error (x{n_errors})",
                priority=True,
                tags="warning",
            )

        # --- LLM failure alert ---
        elif alert_type == "llm_failure":
            reason = payload.get("failure_reason", "unknown")
            msg = payload.get("message", "LLM non-functional")
            calls = payload.get("calls_today", 0)
            max_calls = payload.get("max_calls", 0)

            text = (
                f"{self.EMOJI['warning']} LLM VETO NON-FUNCTIONAL\n\n"
                f"Reason: {reason}\n"
                f"Calls today: {calls}/{max_calls}\n\n"
                f"{msg}\n\n"
                f"Action: all new trades are being DENIED until LLM is restored."
            )
            await self._send_message(
                text,
                title="LLM Veto Down",
                priority=True,
                tags="warning,robot",
            )

        # --- Kill switch resume ---
        elif alert_type == "kill_switch_resume":
            prev = payload.get("previous_status", "unknown")
            text = (
                f"{self.EMOJI['startup']} KILL SWITCH RESUMED\n\n"
                f"Previous status: {prev}\n"
                f"Trading is now active."
            )
            await self._send_message(text, title="Kill Switch Resumed")

    # =========================================================================
    # Persistent Dedup
    # =========================================================================

    def _load_dedup(self) -> None:
        """Load dedup keys from disk (survives restarts)."""
        try:
            if _DEDUP_FILE.exists():
                with open(_DEDUP_FILE, "r") as f:
                    data = json.load(f)
                now = datetime.now(timezone.utc).timestamp()
                # Only keep non-expired keys
                self._dedup_keys = {
                    k: v for k, v in data.items() if v > now
                }
        except Exception:
            self._dedup_keys = {}

    def _save_dedup(self) -> None:
        """Persist dedup keys to disk."""
        try:
            _DEDUP_DIR.mkdir(parents=True, exist_ok=True)
            with open(_DEDUP_FILE, "w") as f:
                json.dump(self._dedup_keys, f)
        except Exception as e:
            self._logger.debug("Failed to save dedup state: %s", e)

    def _is_duplicate(self, key: str) -> bool:
        """Check if key was already sent recently. If not, mark it as sent."""
        now = datetime.now(timezone.utc).timestamp()
        expiry = self._dedup_keys.get(key)
        if expiry and expiry > now:
            return True
        self._dedup_keys[key] = now + _DEDUP_TTL_SECONDS
        self._save_dedup()
        return False

    # =========================================================================
    # ntfy.sh API
    # =========================================================================

    async def _send_message(
        self,
        text: str,
        title: str = "HLQuantBot",
        priority: bool = False,
        tags: str = "",
    ) -> bool:
        """
        Send a push notification via ntfy.sh.

        Args:
            text: Message body
            title: Notification title
            priority: If True, bypass rate limiting and use urgent priority
            tags: Comma-separated ntfy tags (emoji shortcodes)

        Returns:
            True if sent successfully
        """
        if not self._enabled or not self._session:
            return False

        if not priority:
            if not await self._check_rate_limit():
                await self._queue_message(text)
                return False

        url = f"{self._server}/{self._topic}"

        headers: Dict[str, str] = {
            "Title": title,
            "Priority": "urgent" if priority else "default",
        }
        if tags:
            headers["Tags"] = tags

        try:
            async with self._session.post(url, data=text.encode("utf-8"), headers=headers) as resp:
                if resp.status == 200:
                    self._messages_sent += 1
                    self._message_timestamps.append(datetime.now(timezone.utc))
                    self._logger.debug("Push notification sent")
                    return True
                else:
                    error_text = await resp.text()
                    self._logger.error(
                        "ntfy API error: %d - %s",
                        resp.status,
                        error_text,
                    )
                    self._messages_failed += 1
                    return False

        except asyncio.TimeoutError:
            self._logger.error("Push notification request timed out")
            self._messages_failed += 1
            return False
        except Exception as e:
            self._logger.error("Push notification send error: %s", e)
            self._messages_failed += 1
            return False

    async def _check_rate_limit(self) -> bool:
        """Check if we're within rate limits."""
        now = datetime.now(timezone.utc)

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

            if len(self._pending_messages) == 1:
                combined = self._pending_messages[0]
            else:
                combined = (
                    f"{self.EMOJI['info']} Batched Updates ({len(self._pending_messages)})\n\n"
                    + "\n".join(self._pending_messages[-10:])
                )

            self._pending_messages.clear()

        await self._send_message(combined, title="Batched Updates")

    # =========================================================================
    # Public Methods
    # =========================================================================

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
        """Check service health."""
        if not self._enabled:
            return True
        return self._session is not None and not self._session.closed

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
