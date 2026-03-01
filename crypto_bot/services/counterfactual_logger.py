"""
HLQuantBot Counterfactual Logger Service
=========================================

Logs rejected trade setups and tracks whether they would have been profitable.

For each rejection:
1. Records entry price, direction, TP/SL targets
2. Monitors forward price via MarketState updates
3. Resolves as "would_have_won", "would_have_lost", or "expired" (24h timeout)
4. Sends daily summary report via ntfy

This answers: "Are we leaving money on the table by rejecting too many trades?"

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

# Persistence
DATA_DIR = Path(os.environ.get("HLQUANTBOT_DATA_DIR", str(Path.home() / ".hlquantbot")))
DATA_FILE = DATA_DIR / "counterfactual_logger.json"

# Limits
MAX_RESOLVED = 200          # Max resolved records to keep
RESOLVED_RETENTION_DAYS = 7 # Keep resolved for 7 days
PENDING_TIMEOUT_HOURS = 24  # Expire pending after 24h
REPORT_INTERVAL_HOURS = 24  # Daily report


@dataclass
class CounterfactualRecord:
    """A rejected trade setup being tracked for counterfactual analysis."""

    id: str
    symbol: str
    direction: str       # "long" or "short"
    entry_price: float
    tp_price: float
    sl_price: float
    rejection_reason: str
    ml_probability: float
    created_at: str      # ISO format

    # Resolution
    status: str = "pending"  # "pending", "would_have_won", "would_have_lost", "expired"
    resolved_at: Optional[str] = None
    resolved_price: Optional[float] = None


class CounterfactualLoggerService(BaseService):
    """
    Tracks rejected trades and determines if they would have been profitable.

    Called directly by main.py (log_rejection) and subscribes to Topic.ORDERS
    for risk manager rejections.
    """

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        config: Optional[Dict[str, Any]] = None,
        whatsapp: Optional[Any] = None,
        take_profit_pct: float = 1.6,
        stop_loss_pct: float = 0.8,
    ) -> None:
        super().__init__(
            name="counterfactual_logger",
            bus=bus,
            config=config or {},
            loop_interval_seconds=300,  # Every 5 min — aligned with scan interval
        )

        self._whatsapp = whatsapp
        self._tp_pct = take_profit_pct
        self._sl_pct = stop_loss_pct

        # Records
        self._pending: List[CounterfactualRecord] = []
        self._resolved: List[CounterfactualRecord] = []

        # Market states for forward price checking
        self._market_states: Dict[str, Any] = {}

        # Report timing
        self._last_report_time: Optional[datetime] = None

        # Counter for unique IDs
        self._counter: int = 0

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def _on_start(self) -> None:
        """Load persisted data and subscribe to order events."""
        self._load_data()

        if self.bus:
            await self.subscribe(Topic.ORDERS, self._handle_order_event)

        self._last_report_time = datetime.now(timezone.utc)
        self._logger.info(
            "CounterfactualLoggerService started: %d pending, %d resolved",
            len(self._pending),
            len(self._resolved),
        )

    async def _on_stop(self) -> None:
        """Save data on shutdown."""
        self._save_data()
        self._logger.info("CounterfactualLoggerService stopped")

    async def _run_iteration(self) -> None:
        """Check pending records against current market states + daily report."""
        self._resolve_pending()
        self._expire_old_pending()

        # Daily report
        if self._last_report_time:
            elapsed = datetime.now(timezone.utc) - self._last_report_time
            if elapsed >= timedelta(hours=REPORT_INTERVAL_HOURS):
                await self._send_daily_report()

    async def _health_check_impl(self) -> bool:
        return True

    # =========================================================================
    # Public API — called by main.py
    # =========================================================================

    def log_rejection(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        reason: str,
        ml_probability: float = 0.0,
    ) -> None:
        """
        Log a rejected trade setup for counterfactual tracking.

        Args:
            symbol: Asset symbol (e.g. "BTC")
            direction: "long" or "short"
            entry_price: Price at rejection time
            reason: Why it was rejected (e.g. "spread", "ml_threshold", "tick_size")
            ml_probability: ML model probability (0-1)
        """
        # Calculate TP/SL prices
        if direction == "long":
            tp_price = entry_price * (1 + self._tp_pct / 100)
            sl_price = entry_price * (1 - self._sl_pct / 100)
        else:
            tp_price = entry_price * (1 - self._tp_pct / 100)
            sl_price = entry_price * (1 + self._sl_pct / 100)

        self._counter += 1
        record = CounterfactualRecord(
            id=f"cf_{self._counter}_{symbol}",
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            tp_price=tp_price,
            sl_price=sl_price,
            rejection_reason=reason,
            ml_probability=ml_probability,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        self._pending.append(record)
        self._save_data()

        self._logger.info(
            "Counterfactual logged: %s %s @ %.4f reason=%s prob=%.3f "
            "(TP=%.4f, SL=%.4f)",
            direction.upper(),
            symbol,
            entry_price,
            reason,
            ml_probability,
            tp_price,
            sl_price,
        )

    def update_market_states(self, states: Dict[str, Any]) -> None:
        """
        Update cached market states for forward price resolution.

        Called by main.py after fetching market states each scan.

        Args:
            states: Dict of symbol -> MarketState
        """
        self._market_states = states

    # =========================================================================
    # Order Event Handler (risk manager rejections)
    # =========================================================================

    async def _handle_order_event(self, message: Message) -> None:
        """Handle setup_rejected events from risk manager."""
        try:
            payload = message.payload
            event = payload.get("event", "")

            if event != "setup_rejected":
                return

            symbol = payload.get("symbol", "")
            direction = payload.get("direction", "long")
            entry_price = float(payload.get("entry_price", 0))
            reason = payload.get("reason", "risk_manager")

            if not symbol or entry_price <= 0:
                return

            self.log_rejection(
                symbol=symbol,
                direction=direction,
                entry_price=entry_price,
                reason=f"risk:{reason}",
                ml_probability=0.0,
            )

        except Exception as e:
            self._logger.error("Error handling order event: %s", e, exc_info=True)

    # =========================================================================
    # Forward Price Resolution
    # =========================================================================

    def _resolve_pending(self) -> None:
        """Check pending records against current market data."""
        if not self._market_states:
            return

        still_pending: List[CounterfactualRecord] = []

        for record in self._pending:
            state = self._market_states.get(record.symbol)
            if state is None:
                still_pending.append(record)
                continue

            # Get high/low from current candle
            candle_high = float(getattr(state, "high", 0))
            candle_low = float(getattr(state, "low", 0))

            if candle_high == 0 or candle_low == 0:
                still_pending.append(record)
                continue

            tp_hit = False
            sl_hit = False

            if record.direction == "long":
                tp_hit = candle_high >= record.tp_price
                sl_hit = candle_low <= record.sl_price
            else:  # short
                tp_hit = candle_low <= record.tp_price
                sl_hit = candle_high >= record.sl_price

            if tp_hit and sl_hit:
                # Both hit in same candle — conservative: count as SL
                record.status = "would_have_lost"
                record.resolved_at = datetime.now(timezone.utc).isoformat()
                record.resolved_price = record.sl_price
                self._resolved.append(record)
                self._logger.debug(
                    "Counterfactual resolved (both hit → SL): %s %s",
                    record.direction, record.symbol,
                )
            elif tp_hit:
                record.status = "would_have_won"
                record.resolved_at = datetime.now(timezone.utc).isoformat()
                record.resolved_price = record.tp_price
                self._resolved.append(record)
                self._logger.debug(
                    "Counterfactual resolved (TP hit): %s %s",
                    record.direction, record.symbol,
                )
            elif sl_hit:
                record.status = "would_have_lost"
                record.resolved_at = datetime.now(timezone.utc).isoformat()
                record.resolved_price = record.sl_price
                self._resolved.append(record)
                self._logger.debug(
                    "Counterfactual resolved (SL hit): %s %s",
                    record.direction, record.symbol,
                )
            else:
                still_pending.append(record)

        if len(still_pending) != len(self._pending):
            self._pending = still_pending
            self._save_data()

    def _expire_old_pending(self) -> None:
        """Expire pending records older than 24h."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=PENDING_TIMEOUT_HOURS)
        still_pending: List[CounterfactualRecord] = []
        expired_count = 0

        for record in self._pending:
            created = datetime.fromisoformat(record.created_at)
            if created < cutoff:
                record.status = "expired"
                record.resolved_at = now.isoformat()
                self._resolved.append(record)
                expired_count += 1
            else:
                still_pending.append(record)

        if expired_count > 0:
            self._pending = still_pending
            self._trim_resolved()
            self._save_data()
            self._logger.info("Expired %d pending counterfactuals", expired_count)

    # =========================================================================
    # Daily Report
    # =========================================================================

    async def _send_daily_report(self) -> None:
        """Send daily counterfactual summary via ntfy."""
        if not self._whatsapp:
            self._last_report_time = datetime.now(timezone.utc)
            return

        now = datetime.now(timezone.utc)
        cutoff_24h = now - timedelta(hours=24)

        # Get records resolved in last 24h
        recent = [
            r for r in self._resolved
            if r.resolved_at and datetime.fromisoformat(r.resolved_at) >= cutoff_24h
        ]

        # Also count rejections logged in last 24h (pending + recently resolved)
        recent_logged = [
            r for r in self._pending
            if datetime.fromisoformat(r.created_at) >= cutoff_24h
        ] + recent

        total_rejected = len(recent_logged)
        won = [r for r in recent if r.status == "would_have_won"]
        lost = [r for r in recent if r.status == "would_have_lost"]
        expired = [r for r in recent if r.status == "expired"]

        resolved_count = len(won) + len(lost)
        win_rate = (len(won) / resolved_count * 100) if resolved_count else 0

        # Theoretical P&L (sum of TP% for wins, SL% for losses)
        theoretical_pnl = len(won) * self._tp_pct - len(lost) * self._sl_pct

        # Rejection reasons breakdown
        reason_counts: Dict[str, int] = {}
        for r in recent_logged:
            reason_counts[r.rejection_reason] = reason_counts.get(r.rejection_reason, 0) + 1
        top_reason = sorted(reason_counts, key=reason_counts.__getitem__, reverse=True)[0] if reason_counts else "none"
        top_pct = (
            reason_counts.get(top_reason, 0) / total_rejected * 100
            if total_rejected else 0
        )
        reasons_str = ", ".join(
            f"{k}={v}" for k, v in sorted(reason_counts.items(), key=lambda x: -x[1])
        )

        pnl_sign = "+" if theoretical_pnl >= 0 else ""

        report = (
            f"Counterfactual Daily Report\n"
            f"Rejected trades (24h): {total_rejected}\n"
            f"Would have WON: {len(won)}"
            + (f" ({win_rate:.1f}% win rate)" if resolved_count else "")
            + f"\nWould have LOST: {len(lost)}\n"
            f"Expired/neutral: {len(expired)}\n"
            f"Theoretical P&L: {pnl_sign}{theoretical_pnl:.1f}% (if all taken)\n"
            f"Top rejection: {top_reason} ({top_pct:.0f}%)\n"
            f"All reasons: {reasons_str}\n"
            f"Still pending: {len(self._pending)}"
        )

        try:
            await self._whatsapp._send_message(
                report,
                title="Counterfactual Report",
                tags="mag",
            )
            self._logger.info("Counterfactual daily report sent")
        except Exception as e:
            self._logger.error("Failed to send counterfactual report: %s", e)

        self._last_report_time = now

    # =========================================================================
    # Persistence
    # =========================================================================

    def _load_data(self) -> None:
        """Load data from disk."""
        try:
            if DATA_FILE.exists():
                with open(DATA_FILE, "r") as f:
                    data = json.load(f)

                self._pending = [
                    CounterfactualRecord(**r) for r in data.get("pending", [])
                ]
                self._resolved = [
                    CounterfactualRecord(**r) for r in data.get("resolved", [])
                ]
                self._counter = data.get("counter", 0)

                self._trim_resolved()

                self._logger.info(
                    "Loaded counterfactual data: %d pending, %d resolved",
                    len(self._pending),
                    len(self._resolved),
                )
        except Exception as e:
            self._logger.warning("Failed to load counterfactual data: %s", e)
            self._pending = []
            self._resolved = []

    def _save_data(self) -> None:
        """Save data to disk."""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)

            data = {
                "pending": [asdict(r) for r in self._pending],
                "resolved": [asdict(r) for r in self._resolved],
                "counter": self._counter,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }

            with open(DATA_FILE, "w") as f:
                json.dump(data, f, indent=2)

        except Exception as e:
            self._logger.error("Failed to save counterfactual data: %s", e)

    def _trim_resolved(self) -> None:
        """Trim resolved records: keep max N and within retention window."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=RESOLVED_RETENTION_DAYS)

        self._resolved = [
            r for r in self._resolved
            if r.resolved_at and datetime.fromisoformat(r.resolved_at) >= cutoff
        ]

        # Cap at MAX_RESOLVED
        if len(self._resolved) > MAX_RESOLVED:
            self._resolved = self._resolved[-MAX_RESOLVED:]

    # =========================================================================
    # Metrics
    # =========================================================================

    @property
    def metrics(self) -> Dict[str, Any]:
        """Service metrics."""
        won = sum(1 for r in self._resolved if r.status == "would_have_won")
        lost = sum(1 for r in self._resolved if r.status == "would_have_lost")
        expired = sum(1 for r in self._resolved if r.status == "expired")

        return {
            "pending": len(self._pending),
            "resolved_total": len(self._resolved),
            "would_have_won": won,
            "would_have_lost": lost,
            "expired": expired,
        }
