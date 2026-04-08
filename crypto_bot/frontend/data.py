"""Read-only accessor for trade JSONL logs and per-trade forecast sidecars.

The bot writes append-only JSONL files in $HLQUANTBOT_DATA_DIR/trade_logs/
and (once Phase 1 lands) sidecar JSON files under .../trade_logs/forecasts/.
This module reads those files with a small mtime-based cache so HTMX polling
every 10 seconds does not re-parse the whole history on each request.

All new TradeRecord fields (trade_id, predicted_*, real_*_curve, exit_reason_v2,
expiry_at, k_candles, candle_interval_sec) are treated as Optional so legacy
records continue to load without errors.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _resolve_log_dir() -> Path:
    env_dir = os.environ.get("HLQUANTBOT_DATA_DIR")
    if env_dir:
        return Path(env_dir) / "trade_logs"
    # Local dev fallback: crypto_bot/data/trade_logs
    here = Path(__file__).resolve().parent.parent
    return here / "data" / "trade_logs"


class TradeStore:
    """Mtime-cached reader for outcomes_*.jsonl + per-trade sidecars."""

    def __init__(self, log_dir: Optional[Path] = None) -> None:
        self.log_dir: Path = Path(log_dir) if log_dir else _resolve_log_dir()
        self.forecasts_dir: Path = self.log_dir / "forecasts"
        self._lock = threading.Lock()
        self._cache_signature: tuple[tuple[str, float, int], ...] = ()
        self._cache_records: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ utils
    def _outcome_files(self) -> list[Path]:
        if not self.log_dir.exists():
            return []
        return sorted(self.log_dir.glob("outcomes_*.jsonl"))

    def _signature(self, files: list[Path]) -> tuple[tuple[str, float, int], ...]:
        sig: list[tuple[str, float, int]] = []
        for f in files:
            try:
                st = f.stat()
                sig.append((f.name, st.st_mtime, st.st_size))
            except OSError:
                continue
        return tuple(sig)

    def _load_all(self) -> list[dict[str, Any]]:
        files = self._outcome_files()
        sig = self._signature(files)
        with self._lock:
            if sig == self._cache_signature and self._cache_records:
                return self._cache_records
            records: list[dict[str, Any]] = []
            for f in files:
                try:
                    with open(f, "r") as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                records.append(json.loads(line))
                            except json.JSONDecodeError:
                                logger.warning("skip malformed line in %s", f.name)
                except OSError:
                    logger.exception("failed to read %s", f)
            # Most recent first
            records.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
            self._cache_signature = sig
            self._cache_records = records
            return records

    # --------------------------------------------------------------- queries
    def list_trades(
        self,
        limit: int = 50,
        offset: int = 0,
        symbol: Optional[str] = None,
        sort_by: str = "timestamp",
    ) -> tuple[list[dict[str, Any]], int]:
        records = self._load_all()
        if symbol:
            records = [r for r in records if r.get("symbol") == symbol]
        if sort_by and sort_by != "timestamp":
            records = sorted(records, key=lambda r: r.get(sort_by) or 0, reverse=True)
        total = len(records)
        return records[offset : offset + limit], total

    def get_trade(self, trade_id: str) -> Optional[dict[str, Any]]:
        for r in self._load_all():
            if r.get("trade_id") == trade_id:
                return r
        return None

    def get_sidecar(self, trade_id: str) -> Optional[dict[str, Any]]:
        if not trade_id:
            return None
        path = self.forecasts_dir / f"{trade_id}.json"
        if not path.exists():
            return None
        try:
            with open(path, "r") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            logger.exception("failed to read sidecar %s", path)
            return None

    def get_summary(self, lookback: int = 100) -> dict[str, Any]:
        records = self._load_all()[:lookback]
        n = len(records)
        if n == 0:
            return {
                "count": 0,
                "tp": 0, "sl": 0, "expiry": 0, "manual": 0,
                "tp_pct": 0.0, "sl_pct": 0.0, "expiry_pct": 0.0, "manual_pct": 0.0,
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "avg_pnl_usd": 0.0,
                "total_pnl_usd": 0.0,
                "wins": 0, "losses": 0,
            }
        buckets = {"tp": 0, "sl": 0, "expiry": 0, "manual": 0}
        wins = 0
        losses = 0
        gross_win = 0.0
        gross_loss = 0.0
        total = 0.0
        for r in records:
            reason = (r.get("exit_reason_v2") or r.get("exit_reason") or "manual").lower()
            mapped = self._map_reason(reason)
            buckets[mapped] = buckets.get(mapped, 0) + 1
            pnl = float(r.get("pnl_usd") or 0.0)
            total += pnl
            if pnl > 0:
                wins += 1
                gross_win += pnl
            elif pnl < 0:
                losses += 1
                gross_loss += -pnl
        pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
        return {
            "count": n,
            **buckets,
            "tp_pct": 100.0 * buckets["tp"] / n,
            "sl_pct": 100.0 * buckets["sl"] / n,
            "expiry_pct": 100.0 * buckets["expiry"] / n,
            "manual_pct": 100.0 * buckets["manual"] / n,
            "win_rate": 100.0 * wins / n,
            "profit_factor": pf if pf != float("inf") else 999.0,
            "avg_pnl_usd": total / n,
            "total_pnl_usd": total,
            "wins": wins,
            "losses": losses,
        }

    @staticmethod
    def _map_reason(reason: str) -> str:
        r = reason.lower()
        if r in ("tp", "take_profit"):
            return "tp"
        if r in ("sl", "stop_loss", "trailing_stop", "violation_exit"):
            return "sl"
        if r in ("expiry", "timeout", "regime_exit", "max_hold"):
            return "expiry"
        return "manual"
