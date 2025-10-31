"""Alerting service for sending notifications on critical events.

Supports multiple channels: email, webhook (Slack, Discord, etc.)
"""

import smtplib
from datetime import UTC, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import Enum
from typing import Any

import httpx
from config.logging import get_logger
from config.settings import settings

logger = get_logger(__name__)


class AlertLevel(Enum):
    """Alert severity levels."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AlertChannel(Enum):
    """Alert delivery channels."""

    EMAIL = "email"
    WEBHOOK = "webhook"
    SLACK = "slack"
    DISCORD = "discord"


class AlertingService:
    """Service for sending alerts via multiple channels (T133)."""

    def __init__(self) -> None:
        """Initialize alerting service with configuration."""
        self.enabled = settings.alert_enabled
        self.email_recipients = settings.alert_email_recipients
        self.webhook_url = settings.alert_webhook_url
        self.smtp_host = getattr(settings, "smtp_host", "localhost")
        self.smtp_port = getattr(settings, "smtp_port", 587)
        self.smtp_user = getattr(settings, "smtp_user", "")
        self.smtp_password = getattr(settings, "smtp_password", "")
        self.smtp_from = getattr(settings, "smtp_from", "trader@example.com")

        logger.info(
            f"AlertingService initialized (enabled: {self.enabled}, "
            f"email: {bool(self.email_recipients)}, webhook: {bool(self.webhook_url)})"
        )

    async def send_alert(
        self,
        level: AlertLevel,
        title: str,
        message: str,
        channels: list[AlertChannel] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Send alert to specified channels.

        Args:
            level: Alert severity level
            title: Alert title/subject
            message: Detailed alert message
            channels: List of channels to send to (default: all configured)
            metadata: Additional context data

        Returns:
            True if alert sent successfully to at least one channel
        """
        if not self.enabled:
            logger.debug(f"Alerting disabled, skipping alert: {title}")
            return False

        if channels is None:
            channels = self._get_configured_channels()

        if not channels:
            logger.warning("No alert channels configured")
            return False

        timestamp = datetime.now(UTC).isoformat()
        alert_data = {
            "level": level.value,
            "title": title,
            "message": message,
            "timestamp": timestamp,
            "metadata": metadata or {},
        }

        success = False

        for channel in channels:
            try:
                if channel == AlertChannel.EMAIL:
                    await self._send_email_alert(alert_data)
                    success = True
                elif channel == AlertChannel.WEBHOOK:
                    await self._send_webhook_alert(alert_data)
                    success = True
                elif channel == AlertChannel.SLACK:
                    await self._send_slack_alert(alert_data)
                    success = True
                elif channel == AlertChannel.DISCORD:
                    await self._send_discord_alert(alert_data)
                    success = True

                logger.info(
                    f"Alert sent via {channel.value}",
                    extra={"context": {"level": level.value, "title": title}},
                )

            except Exception as e:
                logger.error(
                    f"Failed to send alert via {channel.value}: {e}",
                    extra={
                        "context": {
                            "level": level.value,
                            "title": title,
                            "error": str(e),
                        }
                    },
                )

        return success

    def _get_configured_channels(self) -> list[AlertChannel]:
        """Get list of configured alert channels.

        Returns:
            List of available channels based on configuration
        """
        channels = []

        if self.email_recipients:
            channels.append(AlertChannel.EMAIL)

        if self.webhook_url:
            # Detect webhook type from URL
            if "slack.com" in self.webhook_url:
                channels.append(AlertChannel.SLACK)
            elif "discord.com" in self.webhook_url:
                channels.append(AlertChannel.DISCORD)
            else:
                channels.append(AlertChannel.WEBHOOK)

        return channels

    async def _send_email_alert(self, alert_data: dict[str, Any]) -> None:
        """Send alert via email.

        Args:
            alert_data: Alert information dictionary
        """
        if not self.email_recipients:
            return

        level = alert_data["level"]
        title = alert_data["title"]
        message = alert_data["message"]
        timestamp = alert_data["timestamp"]
        metadata = alert_data["metadata"]

        # Create email
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[{level.upper()}] {title}"
        msg["From"] = self.smtp_from
        msg["To"] = ", ".join(self.email_recipients)

        # Plain text version
        text_body = f"""
Trading System Alert

Level: {level.upper()}
Title: {title}
Time: {timestamp}

Message:
{message}

Metadata:
{self._format_metadata(metadata)}
"""

        # HTML version
        html_body = f"""
<html>
<head>
    <style>
        body {{ font-family: Arial, sans-serif; }}
        .alert {{ padding: 20px; border-radius: 5px; margin: 10px 0; }}
        .info {{ background-color: #d1ecf1; border-left: 4px solid #0c5460; }}
        .warning {{ background-color: #fff3cd; border-left: 4px solid #856404; }}
        .error {{ background-color: #f8d7da; border-left: 4px solid #721c24; }}
        .critical {{ background-color: #dc3545; color: white; border-left: 4px solid #721c24; }}
        .metadata {{ background-color: #f8f9fa; padding: 10px; margin-top: 10px; }}
    </style>
</head>
<body>
    <div class="alert {level}">
        <h2>{title}</h2>
        <p><strong>Level:</strong> {level.upper()}</p>
        <p><strong>Time:</strong> {timestamp}</p>
        <p><strong>Message:</strong></p>
        <p>{message}</p>
        <div class="metadata">
            <strong>Metadata:</strong>
            <pre>{self._format_metadata(metadata)}</pre>
        </div>
    </div>
</body>
</html>
"""

        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        # Send email
        with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
            if self.smtp_user and self.smtp_password:
                server.starttls()
                server.login(self.smtp_user, self.smtp_password)
            server.sendmail(self.smtp_from, self.email_recipients, msg.as_string())

    async def _send_webhook_alert(self, alert_data: dict[str, Any]) -> None:
        """Send alert via generic webhook.

        Args:
            alert_data: Alert information dictionary
        """
        if not self.webhook_url:
            return

        async with httpx.AsyncClient() as client:
            await client.post(
                self.webhook_url,
                json=alert_data,
                timeout=10.0,
            )

    async def _send_slack_alert(self, alert_data: dict[str, Any]) -> None:
        """Send alert via Slack webhook.

        Args:
            alert_data: Alert information dictionary
        """
        if not self.webhook_url or "slack.com" not in self.webhook_url:
            return

        level = alert_data["level"]
        title = alert_data["title"]
        message = alert_data["message"]
        timestamp = alert_data["timestamp"]
        metadata = alert_data["metadata"]

        # Color coding by level
        color_map = {
            "info": "#36a64f",
            "warning": "#ff9800",
            "error": "#f44336",
            "critical": "#d32f2f",
        }

        slack_message = {
            "attachments": [
                {
                    "color": color_map.get(level, "#808080"),
                    "title": title,
                    "text": message,
                    "fields": [
                        {"title": "Level", "value": level.upper(), "short": True},
                        {"title": "Time", "value": timestamp, "short": True},
                    ],
                    "footer": "Trading System Alert",
                    "ts": int(datetime.now(UTC).timestamp()),
                }
            ]
        }

        if metadata:
            slack_message["attachments"][0]["fields"].append(
                {
                    "title": "Metadata",
                    "value": f"```{self._format_metadata(metadata)}```",
                    "short": False,
                }
            )

        async with httpx.AsyncClient() as client:
            await client.post(
                self.webhook_url,
                json=slack_message,
                timeout=10.0,
            )

    async def _send_discord_alert(self, alert_data: dict[str, Any]) -> None:
        """Send alert via Discord webhook.

        Args:
            alert_data: Alert information dictionary
        """
        if not self.webhook_url or "discord.com" not in self.webhook_url:
            return

        level = alert_data["level"]
        title = alert_data["title"]
        message = alert_data["message"]
        timestamp = alert_data["timestamp"]
        metadata = alert_data["metadata"]

        # Color coding by level (Discord uses integer colors)
        color_map = {
            "info": 3447003,  # Blue
            "warning": 16776960,  # Yellow
            "error": 15158332,  # Red
            "critical": 10038562,  # Dark red
        }

        discord_message = {
            "embeds": [
                {
                    "title": title,
                    "description": message,
                    "color": color_map.get(level, 8421504),
                    "fields": [
                        {"name": "Level", "value": level.upper(), "inline": True},
                        {"name": "Time", "value": timestamp, "inline": True},
                    ],
                    "footer": {"text": "Trading System Alert"},
                    "timestamp": timestamp,
                }
            ]
        }

        if metadata:
            discord_message["embeds"][0]["fields"].append(
                {
                    "name": "Metadata",
                    "value": f"```{self._format_metadata(metadata)}```",
                    "inline": False,
                }
            )

        async with httpx.AsyncClient() as client:
            await client.post(
                self.webhook_url,
                json=discord_message,
                timeout=10.0,
            )

    def _format_metadata(self, metadata: dict[str, Any]) -> str:
        """Format metadata dictionary for display.

        Args:
            metadata: Metadata dictionary

        Returns:
            Formatted string
        """
        if not metadata:
            return "None"

        lines = []
        for key, value in metadata.items():
            lines.append(f"{key}: {value}")

        return "\n".join(lines)


# Global alerting service instance
alerting_service = AlertingService()
