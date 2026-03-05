"""
HLQuantBot Performance Monitor Service
=======================================

Tracks live trading performance and sends scheduled reports via ntfy.

Features:
- Records every closed trade (from Topic.FILLS position_closed events)
- Sends scheduled reports at 08:00 and 20:00 (Europe/Rome), each covering last 12h
- Alerts if win rate drops below 35% after 10+ trades
- Persists trade history to ~/.hlquantbot/performance_monitor.json
- Rolling 30-day window to keep data manageable

Author: Francesco Carlesi
"""

import json
import logging
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from .base import BaseService
from .message_bus import Message, MessageBus
from ..core.enums import Topic

logger = logging.getLogger(__name__)

# Persistence directory
DATA_DIR = Path(os.environ.get("HLQUANTBOT_DATA_DIR", str(Path.home() / ".hlquantbot")))
DATA_FILE = DATA_DIR / "performance_monitor.json"

# Thresholds
WIN_RATE_ALERT_THRESHOLD = 0.35  # Alert if win rate < 35%
MIN_TRADES_FOR_ALERT = 10        # Only alert after N trades
ROLLING_WINDOW_DAYS = 30         # Keep 30 days of history

# Schedule: report times in Europe/Rome timezone
REPORT_TIMEZONE = ZoneInfo("Europe/Rome")
REPORT_HOURS = [8, 20]  # 08:00 and 20:00
REPORT_WINDOW_HOURS = 12  # Each report covers last 12h


@dataclass
class TradeRecord:
    """Single closed trade record."""

    symbol: str
    direction: str  # "long" or "short"
    entry_price: float
    exit_price: float
    realized_pnl: float
    pnl_pct: float
    exit_reason: str  # "take_profit", "stop_loss", "manual", etc.
    closed_at: str    # ISO format timestamp
    fee: float = 0.0  # Trading fee (entry + exit)
    gross_pnl: float = 0.0  # P&L before fees

    @property
    def is_win(self) -> bool:
        return self.realized_pnl > 0


class PerformanceMonitorService(BaseService):
    """
    Tracks closed trades and sends scheduled performance reports.

    Subscribes to: Topic.FILLS (position_closed events)
    Reports via: WhatsAppService (ntfy.sh)

    Schedule: 08:00 and 20:00 Europe/Rome, each covering last 12 hours.
    """

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        config: Optional[Dict[str, Any]] = None,
        whatsapp: Optional[Any] = None,
    ) -> None:
        super().__init__(
            name="performance_monitor",
            bus=bus,
            config=config or {},
            loop_interval_seconds=300,  # Check every 5 minutes
        )

        self._whatsapp = whatsapp

        # Trade history (loaded from disk)
        self._trades: List[TradeRecord] = []

        # Track which scheduled reports have been sent (to avoid duplicates)
        self._last_report_date_hour: Optional[str] = None

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def _on_start(self) -> None:
        """Load persisted data and subscribe to fills."""
        self._load_data()

        if self.bus:
            await self.subscribe(Topic.FILLS, self._handle_fill_event)

        self._logger.info(
            "PerformanceMonitorService started: %d historical trades loaded, "
            "scheduled reports at %s Europe/Rome",
            len(self._trades),
            "/".join(f"{h}:00" for h in REPORT_HOURS),
        )

    async def _on_stop(self) -> None:
        """Save data on shutdown."""
        self._save_data()
        self._logger.info("PerformanceMonitorService stopped")

    async def _run_iteration(self) -> None:
        """Check if a scheduled report is due."""
        now_rome = datetime.now(REPORT_TIMEZONE)
        current_hour = now_rome.hour
        date_hour_key = now_rome.strftime("%Y-%m-%d") + f"_{current_hour}"

        if current_hour in REPORT_HOURS and date_hour_key != self._last_report_date_hour:
            await self._send_scheduled_report()
            self._last_report_date_hour = date_hour_key

    async def _health_check_impl(self) -> bool:
        return True

    # =========================================================================
    # Event Handling
    # =========================================================================

    async def _handle_fill_event(self, message: Message) -> None:
        """Handle fill events, record closed trades."""
        try:
            payload = message.payload
            event = payload.get("event", "")

            if event != "position_closed":
                return

            record = TradeRecord(
                symbol=payload.get("symbol", "UNKNOWN"),
                direction=payload.get("side", payload.get("direction", "unknown")),
                entry_price=float(payload.get("entry_price", 0)),
                exit_price=float(payload.get("exit_price", 0)),
                realized_pnl=float(payload.get("realized_pnl", 0)),
                pnl_pct=float(payload.get("pnl_pct", 0)),
                exit_reason=payload.get("exit_reason", "unknown"),
                closed_at=datetime.now(timezone.utc).isoformat(),
                fee=float(payload.get("fee", 0)),
                gross_pnl=float(payload.get("gross_pnl", 0)),
            )

            self._trades.append(record)
            self._save_data()

            self._logger.info(
                "Trade recorded: %s %s P&L=$%.2f (%.2f%%) [%s]",
                record.direction.upper(),
                record.symbol,
                record.realized_pnl,
                record.pnl_pct,
                record.exit_reason,
            )

            # Check win rate alert
            await self._check_win_rate_alert()

        except Exception as e:
            self._logger.error("Error handling fill event: %s", e, exc_info=True)

    # =========================================================================
    # Reports & Alerts
    # =========================================================================

    async def _send_scheduled_report(self) -> None:
        """Send scheduled 12h performance report via ntfy."""
        if not self._whatsapp:
            return

        now = datetime.now(timezone.utc)
        now_rome = now.astimezone(REPORT_TIMEZONE)

        # Last 12 hours trades
        cutoff_12h = now - timedelta(hours=REPORT_WINDOW_HOURS)
        recent = [
            t for t in self._trades
            if datetime.fromisoformat(t.closed_at).replace(tzinfo=timezone.utc) >= cutoff_12h
        ]

        # --- Build 12h section ---
        recent_count = len(recent)
        if recent_count > 0:
            recent_wins = [t for t in recent if t.is_win]
            recent_losses = [t for t in recent if not t.is_win]
            recent_wr = len(recent_wins) / recent_count * 100
            recent_pnl = sum(t.realized_pnl for t in recent)
            recent_avg_win = (
                sum(t.realized_pnl for t in recent_wins) / len(recent_wins)
                if recent_wins else 0
            )
            recent_avg_loss = (
                sum(t.realized_pnl for t in recent_losses) / len(recent_losses)
                if recent_losses else 0
            )
            recent_tp = sum(1 for t in recent if t.exit_reason == "take_profit")
            recent_sl = sum(1 for t in recent if t.exit_reason == "stop_loss")
            total_fees = sum(t.fee for t in recent)
            gross_pnl = sum(t.gross_pnl for t in recent) if any(t.gross_pnl for t in recent) else recent_pnl + total_fees
            gp_sign = "+" if gross_pnl >= 0 else ""
            rp_sign = "+" if recent_pnl >= 0 else ""

            recent_section = (
                f"Last 12h: {recent_count} trades\n"
                f"Win rate: {recent_wr:.0f}% ({len(recent_wins)}W/{len(recent_losses)}L)\n"
                f"P&L: {rp_sign}${recent_pnl:.2f} (fees: ${total_fees:.2f})\n"
                f"Avg win: +${recent_avg_win:.2f} | Avg loss: ${recent_avg_loss:.2f}\n"
                f"TP: {recent_tp} | SL: {recent_sl}"
            )
        else:
            recent_section = "Last 12h: no trades"

        # --- Compose ---
        period = "notte" if now_rome.hour == 8 else "giorno"
        time_str = now_rome.strftime("%H:%M")

        report = (
            f"Report {period} — {time_str}\n"
            f"{recent_section}"
        )

        try:
            await self._whatsapp._send_message(
                report,
                title=f"Performance Report ({period})",
                tags="chart_with_upwards_trend",
            )
            self._logger.info("Scheduled performance report sent (%s)", period)
        except Exception as e:
            self._logger.error("Failed to send performance report: %s", e)

    async def _check_win_rate_alert(self) -> None:
        """Alert if win rate is below threshold after enough trades."""
        if not self._whatsapp:
            return

        window = self._get_rolling_trades()
        total = len(window)

        if total < MIN_TRADES_FOR_ALERT:
            return

        wins = sum(1 for t in window if t.is_win)
        win_rate = wins / total

        if win_rate < WIN_RATE_ALERT_THRESHOLD:
            alert = (
                f"ALERT: Low Win Rate\n"
                f"Win rate {win_rate:.1%} ({wins}/{total}) — "
                f"below {WIN_RATE_ALERT_THRESHOLD:.0%} threshold\n"
                f"Expected: ~50%. Check strategy conditions."
            )

            try:
                await self._whatsapp._send_message(
                    alert,
                    title="Low Win Rate Alert",
                    priority=True,
                    tags="warning",
                )
                self._logger.warning("Win rate alert sent: %.1f%%", win_rate * 100)
            except Exception as e:
                self._logger.error("Failed to send win rate alert: %s", e)

    # =========================================================================
    # Data Helpers
    # =========================================================================

    def _get_rolling_trades(self) -> List[TradeRecord]:
        """Get trades within the rolling window."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=ROLLING_WINDOW_DAYS)
        return [
            t for t in self._trades
            if datetime.fromisoformat(t.closed_at) >= cutoff
        ]

    # =========================================================================
    # Persistence
    # =========================================================================

    def _load_data(self) -> None:
        """Load trade history from disk."""
        try:
            if DATA_FILE.exists():
                with open(DATA_FILE, "r") as f:
                    data = json.load(f)

                self._trades = [
                    TradeRecord(**t) for t in data.get("trades", [])
                ]

                # Trim old trades
                self._trim_old_trades()

                self._logger.info(
                    "Loaded %d trades from %s", len(self._trades), DATA_FILE
                )
        except Exception as e:
            self._logger.warning("Failed to load performance data: %s", e)
            self._trades = []

    def _save_data(self) -> None:
        """Save trade history to disk."""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)

            data = {
                "trades": [asdict(t) for t in self._trades],
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }

            with open(DATA_FILE, "w") as f:
                json.dump(data, f, indent=2)

        except Exception as e:
            self._logger.error("Failed to save performance data: %s", e)

    def _trim_old_trades(self) -> None:
        """Remove trades older than the rolling window."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=ROLLING_WINDOW_DAYS)
        before = len(self._trades)
        self._trades = [
            t for t in self._trades
            if datetime.fromisoformat(t.closed_at) >= cutoff
        ]
        trimmed = before - len(self._trades)
        if trimmed > 0:
            self._logger.info("Trimmed %d old trades", trimmed)

    # =========================================================================
    # Metrics
    # =========================================================================

    @property
    def metrics(self) -> Dict[str, Any]:
        """Service metrics for health dashboard."""
        window = self._get_rolling_trades()
        total = len(window)
        wins = sum(1 for t in window if t.is_win)

        return {
            "total_trades": total,
            "win_rate": (wins / total * 100) if total else 0,
            "total_pnl": sum(t.realized_pnl for t in window),
        }
