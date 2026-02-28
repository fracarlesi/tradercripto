"""
Notification Service
====================

Push notifications via ntfy.sh.
Adapted from crypto_bot pattern.
"""

import logging
import os
from typing import Optional

import httpx

from ..config.loader import NotificationsConfig

logger = logging.getLogger(__name__)


class NotificationService:
    """Send push notifications via ntfy.sh."""

    def __init__(self, config: NotificationsConfig) -> None:
        self._enabled = config.enabled
        self._topic = config.ntfy_topic_resolved
        self._base_url = "https://ntfy.sh"

        if not self._topic:
            self._enabled = False
            logger.warning("Notifications disabled: no ntfy topic configured")

    async def send(
        self,
        message: str,
        title: str = "IB Bot",
        priority: str = "default",
        tags: Optional[str] = None,
    ) -> bool:
        """Send a notification.

        Args:
            message: Notification body
            title: Notification title
            priority: Priority level (min, low, default, high, urgent)
            tags: Comma-separated emoji tags

        Returns:
            True if sent successfully
        """
        if not self._enabled:
            return False

        try:
            headers: dict[str, str] = {
                "Title": title,
                "Priority": priority,
            }
            if tags:
                headers["Tags"] = tags

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{self._base_url}/{self._topic}",
                    content=message,
                    headers=headers,
                )
                response.raise_for_status()

            logger.debug("Notification sent: %s", title)
            return True

        except Exception as e:
            logger.error("Failed to send notification: %s", e)
            return False

    async def notify_trade(
        self,
        action: str,
        symbol: str,
        direction: str,
        contracts: int,
        price: float,
        pnl: Optional[float] = None,
    ) -> bool:
        """Send a trade notification."""
        if pnl is not None:
            emoji = "chart_with_upwards_trend" if pnl >= 0 else "chart_with_downwards_trend"
            msg = f"{action}: {direction} {symbol} x{contracts} @ {price:.2f} | P&L: ${pnl:.2f}"
        else:
            emoji = "rocket" if direction == "long" else "bear"
            msg = f"{action}: {direction} {symbol} x{contracts} @ {price:.2f}"

        return await self.send(msg, title=f"IB {action}", tags=emoji)

    async def notify_kill_switch(self, reason: str) -> bool:
        """Send kill switch alert."""
        return await self.send(
            f"KILL SWITCH HALTED: {reason}",
            title="IB Bot ALERT",
            priority="urgent",
            tags="warning,octagonal_sign",
        )

    async def notify_session(self, event: str) -> bool:
        """Send session lifecycle notification."""
        return await self.send(event, title="IB Session", tags="clock3")
