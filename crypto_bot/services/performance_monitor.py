"""
HLQuantBot Performance Monitor Service
=======================================

Tracks live trading performance and sends periodic reports via ntfy.

Features:
- Records every closed trade (from Topic.FILLS position_closed events)
- Reports every 5 completed trades or every 24 hours
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
REPORT_EVERY_N_TRADES = 5        # Report every N completed trades
REPORT_INTERVAL_HOURS = 24       # Also report every 24h
ROLLING_WINDOW_DAYS = 30         # Keep 30 days of history


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

    @property
    def is_win(self) -> bool:
        return self.realized_pnl > 0


class PerformanceMonitorService(BaseService):
    """
    Tracks closed trades and sends periodic performance reports.

    Subscribes to: Topic.FILLS (position_closed events)
    Reports via: WhatsAppService (ntfy.sh)
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
            loop_interval_seconds=3600,  # Check every hour for 24h trigger
        )

        self._whatsapp = whatsapp

        # Trade history (loaded from disk)
        self._trades: List[TradeRecord] = []

        # Counters for report triggers
        self._trades_since_last_report: int = 0
        self._last_report_time: Optional[datetime] = None

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def _on_start(self) -> None:
        """Load persisted data and subscribe to fills."""
        self._load_data()

        if self.bus:
            await self.subscribe(Topic.FILLS, self._handle_fill_event)

        self._last_report_time = datetime.now(timezone.utc)
        self._logger.info(
            "PerformanceMonitorService started: %d historical trades loaded",
            len(self._trades),
        )

    async def _on_stop(self) -> None:
        """Save data on shutdown."""
        self._save_data()
        self._logger.info("PerformanceMonitorService stopped")

    async def _run_iteration(self) -> None:
        """Check if 24h report is due."""
        if self._last_report_time is None:
            return

        elapsed = datetime.now(timezone.utc) - self._last_report_time
        if elapsed >= timedelta(hours=REPORT_INTERVAL_HOURS) and self._trades:
            await self._send_report()

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
            )

            self._trades.append(record)
            self._trades_since_last_report += 1
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

            # Report every N trades
            if self._trades_since_last_report >= REPORT_EVERY_N_TRADES:
                await self._send_report()

        except Exception as e:
            self._logger.error("Error handling fill event: %s", e, exc_info=True)

    # =========================================================================
    # Reports & Alerts
    # =========================================================================

    async def _send_report(self) -> None:
        """Send performance report via ntfy."""
        if not self._whatsapp or not self._trades:
            return

        now = datetime.now(timezone.utc)
        window = self._get_rolling_trades()
        total = len(window)

        if total == 0:
            return

        wins = [t for t in window if t.is_win]
        losses = [t for t in window if not t.is_win]
        win_rate = len(wins) / total * 100

        total_pnl = sum(t.realized_pnl for t in window)
        avg_win = sum(t.realized_pnl for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t.realized_pnl for t in losses) / len(losses) if losses else 0

        tp_exits = sum(1 for t in window if t.exit_reason == "take_profit")
        sl_exits = sum(1 for t in window if t.exit_reason == "stop_loss")

        # Last 24h stats
        cutoff_24h = now - timedelta(hours=24)
        recent = [
            t for t in window
            if datetime.fromisoformat(t.closed_at) >= cutoff_24h
        ]
        recent_count = len(recent)
        recent_wins = sum(1 for t in recent if t.is_win)
        recent_win_rate = (recent_wins / recent_count * 100) if recent_count else 0
        recent_pnl = sum(t.realized_pnl for t in recent)

        pnl_sign = "+" if total_pnl >= 0 else ""
        recent_pnl_sign = "+" if recent_pnl >= 0 else ""

        report = (
            f"Performance Report — {total} trades ({ROLLING_WINDOW_DAYS}d)\n"
            f"Win rate: {win_rate:.1f}% ({len(wins)}/{total})\n"
            f"Total P&L: {pnl_sign}${total_pnl:.2f}\n"
            f"Avg win: +${avg_win:.2f} | Avg loss: ${avg_loss:.2f}\n"
            f"TP exits: {tp_exits} | SL exits: {sl_exits}\n"
            f"Last 24h: {recent_count} trades, {recent_win_rate:.1f}% win, "
            f"{recent_pnl_sign}${recent_pnl:.2f}"
        )

        try:
            await self._whatsapp._send_message(
                report,
                title="Performance Report",
                tags="chart_with_upwards_trend",
            )
            self._logger.info("Performance report sent")
        except Exception as e:
            self._logger.error("Failed to send performance report: %s", e)

        self._trades_since_last_report = 0
        self._last_report_time = now

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
            "trades_since_report": self._trades_since_last_report,
        }
