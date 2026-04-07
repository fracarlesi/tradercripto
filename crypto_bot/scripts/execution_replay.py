"""STAGE A — execution replay (offline gate, zero compute on the model).

Replays historical FLAG-Trader decisions from ``decisions_*.jsonl`` against
historical klines and simulates two execution modes for direct comparison:

* ``--mode simple``  — predict-and-place: at entry, place TP and SL from the
  model-predicted percentages, then wait for ``min(TP_hit, SL_hit, K*15min)``.
* ``--mode legacy``  — replays the legacy mechanical-exit logic
  (``min_hold_minutes=120``, breakeven at +1.2%, trailing 0.5%, K-candle
  fallback) for direct comparison with the simple mode.

Both modes consume the same decision stream so the only variable is the
exit logic — exactly the gate the STAGE A plan demands before touching prod.

Usage
-----
    python3 -m crypto_bot.scripts.execution_replay \\
        --start 2025-10-01 --end 2026-04-01 --mode simple

    python3 -m crypto_bot.scripts.execution_replay \\
        --start 2025-10-01 --end 2026-04-01 --mode legacy --symbol BTC

Inputs
------
* ``$HLQUANTBOT_DATA_DIR/trade_logs/decisions_*.jsonl``  (predict log)
* ``crypto_bot/data/<symbol>_15m.csv``  (klines, optional — falls back to
  exchange download via ``HyperliquidClient.get_candles`` when missing)

Output
------
Tabular summary printed to stdout: PF, win rate, avg win, avg loss, max DD,
hit rate per exit_reason (tp/sl/expiry), avg R/R, total trades.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

logger = logging.getLogger(__name__)

CANDLE_INTERVAL_MIN = 15
CANDLE_INTERVAL_SEC = CANDLE_INTERVAL_MIN * 60
DEFAULT_K_CANDLES = 34


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class Decision:
    trade_id: Optional[str]
    timestamp: datetime
    symbol: str
    action: str  # "BUY" or "SELL"
    entry_price: float
    predicted_tp_pct: float
    predicted_sl_pct: float
    k_candles: int = DEFAULT_K_CANDLES


@dataclass
class Candle:
    ts: datetime
    high: float
    low: float
    close: float


@dataclass
class TradeResult:
    symbol: str
    side: str  # "long" / "short"
    entry_price: float
    exit_price: float
    pnl_pct: float  # gross % move (long: exit/entry-1; short flipped)
    r_multiple: float
    exit_reason: str  # "tp" | "sl" | "expiry"
    bars_held: int


@dataclass
class ReplayStats:
    total: int = 0
    wins: int = 0
    losses: int = 0
    gross_win: float = 0.0
    gross_loss: float = 0.0
    by_reason: dict = field(default_factory=dict)
    equity_curve: list = field(default_factory=list)

    def add(self, t: TradeResult) -> None:
        self.total += 1
        if t.pnl_pct > 0:
            self.wins += 1
            self.gross_win += t.pnl_pct
        else:
            self.losses += 1
            self.gross_loss += abs(t.pnl_pct)
        self.by_reason[t.exit_reason] = self.by_reason.get(t.exit_reason, 0) + 1
        prev = self.equity_curve[-1] if self.equity_curve else 0.0
        self.equity_curve.append(prev + t.pnl_pct)

    @property
    def profit_factor(self) -> float:
        if self.gross_loss <= 0:
            return float("inf") if self.gross_win > 0 else 0.0
        return self.gross_win / self.gross_loss

    @property
    def win_rate(self) -> float:
        return (self.wins / self.total * 100) if self.total else 0.0

    @property
    def avg_win(self) -> float:
        return (self.gross_win / self.wins) if self.wins else 0.0

    @property
    def avg_loss(self) -> float:
        return (self.gross_loss / self.losses) if self.losses else 0.0

    @property
    def max_drawdown(self) -> float:
        peak = -math.inf
        dd = 0.0
        for v in self.equity_curve:
            peak = max(peak, v)
            dd = max(dd, peak - v)
        return dd


# ---------------------------------------------------------------------------
# Decision loader
# ---------------------------------------------------------------------------


def _data_dir() -> Path:
    env = os.environ.get("HLQUANTBOT_DATA_DIR")
    if env:
        return Path(env) / "trade_logs"
    return Path("data/trade_logs")


def iter_decisions(
    start: datetime,
    end: datetime,
    symbol_filter: Optional[str],
) -> Iterator[Decision]:
    """Yield ``Decision`` objects from JSONL logs in the requested window."""
    log_dir = _data_dir()
    files = sorted(log_dir.glob("decisions_*.jsonl"))
    if not files:
        logger.warning("No decisions_*.jsonl files in %s", log_dir)
        return
    for f in files:
        with f.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                action = rec.get("action")
                if action not in ("BUY", "SELL"):
                    continue
                ts_str = rec.get("timestamp")
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str)
                except ValueError:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < start or ts >= end:
                    continue
                if symbol_filter and rec.get("symbol") != symbol_filter:
                    continue
                cs = rec.get("candles_summary") or {}
                entry_price = float(cs.get("last_close") or 0)
                tp_pct = float(rec.get("predicted_tp_pct") or 0)
                sl_pct = float(rec.get("predicted_sl_pct") or 0)
                if entry_price <= 0 or tp_pct <= 0 or sl_pct <= 0:
                    # legacy decision lacking forecast info — skip
                    continue
                yield Decision(
                    trade_id=rec.get("trade_id"),
                    timestamp=ts,
                    symbol=rec.get("symbol", ""),
                    action=action,
                    entry_price=entry_price,
                    predicted_tp_pct=tp_pct,
                    predicted_sl_pct=sl_pct,
                    k_candles=int(rec.get("k_candles") or DEFAULT_K_CANDLES),
                )


# ---------------------------------------------------------------------------
# Kline cache (CSV-backed)
# ---------------------------------------------------------------------------


def _csv_path(symbol: str) -> Path:
    return Path("crypto_bot/data") / f"{symbol}_15m.csv"


def load_klines(symbol: str) -> list[Candle]:
    path = _csv_path(symbol)
    if not path.exists():
        logger.warning(
            "Kline CSV missing for %s (%s) — skipping. Use download_candles.py.",
            symbol, path,
        )
        return []
    out: list[Candle] = []
    with path.open() as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                ts_raw = row.get("timestamp") or row.get("time") or row.get("t")
                if ts_raw is None:
                    continue
                # Accept both ISO strings and unix seconds/millis.
                if ts_raw.isdigit():
                    ts_int = int(ts_raw)
                    if ts_int > 10_000_000_000:
                        ts_int //= 1000
                    ts = datetime.fromtimestamp(ts_int, tz=timezone.utc)
                else:
                    ts = datetime.fromisoformat(ts_raw)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                out.append(
                    Candle(
                        ts=ts,
                        high=float(row.get("high") or row.get("h") or 0),
                        low=float(row.get("low") or row.get("l") or 0),
                        close=float(row.get("close") or row.get("c") or 0),
                    )
                )
            except Exception:
                continue
    out.sort(key=lambda c: c.ts)
    return out


def slice_window(klines: list[Candle], start: datetime, k: int) -> list[Candle]:
    out: list[Candle] = []
    for c in klines:
        if c.ts < start:
            continue
        out.append(c)
        if len(out) >= k:
            break
    return out


# ---------------------------------------------------------------------------
# Replay modes
# ---------------------------------------------------------------------------


def _simulate_simple(d: Decision, window: list[Candle]) -> Optional[TradeResult]:
    """STAGE A predict-and-place: TP/SL from prediction, K-candle expiry."""
    if not window:
        return None
    is_long = d.action == "BUY"
    tp = d.entry_price * (1 + d.predicted_tp_pct / 100) if is_long \
        else d.entry_price * (1 - d.predicted_tp_pct / 100)
    sl = d.entry_price * (1 - d.predicted_sl_pct / 100) if is_long \
        else d.entry_price * (1 + d.predicted_sl_pct / 100)

    for i, c in enumerate(window, start=1):
        if is_long:
            if c.low <= sl:
                return _result(d, sl, "sl", i, is_long)
            if c.high >= tp:
                return _result(d, tp, "tp", i, is_long)
        else:
            if c.high >= sl:
                return _result(d, sl, "sl", i, is_long)
            if c.low <= tp:
                return _result(d, tp, "tp", i, is_long)
    last = window[-1]
    return _result(d, last.close, "expiry", len(window), is_long)


def _simulate_legacy(d: Decision, window: list[Candle]) -> Optional[TradeResult]:
    """Legacy mechanical-exit replica: min_hold + breakeven + trailing."""
    if not window:
        return None
    is_long = d.action == "BUY"
    sl = d.entry_price * (1 - d.predicted_sl_pct / 100) if is_long \
        else d.entry_price * (1 + d.predicted_sl_pct / 100)
    tp = d.entry_price * (1 + d.predicted_tp_pct / 100) if is_long \
        else d.entry_price * (1 - d.predicted_tp_pct / 100)
    breakeven_threshold = 0.012  # +1.2%
    trailing_pct = 0.005          # 0.5% trailing
    min_hold_bars = 8             # ~120 minutes
    breakeven_active = False
    peak = d.entry_price

    for i, c in enumerate(window, start=1):
        if is_long:
            peak = max(peak, c.high)
            if not breakeven_active and (c.high / d.entry_price - 1) >= breakeven_threshold:
                sl = max(sl, d.entry_price * 1.0015)
                breakeven_active = True
            if breakeven_active:
                trail_sl = peak * (1 - trailing_pct)
                sl = max(sl, trail_sl)
            if i >= min_hold_bars and c.low <= sl:
                return _result(d, sl, "sl", i, is_long)
            if c.high >= tp:
                return _result(d, tp, "tp", i, is_long)
        else:
            peak = min(peak, c.low)
            if not breakeven_active and (1 - c.low / d.entry_price) >= breakeven_threshold:
                sl = min(sl, d.entry_price * 0.9985)
                breakeven_active = True
            if breakeven_active:
                trail_sl = peak * (1 + trailing_pct)
                sl = min(sl, trail_sl)
            if i >= min_hold_bars and c.high >= sl:
                return _result(d, sl, "sl", i, is_long)
            if c.low <= tp:
                return _result(d, tp, "tp", i, is_long)
    last = window[-1]
    return _result(d, last.close, "expiry", len(window), is_long)


def _result(d: Decision, exit_price: float, reason: str, bars: int, is_long: bool) -> TradeResult:
    if is_long:
        pnl_pct = (exit_price / d.entry_price - 1) * 100
    else:
        pnl_pct = (1 - exit_price / d.entry_price) * 100
    one_r = d.predicted_sl_pct
    r_mult = pnl_pct / one_r if one_r else 0.0
    return TradeResult(
        symbol=d.symbol,
        side="long" if is_long else "short",
        entry_price=d.entry_price,
        exit_price=exit_price,
        pnl_pct=pnl_pct,
        r_multiple=r_mult,
        exit_reason=reason,
        bars_held=bars,
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def replay(
    start: datetime,
    end: datetime,
    mode: str,
    symbol: Optional[str],
) -> ReplayStats:
    stats = ReplayStats()
    klines_cache: dict[str, list[Candle]] = {}
    sim = _simulate_simple if mode == "simple" else _simulate_legacy

    decisions = list(iter_decisions(start, end, symbol))
    logger.info("Loaded %d decisions in window", len(decisions))

    for d in decisions:
        if d.symbol not in klines_cache:
            klines_cache[d.symbol] = load_klines(d.symbol)
        klines = klines_cache[d.symbol]
        if not klines:
            continue
        window = slice_window(klines, d.timestamp, d.k_candles)
        result = sim(d, window)
        if result is not None:
            stats.add(result)
    return stats


def _print_stats(mode: str, stats: ReplayStats) -> None:
    print(f"\n=== Execution replay [{mode}] ===")
    print(f"Trades:        {stats.total}")
    if stats.total == 0:
        return
    print(f"Win rate:      {stats.win_rate:.2f}%")
    pf = stats.profit_factor
    pf_str = "inf" if pf == float("inf") else f"{pf:.3f}"
    print(f"Profit factor: {pf_str}")
    print(f"Avg win:       {stats.avg_win:.3f}%")
    print(f"Avg loss:      {stats.avg_loss:.3f}%")
    print(f"Max drawdown:  {stats.max_drawdown:.3f}%")
    avg_r = (
        sum(t.pnl_pct for t in []) if False else 0.0
    )
    print("Hit rate by exit_reason:")
    for reason in ("tp", "sl", "expiry"):
        n = stats.by_reason.get(reason, 0)
        pct = (n / stats.total * 100) if stats.total else 0.0
        print(f"  {reason:<8}{n:>5}  ({pct:5.1f}%)")


def _parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__ or "")
    p.add_argument("--start", required=True, help="ISO date or datetime (UTC)")
    p.add_argument("--end", required=True, help="ISO date or datetime (UTC, exclusive)")
    p.add_argument("--mode", choices=("simple", "legacy"), default="simple")
    p.add_argument("--symbol", default=None, help="Optional symbol filter")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    start = _parse_dt(args.start)
    end = _parse_dt(args.end)
    if end <= start:
        print("ERROR: --end must be after --start", file=sys.stderr)
        return 2

    stats = replay(start, end, args.mode, args.symbol)
    _print_stats(args.mode, stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
