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
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

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
    exit_reason: Optional[str] = None  # legacy: "take_profit", "stop_loss", "timeout", ...
    hold_duration_minutes: Optional[float] = None
    market_state_summary: Optional[dict] = None  # {rsi, adx, regime, atr_pct, ema9_slope}
    prompt_summary: str = ""  # First 200 chars of prompt for audit trail
    # --- STAGE A forecast-mode fields (all Optional, backward compatible) ---
    trade_id: Optional[str] = None
    predicted_tp_pct: Optional[float] = None
    predicted_sl_pct: Optional[float] = None
    predicted_tp_price: Optional[float] = None
    predicted_sl_price: Optional[float] = None
    expiry_at: Optional[str] = None  # ISO format
    k_candles: Optional[int] = None
    candle_interval_sec: Optional[int] = None
    real_high_curve: Optional[list] = None
    real_low_curve: Optional[list] = None
    real_observed_k: Optional[int] = None
    exit_reason_v2: Optional[str] = None  # "tp" | "sl" | "expiry" | "manual"


def _map_exit_reason_to_v2(legacy: Optional[str]) -> Optional[str]:
    """Map legacy exit_reason to STAGE A v2 enum {tp, sl, expiry, manual}."""
    if not legacy:
        return None
    s = legacy.lower()
    if s in ("take_profit", "tp"):
        return "tp"
    if s in ("stop_loss", "sl", "trailing_stop", "violation_exit"):
        return "sl"
    if s in ("timeout", "regime_exit", "max_hold", "expiry", "regime_change"):
        return "expiry"
    if s in ("manual", "external_close"):
        return "manual"
    return None


class FlagTradeLogger:
    """Logs every FLAG-Trader decision and trade outcome for future retraining."""

    def __init__(self, log_dir: Optional[Path] = None) -> None:
        # Resolve log_dir: explicit arg > HLQUANTBOT_DATA_DIR env > default cwd-relative
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

        # symbol -> FIFO list of pending decisions. Using a list (instead of a single
        # record) handles reversal scenarios where a new decision arrives before the
        # previous one has been closed on the exchange.
        self._pending_trades: dict[str, list[TradeRecord]] = {}
        self._pending_cap: int = 10  # max pending records per symbol

        # Sidecar dir for STAGE A forecast curves (per-trade JSON, not in main JSONL).
        self.forecasts_dir = self.log_dir / "forecasts"
        try:
            self.forecasts_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.exception("FlagTradeLogger: failed to create forecasts_dir=%s", self.forecasts_dir)

    def log_decision(self, record: TradeRecord) -> None:
        """Log a trading decision (before execution).

        Auto-assigns ``trade_id`` (uuid4 hex) on non-HOLD records if missing,
        and derives ``predicted_tp_price`` / ``predicted_sl_price`` from
        ``predicted_tp_pct`` / ``predicted_sl_pct`` if entry context is known.
        """
        if record.action != "HOLD" and not record.trade_id:
            record.trade_id = uuid.uuid4().hex
        log_file = self.log_dir / f"decisions_{datetime.now(timezone.utc).strftime('%Y_%m')}.jsonl"
        try:
            with open(log_file, "a") as f:
                f.write(json.dumps(asdict(record), default=str) + "\n")
        except OSError:
            logger.exception("FlagTradeLogger: failed to write decision for %s to %s", record.symbol, log_file)
            return

        # Track non-HOLD decisions for outcome updates (FIFO per symbol).
        if record.action != "HOLD":
            bucket = self._pending_trades.setdefault(record.symbol, [])
            bucket.append(record)
            if len(bucket) > self._pending_cap:
                dropped = bucket.pop(0)
                logger.warning(
                    "FlagTradeLogger: pending cap (%d) hit for %s — dropping oldest "
                    "decision (action=%s ts=%s). Decisions are not being closed; "
                    "investigate execution/outcome wiring.",
                    self._pending_cap, record.symbol, dropped.action, dropped.timestamp,
                )

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
        *,
        real_high_curve: Optional[list] = None,
        real_low_curve: Optional[list] = None,
        exit_reason_v2: Optional[str] = None,
    ) -> None:
        """
        Log a closed trade outcome.

        If a matching decision is in ``_pending_trades`` (same process, not restarted),
        we enrich the outcome record with decision-time context (confidence, prompt,
        market_state_summary, etc.). Otherwise we synthesise a minimal TradeRecord so
        the outcome is ALWAYS persisted — critical after process restarts where the
        in-memory pending map has been lost.
        """
        bucket = self._pending_trades.get(symbol)
        record: Optional[TradeRecord] = None
        if bucket:
            # FIFO: the oldest pending decision matches the close event on the exchange.
            record = bucket.pop(0)
            if not bucket:
                # Keep the dict clean — debug tooling iterates over keys.
                self._pending_trades.pop(symbol, None)
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
        if exit_reason_v2 is not None:
            record.exit_reason_v2 = exit_reason_v2
        else:
            record.exit_reason_v2 = _map_exit_reason_to_v2(exit_reason)
        if real_high_curve is not None:
            record.real_high_curve = list(real_high_curve)
            record.real_observed_k = len(real_high_curve)
        if real_low_curve is not None:
            record.real_low_curve = list(real_low_curve)

        # Write sidecar JSON (per-trade) atomically. Stays out of the main
        # JSONL so the curve payload doesn't bloat decision/outcome scans.
        if record.trade_id and (real_high_curve is not None or real_low_curve is not None):
            try:
                sidecar_path = self.forecasts_dir / f"{record.trade_id}.json"
                sidecar = {
                    "trade_id": record.trade_id,
                    "symbol": symbol,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "predicted_tp_pct": record.predicted_tp_pct,
                    "predicted_sl_pct": record.predicted_sl_pct,
                    "predicted_tp_price": record.predicted_tp_price,
                    "predicted_sl_price": record.predicted_sl_price,
                    "k_candles": record.k_candles,
                    "candle_interval_sec": record.candle_interval_sec,
                    "real_high_curve": record.real_high_curve,
                    "real_low_curve": record.real_low_curve,
                    "real_observed_k": record.real_observed_k,
                    "expiry_at": record.expiry_at,
                    "exit_reason_v2": record.exit_reason_v2,
                    "exit_reason_legacy": exit_reason,
                    "timestamp": record.timestamp,
                    "closed_at": datetime.now(timezone.utc).isoformat(),
                }
                tmp_path = sidecar_path.with_suffix(".json.tmp")
                with open(tmp_path, "w") as fh:
                    json.dump(sidecar, fh, default=str)
                os.replace(tmp_path, sidecar_path)
            except OSError:
                logger.exception(
                    "FlagTradeLogger: failed to write sidecar for trade_id=%s",
                    record.trade_id,
                )

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
