"""
FLAG-Trader Trade Logger
========================

Logs every FLAG-Trader decision and trade outcome to JSONL files
for future model retraining and performance analysis.

Files:
- decisions_YYYY_MM.jsonl  — every model decision (BUY/SELL/HOLD)
- outcomes_YYYY_MM.jsonl   — closed trades with P&L and exit reason
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    """A single FLAG-Trader decision with optional outcome data."""

    timestamp: str  # ISO format
    symbol: str
    action: str  # "BUY", "SELL", "HOLD"
    action_id: int  # 0=Sell, 1=Hold, 2=Buy
    confidence: float  # state value from value head
    log_prob: float
    # Market context at decision time
    candles_summary: dict  # {last_close, pct_change_20, volume_avg}
    portfolio: dict  # {cash, position_value, total}
    # Outcome (filled after trade closes)
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    pnl_usd: Optional[float] = None
    pnl_pct: Optional[float] = None
    exit_reason: Optional[str] = None  # "take_profit", "stop_loss", "timeout", etc.
    hold_duration_minutes: Optional[float] = None
    market_state_summary: Optional[dict] = None  # {rsi, adx, regime, atr_pct, ema9_slope}
    prompt_summary: str = ""  # First 200 chars of prompt for audit trail


class FlagTradeLogger:
    """Logs every FLAG-Trader decision and trade outcome for future retraining."""

    def __init__(self, log_dir: Optional[Path] = None) -> None:
        # Resolve log_dir: explicit arg > HLQUANTBOT_DATA_DIR env > default cwd-relative
        import os

        if log_dir is None:
            env_dir = os.environ.get("HLQUANTBOT_DATA_DIR")
            if env_dir:
                log_dir = Path(env_dir) / "trade_logs"
            else:
                log_dir = Path("data/trade_logs")

        self.log_dir = Path(log_dir)
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.exception("FlagTradeLogger: failed to create log_dir=%s", self.log_dir)
            raise

        # Log the resolved absolute path so we can see it in container logs
        try:
            resolved = self.log_dir.resolve()
        except OSError:
            resolved = self.log_dir
        logger.info("FlagTradeLogger log_dir resolved to: %s", resolved)

        self._pending_trades: dict[str, TradeRecord] = {}  # symbol -> record

    def log_decision(self, record: TradeRecord) -> None:
        """Log a trading decision (before execution)."""
        log_file = self.log_dir / f"decisions_{datetime.now(timezone.utc).strftime('%Y_%m')}.jsonl"
        try:
            with open(log_file, "a") as f:
                f.write(json.dumps(asdict(record), default=str) + "\n")
        except OSError:
            logger.exception("FlagTradeLogger: failed to write decision for %s to %s", record.symbol, log_file)
            return

        # Track non-HOLD decisions for outcome updates
        if record.action != "HOLD":
            self._pending_trades[record.symbol] = record

        logger.debug("Decision logged: %s %s (confidence=%.4f)", record.symbol, record.action, record.confidence)

    def log_outcome(
        self,
        symbol: str,
        entry_price: float,
        exit_price: float,
        pnl_usd: float,
        pnl_pct: float,
        exit_reason: str,
        hold_duration_minutes: float,
        side: Optional[str] = None,
    ) -> None:
        """
        Log a closed trade outcome.

        If a matching decision is in ``_pending_trades`` (same process, not restarted),
        we enrich the outcome record with decision-time context (confidence, prompt,
        market_state_summary, etc.). Otherwise we synthesise a minimal TradeRecord so
        the outcome is ALWAYS persisted — critical after process restarts where the
        in-memory pending map has been lost.
        """
        record = self._pending_trades.pop(symbol, None)
        if record is None:
            # Fallback: synthesise a minimal record so the outcome is still logged.
            action = "SELL" if (side and side.lower() == "short") else "BUY"
            record = TradeRecord(
                timestamp=datetime.now(timezone.utc).isoformat(),
                symbol=symbol,
                action=action,
                action_id=0 if action == "SELL" else 2,
                confidence=0.0,
                log_prob=0.0,
                candles_summary={},
                portfolio={},
            )
            logger.debug(
                "log_outcome: no pending decision for %s (likely restart); "
                "writing minimal outcome record",
                symbol,
            )

        record.entry_price = entry_price
        record.exit_price = exit_price
        record.pnl_usd = pnl_usd
        record.pnl_pct = pnl_pct
        record.exit_reason = exit_reason
        record.hold_duration_minutes = hold_duration_minutes

        log_file = self.log_dir / f"outcomes_{datetime.now(timezone.utc).strftime('%Y_%m')}.jsonl"
        try:
            with open(log_file, "a") as f:
                f.write(json.dumps(asdict(record), default=str) + "\n")
        except OSError:
            logger.exception("FlagTradeLogger: failed to write outcome for %s to %s", symbol, log_file)
            return

        logger.info(
            "Trade outcome logged: %s %s PnL=$%.2f (%s) -> %s",
            symbol, record.action, pnl_usd, exit_reason, log_file.name,
        )

    # Backwards-compatible alias (callers may still use the old name).
    update_outcome = log_outcome

    def get_training_data(self, min_date: Optional[str] = None) -> list[dict]:
        """Load all outcomes for retraining."""
        records: list[dict] = []
        for f in sorted(self.log_dir.glob("outcomes_*.jsonl")):
            with open(f) as fh:
                for line in fh:
                    r = json.loads(line)
                    if min_date and r["timestamp"] < min_date:
                        continue
                    records.append(r)
        return records

    def get_stats(self) -> dict:
        """Quick stats on logged trades."""
        records = self.get_training_data()
        if not records:
            return {"total": 0}
        wins = [r for r in records if (r.get("pnl_usd") or 0) > 0]
        return {
            "total": len(records),
            "wins": len(wins),
            "losses": len(records) - len(wins),
            "win_rate": len(wins) / len(records) * 100,
            "total_pnl": sum(r.get("pnl_usd", 0) for r in records),
        }
