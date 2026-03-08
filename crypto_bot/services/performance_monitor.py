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
        exchange: Optional[Any] = None,
        capital_ladder: Optional[Any] = None,
    ) -> None:
        super().__init__(
            name="performance_monitor",
            bus=bus,
            config=config or {},
            loop_interval_seconds=300,  # Check every 5 minutes
        )

        self._whatsapp = whatsapp
        self._exchange = exchange
        self._capital_ladder = capital_ladder

        # Trade history (loaded from disk)
        self._trades: List[TradeRecord] = []

        # Track which scheduled reports have been sent (persisted to avoid duplicates across restarts)
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
        """Send Account Snapshot via ntfy (replaces old Performance Report)."""
        if not self._whatsapp:
            return

        now = datetime.now(timezone.utc)
        now_rome = now.astimezone(REPORT_TIMEZONE)

        # Get equity from exchange
        equity_str = "N/A"
        try:
            if self._exchange and hasattr(self._exchange, "get_account_state"):
                acct = await self._exchange.get_account_state()
                equity = acct.get("equity", 0)
                equity_str = f"${equity:,.2f}" if equity else "N/A"
        except Exception:
            pass

        # Today's trades (midnight UTC)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_trades = [
            t for t in self._trades
            if datetime.fromisoformat(t.closed_at).replace(tzinfo=timezone.utc) >= today_start
        ]

        today_count = len(today_trades)
        today_wins = sum(1 for t in today_trades if t.is_win)
        today_pnl = sum(t.realized_pnl for t in today_trades)
        tp_sign = "+" if today_pnl >= 0 else ""

        # Rolling metrics (30 days)
        window = self._get_rolling_trades()
        total = len(window)
        wins = sum(1 for t in window if t.is_win)
        win_rate = (wins / total * 100) if total else 0
        gross_wins = sum(t.realized_pnl for t in window if t.realized_pnl > 0)
        gross_losses = abs(sum(t.realized_pnl for t in window if t.realized_pnl < 0))
        pf = gross_wins / gross_losses if gross_losses > 0 else float("inf")

        # Open positions
        open_positions = "none"
        try:
            if self._exchange and hasattr(self._exchange, "get_positions"):
                positions = await self._exchange.get_positions()
                if positions:
                    open_positions = ", ".join(
                        f"{p.get('coin', '?')} {p.get('szi', '?')}" for p in positions[:5]
                    )
        except Exception:
            pass

        # Top symbols by P&L
        sym_pnl: Dict[str, float] = {}
        for t in window:
            sym_pnl[t.symbol] = sym_pnl.get(t.symbol, 0.0) + t.realized_pnl
        sorted_syms = sorted(sym_pnl.items(), key=lambda x: x[1], reverse=True)[:5]
        sym_lines = ", ".join(f"{s} {v:+.2f}" for s, v in sorted_syms) if sorted_syms else "none"

        # Ladder status
        ladder_line = ""
        if self._capital_ladder:
            state = getattr(self._capital_ladder, "_state", None)
            if state:
                ladder_line = f"Ladder: L{state.current_level} [{state.status}]"

        # Max drawdown
        max_dd = 0.0
        if window:
            running = 0.0
            peak = 0.0
            for t in window:
                running += t.realized_pnl
                peak = max(peak, running)
                dd = peak - running
                max_dd = max(max_dd, dd)

        time_str = now_rome.strftime("%H:%M %d/%m")

        report = (
            f"ACCOUNT SNAPSHOT\n"
            f"Time: {time_str}\n"
            f"Equity: {equity_str}\n"
            f"Net P&L today: {tp_sign}${today_pnl:.2f} ({today_count} trades)\n"
            f"Today: {today_wins}W/{today_count - today_wins}L\n"
            f"---\n"
            f"Win rate (30d): {win_rate:.0f}% ({total} trades)\n"
            f"Profit factor: {pf:.2f}\n"
            f"Max DD: ${max_dd:.2f}\n"
            f"Open: {open_positions}\n"
            f"Top symbols: {sym_lines}\n"
            f"{ladder_line}"
        )

        try:
            await self._whatsapp._send_message(
                report,
                title=f"Account Snapshot — {time_str}",
                tags="chart_with_upwards_trend",
            )
            self._logger.info("Account Snapshot sent")
        except Exception as e:
            self._logger.error("Failed to send Account Snapshot: %s", e)

    async def _check_win_rate_alert(self) -> None:
        """Log win rate warning (ntfy alert disabled — covered by Account Snapshot)."""
        window = self._get_rolling_trades()
        total = len(window)

        if total < MIN_TRADES_FOR_ALERT:
            return

        wins = sum(1 for t in window if t.is_win)
        win_rate = wins / total

        if win_rate < WIN_RATE_ALERT_THRESHOLD:
            self._logger.warning("Low win rate: %.1f%% (%d/%d)", win_rate * 100, wins, total)

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
                self._last_report_date_hour = data.get("last_report_date_hour")

                # Trim old trades
                self._trim_old_trades()

                self._logger.info(
                    "Loaded %d trades from %s (last_report=%s)",
                    len(self._trades), DATA_FILE, self._last_report_date_hour,
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
                "last_report_date_hour": self._last_report_date_hour,
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
