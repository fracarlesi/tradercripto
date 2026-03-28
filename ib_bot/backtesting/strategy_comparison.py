"""
Comprehensive MES Strategy Comparison Backtest
================================================
Fetches 90 days of 5-min RTH bars from IB, then tests 5 strategies:
  1. VWAP Mean Reversion (ADX<25)
  2. Bollinger Band Mean Reversion (ADX<25)
  3. RSI Mean Reversion
  4. VWAP Trend / Momentum (ADX>25)
  5. Composite (time-of-day + ADX adaptive)

All indicators computed incrementally using math module only.
MES = $5/point.
"""
from __future__ import annotations

import asyncio
import json
import math
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from ib_insync import IB, Future

ET = ZoneInfo("America/New_York")
POINT_VALUE = 5.0  # MES = $5 per point

# ── Holiday calendar ──────────────────────────────────────────────
US_HOLIDAYS = {
    date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
    date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4),
    date(2025, 9, 1), date(2025, 11, 27), date(2025, 12, 25),
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16),
    date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19),
    date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26),
    date(2026, 12, 25),
}


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in US_HOLIDAYS


def get_trading_days(start: date, end: date) -> List[date]:
    days = []
    cur = start
    while cur <= end:
        if is_trading_day(cur):
            days.append(cur)
        cur += timedelta(days=1)
    return days


def front_month_expiry(day: date) -> str:
    y, m = day.year, day.month
    quarters = [3, 6, 9, 12]
    for q in quarters:
        if m < q:
            return f"{y}{q:02d}"
        if m == q and day.day <= 14:
            return f"{y}{q:02d}"
    return f"{y + 1}03"


# ── Data fetching ─────────────────────────────────────────────────
CACHE_DIR = Path("/app/ib_bot/backtesting/cache_5min")


async def fetch_all_data(ib: IB, days: List[date]) -> Dict[str, List[dict]]:
    """Fetch 5-min RTH bars for MES, one day at a time, with caching."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    all_data: Dict[str, List[dict]] = {}

    for day in days:
        cache_file = CACHE_DIR / f"MES_5m_{day.isoformat()}.json"
        if cache_file.exists():
            with open(cache_file) as f:
                raw = json.load(f)
            bars = [
                {"dt": datetime.fromisoformat(b["dt"]),
                 "o": float(b["o"]), "h": float(b["h"]),
                 "l": float(b["l"]), "c": float(b["c"]),
                 "v": float(b["v"])}
                for b in raw
            ]
            print(f"  Cache hit: {day} ({len(bars)} bars)")
        else:
            expiry = front_month_expiry(day)
            contract = Future("MES", lastTradeDateOrContractMonth=expiry,
                              exchange="CME", currency="USD")
            qualified = await ib.qualifyContractsAsync(contract)
            if not qualified:
                print(f"  WARN: Cannot qualify MES for {day}, skipping")
                continue

            end_dt = datetime(day.year, day.month, day.day, 23, 59, 59)
            raw_bars = await ib.reqHistoricalDataAsync(
                qualified[0],
                endDateTime=end_dt,
                durationStr="1 D",
                barSizeSetting="5 mins",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=2,
            )
            if not raw_bars:
                print(f"  WARN: No bars for {day}")
                continue

            bars = []
            for bar in raw_bars:
                dt = bar.date
                if isinstance(dt, datetime):
                    dt_et = dt.astimezone(ET)
                else:
                    dt_et = datetime.combine(day, datetime.min.time(), tzinfo=ET)
                bars.append({
                    "dt": dt_et,
                    "o": float(bar.open), "h": float(bar.high),
                    "l": float(bar.low), "c": float(bar.close),
                    "v": float(bar.volume),
                })

            # Cache it
            serializable = [
                {"dt": b["dt"].isoformat(),
                 "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]}
                for b in bars
            ]
            with open(cache_file, "w") as f:
                json.dump(serializable, f)
            print(f"  Fetched: {day} ({len(bars)} bars)")
            await asyncio.sleep(1.0)  # IB pacing

        if bars:
            all_data[day.isoformat()] = bars

    return all_data


# ── Indicator helpers (pure math, incremental) ────────────────────

def compute_sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def compute_ema(prev_ema: Optional[float], value: float, period: int) -> float:
    k = 2.0 / (period + 1)
    if prev_ema is None:
        return value
    return value * k + prev_ema * (1 - k)


def compute_rsi(gains: List[float], losses: List[float], period: int) -> Optional[float]:
    if len(gains) < period:
        return None
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_atr(tr_values: List[float], period: int) -> Optional[float]:
    if len(tr_values) < period:
        return None
    return sum(tr_values[-period:]) / period


def compute_adx(plus_dm_list: List[float], minus_dm_list: List[float],
                tr_list: List[float], period: int) -> Optional[float]:
    """Simplified ADX using Wilder smoothing approximation."""
    if len(tr_list) < period * 2:
        return None
    # Average true range, +DM, -DM over period
    atr = sum(tr_list[-period:]) / period
    if atr == 0:
        return 0.0
    avg_pdm = sum(plus_dm_list[-period:]) / period
    avg_mdm = sum(minus_dm_list[-period:]) / period
    plus_di = 100.0 * avg_pdm / atr
    minus_di = 100.0 * avg_mdm / atr
    di_sum = plus_di + minus_di
    if di_sum == 0:
        return 0.0
    dx = 100.0 * abs(plus_di - minus_di) / di_sum
    return dx


def compute_bb(closes: List[float], period: int, mult: float
               ) -> Optional[Tuple[float, float, float]]:
    """Returns (lower, middle, upper) Bollinger Bands."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    var = sum((x - mid) ** 2 for x in window) / period
    sd = math.sqrt(var)
    return (mid - mult * sd, mid, mid + mult * sd)


# ── Trade tracking ────────────────────────────────────────────────

class Trade:
    __slots__ = ("direction", "entry_price", "entry_time", "exit_price",
                 "exit_time", "pnl_points", "pnl_dollars")

    def __init__(self, direction: str, entry_price: float, entry_time: datetime):
        self.direction = direction
        self.entry_price = entry_price
        self.entry_time = entry_time
        self.exit_price = 0.0
        self.exit_time: Optional[datetime] = None
        self.pnl_points = 0.0
        self.pnl_dollars = 0.0

    def close(self, exit_price: float, exit_time: datetime) -> None:
        self.exit_price = exit_price
        self.exit_time = exit_time
        if self.direction == "LONG":
            self.pnl_points = exit_price - self.entry_price
        else:
            self.pnl_points = self.entry_price - exit_price
        self.pnl_dollars = self.pnl_points * POINT_VALUE


class StrategyResult:
    def __init__(self, name: str):
        self.name = name
        self.trades: List[Trade] = []

    def add(self, t: Trade) -> None:
        self.trades.append(t)

    def stats(self) -> Dict[str, Any]:
        if not self.trades:
            return {"name": self.name, "total": 0, "wins": 0, "losses": 0,
                    "win_rate": 0, "pf": 0, "total_pnl": 0, "max_dd": 0,
                    "avg_trades_day": 0, "best_day": 0, "worst_day": 0,
                    "sharpe": 0}
        wins = [t for t in self.trades if t.pnl_dollars > 0]
        losses = [t for t in self.trades if t.pnl_dollars <= 0]
        gross_profit = sum(t.pnl_dollars for t in wins)
        gross_loss = abs(sum(t.pnl_dollars for t in losses))
        total_pnl = sum(t.pnl_dollars for t in self.trades)
        pf = gross_profit / gross_loss if gross_loss > 0 else (
            999.0 if gross_profit > 0 else 0.0)

        # Max drawdown
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in self.trades:
            equity += t.pnl_dollars
            if equity > peak:
                peak = equity
            dd = peak - equity
            if dd > max_dd:
                max_dd = dd

        # Daily P&L
        daily_pnl: Dict[str, float] = defaultdict(float)
        for t in self.trades:
            day_key = t.entry_time.strftime("%Y-%m-%d")
            daily_pnl[day_key] += t.pnl_dollars

        daily_vals = list(daily_pnl.values())
        # Count unique trading days in data (not just days with trades)
        n_trading_days = max(len(daily_vals), 1)
        avg_trades_day = len(self.trades) / n_trading_days

        best_day = max(daily_vals) if daily_vals else 0
        worst_day = min(daily_vals) if daily_vals else 0

        # Sharpe (annualized from daily)
        if len(daily_vals) > 1:
            mean_d = sum(daily_vals) / len(daily_vals)
            var_d = sum((x - mean_d) ** 2 for x in daily_vals) / (len(daily_vals) - 1)
            std_d = math.sqrt(var_d) if var_d > 0 else 1e-9
            sharpe = (mean_d / std_d) * math.sqrt(252)
        else:
            sharpe = 0.0

        return {
            "name": self.name,
            "total": len(self.trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(self.trades) * 100,
            "pf": pf,
            "total_pnl": total_pnl,
            "max_dd": max_dd,
            "avg_trades_day": avg_trades_day,
            "best_day": best_day,
            "worst_day": worst_day,
            "sharpe": sharpe,
            "daily_pnl": daily_pnl,
        }


# ── Strategy simulation engine ───────────────────────────────────

def run_all_strategies(all_data: Dict[str, List[dict]]) -> List[StrategyResult]:
    """Run all 5 strategies on the same data set."""

    results = [
        StrategyResult("S1: VWAP MeanRev"),
        StrategyResult("S2: BB MeanRev"),
        StrategyResult("S3: RSI MeanRev"),
        StrategyResult("S4: VWAP Trend"),
        StrategyResult("S5: Composite"),
    ]

    sorted_days = sorted(all_data.keys())
    total_bars = 0

    # We process day by day — indicators reset daily for VWAP
    # but ADX/RSI/BB need history, so we maintain rolling buffers across days
    # Actually VWAP resets daily. Others use rolling windows.

    # Global rolling buffers for multi-day indicators
    all_closes: List[float] = []
    all_highs: List[float] = []
    all_lows: List[float] = []
    rsi_gains: List[float] = []
    rsi_losses: List[float] = []
    tr_list: List[float] = []
    plus_dm_list: List[float] = []
    minus_dm_list: List[float] = []
    prev_close: Optional[float] = None

    for day_key in sorted_days:
        bars = all_data[day_key]
        if not bars:
            continue

        # Daily VWAP accumulators
        vwap_cum_vol = 0.0
        vwap_cum_tp_vol = 0.0
        vwap_cum_tp2_vol = 0.0  # for SD calculation

        # Positions for each strategy (max 1 at a time)
        positions: List[Optional[Trade]] = [None] * 5
        daily_trade_count = [0] * 5  # for S5 max 8/day

        for bar in bars:
            dt = bar["dt"]
            t = dt.time()
            o, h, l, c, v = bar["o"], bar["h"], bar["l"], bar["c"], bar["v"]
            total_bars += 1

            # ── Update rolling indicators ──
            all_closes.append(c)
            all_highs.append(h)
            all_lows.append(l)

            # True Range
            if prev_close is not None:
                tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
            else:
                tr = h - l
            tr_list.append(tr)

            # +DM / -DM
            if len(all_highs) >= 2:
                up_move = all_highs[-1] - all_highs[-2]
                down_move = all_lows[-2] - all_lows[-1]
                pdm = up_move if (up_move > down_move and up_move > 0) else 0.0
                mdm = down_move if (down_move > up_move and down_move > 0) else 0.0
            else:
                pdm, mdm = 0.0, 0.0
            plus_dm_list.append(pdm)
            minus_dm_list.append(mdm)

            # RSI gains/losses
            if prev_close is not None:
                change = c - prev_close
                rsi_gains.append(max(change, 0.0))
                rsi_losses.append(max(-change, 0.0))

            prev_close = c

            # ── VWAP (daily reset) ──
            tp = (h + l + c) / 3.0
            if v > 0:
                vwap_cum_vol += v
                vwap_cum_tp_vol += tp * v
                vwap_cum_tp2_vol += (tp ** 2) * v

            vwap = vwap_cum_tp_vol / vwap_cum_vol if vwap_cum_vol > 0 else c
            vwap_var = max(0, vwap_cum_tp2_vol / vwap_cum_vol - vwap ** 2) if vwap_cum_vol > 0 else 0
            vwap_sd = math.sqrt(vwap_var) if vwap_var > 0 else 0.001

            # ── Other indicators ──
            adx = compute_adx(plus_dm_list, minus_dm_list, tr_list, 14)
            rsi = compute_rsi(rsi_gains, rsi_losses, 14)
            atr = compute_atr(tr_list, 14)
            bb = compute_bb(all_closes, 20, 2.0)

            if adx is None or rsi is None or atr is None or bb is None:
                continue  # Not enough history

            bb_lower, bb_mid, bb_upper = bb

            # ══════════════════════════════════════════════════════
            # STRATEGY 1: VWAP Mean Reversion (ADX<25, 10:00-15:30)
            # ══════════════════════════════════════════════════════
            s1_active = time(10, 0) <= t <= time(15, 30)
            if positions[0] is not None:
                pos = positions[0]
                # Exit: price returns to VWAP
                if pos.direction == "LONG" and c >= vwap:
                    pos.close(c, dt)
                    results[0].add(pos)
                    positions[0] = None
                elif pos.direction == "SHORT" and c <= vwap:
                    pos.close(c, dt)
                    results[0].add(pos)
                    positions[0] = None
                # Stop: 8 pts fixed
                elif pos.direction == "LONG" and c <= pos.entry_price - 8.0:
                    pos.close(pos.entry_price - 8.0, dt)
                    results[0].add(pos)
                    positions[0] = None
                elif pos.direction == "SHORT" and c >= pos.entry_price + 8.0:
                    pos.close(pos.entry_price + 8.0, dt)
                    results[0].add(pos)
                    positions[0] = None
            elif s1_active and adx < 25:
                # Entry LONG: close below VWAP - 2.0 SD
                if c < vwap - 2.0 * vwap_sd:
                    positions[0] = Trade("LONG", c, dt)
                # Entry SHORT: close above VWAP + 2.0 SD
                elif c > vwap + 2.0 * vwap_sd:
                    positions[0] = Trade("SHORT", c, dt)

            # ══════════════════════════════════════════════════════
            # STRATEGY 2: BB Mean Reversion (ADX<25, 10:00-15:30)
            # ══════════════════════════════════════════════════════
            s2_active = time(10, 0) <= t <= time(15, 30)
            if positions[1] is not None:
                pos = positions[1]
                # Exit: price returns to middle band (SMA 20)
                if pos.direction == "LONG" and c >= bb_mid:
                    pos.close(c, dt)
                    results[1].add(pos)
                    positions[1] = None
                elif pos.direction == "SHORT" and c <= bb_mid:
                    pos.close(c, dt)
                    results[1].add(pos)
                    positions[1] = None
                # Stop: 8 pts fixed
                elif pos.direction == "LONG" and c <= pos.entry_price - 8.0:
                    pos.close(pos.entry_price - 8.0, dt)
                    results[1].add(pos)
                    positions[1] = None
                elif pos.direction == "SHORT" and c >= pos.entry_price + 8.0:
                    pos.close(pos.entry_price + 8.0, dt)
                    results[1].add(pos)
                    positions[1] = None
            elif s2_active and adx < 25:
                if c < bb_lower:
                    positions[1] = Trade("LONG", c, dt)
                elif c > bb_upper:
                    positions[1] = Trade("SHORT", c, dt)

            # ══════════════════════════════════════════════════════
            # STRATEGY 3: RSI Mean Reversion (10:00-15:30)
            # ══════════════════════════════════════════════════════
            s3_active = time(10, 0) <= t <= time(15, 30)
            if positions[2] is not None:
                pos = positions[2]
                # Exit: RSI crosses back to 50
                if pos.direction == "LONG" and rsi >= 50:
                    pos.close(c, dt)
                    results[2].add(pos)
                    positions[2] = None
                elif pos.direction == "SHORT" and rsi <= 50:
                    pos.close(c, dt)
                    results[2].add(pos)
                    positions[2] = None
                # Stop: 6 pts fixed
                elif pos.direction == "LONG" and c <= pos.entry_price - 6.0:
                    pos.close(pos.entry_price - 6.0, dt)
                    results[2].add(pos)
                    positions[2] = None
                elif pos.direction == "SHORT" and c >= pos.entry_price + 6.0:
                    pos.close(pos.entry_price + 6.0, dt)
                    results[2].add(pos)
                    positions[2] = None
            elif s3_active:
                if rsi < 20:
                    positions[2] = Trade("LONG", c, dt)
                elif rsi > 80:
                    positions[2] = Trade("SHORT", c, dt)

            # ══════════════════════════════════════════════════════
            # STRATEGY 4: VWAP Trend / Momentum (ADX>25, 09:45-15:00)
            # ══════════════════════════════════════════════════════
            s4_active = time(9, 45) <= t <= time(15, 0)
            if positions[3] is not None:
                pos = positions[3]
                stop_dist = 1.5 * atr
                if stop_dist < 2.0:
                    stop_dist = 2.0
                target_dist = 2.0 * stop_dist  # 2:1 R:R
                if pos.direction == "LONG":
                    if c >= pos.entry_price + target_dist:
                        pos.close(pos.entry_price + target_dist, dt)
                        results[3].add(pos)
                        positions[3] = None
                    elif c <= pos.entry_price - stop_dist:
                        pos.close(pos.entry_price - stop_dist, dt)
                        results[3].add(pos)
                        positions[3] = None
                else:  # SHORT
                    if c <= pos.entry_price - target_dist:
                        pos.close(pos.entry_price - target_dist, dt)
                        results[3].add(pos)
                        positions[3] = None
                    elif c >= pos.entry_price + stop_dist:
                        pos.close(pos.entry_price + stop_dist, dt)
                        results[3].add(pos)
                        positions[3] = None
            elif s4_active and adx > 25:
                # LONG: price above VWAP, pullback within 0.5 SD of VWAP
                if c > vwap and (c - vwap) < 0.5 * vwap_sd:
                    positions[3] = Trade("LONG", c, dt)
                # SHORT: price below VWAP, rally within 0.5 SD of VWAP
                elif c < vwap and (vwap - c) < 0.5 * vwap_sd:
                    positions[3] = Trade("SHORT", c, dt)

            # ══════════════════════════════════════════════════════
            # STRATEGY 5: Composite (time-of-day + ADX adaptive)
            # ══════════════════════════════════════════════════════
            s5_max_trades = 8
            if positions[4] is not None:
                pos = positions[4]
                # Determine which sub-strategy is managing this position
                entry_t = pos.entry_time.time()
                is_momentum_pos = (
                    (time(9, 45) <= entry_t < time(10, 30)) or
                    (time(14, 0) <= entry_t <= time(15, 30))
                )
                if is_momentum_pos:
                    # Momentum exit logic (like S4)
                    stop_dist = 1.5 * atr
                    if stop_dist < 2.0:
                        stop_dist = 2.0
                    target_dist = 2.0 * stop_dist
                    if pos.direction == "LONG":
                        if c >= pos.entry_price + target_dist:
                            pos.close(pos.entry_price + target_dist, dt)
                            results[4].add(pos)
                            positions[4] = None
                        elif c <= pos.entry_price - stop_dist:
                            pos.close(pos.entry_price - stop_dist, dt)
                            results[4].add(pos)
                            positions[4] = None
                    else:
                        if c <= pos.entry_price - target_dist:
                            pos.close(pos.entry_price - target_dist, dt)
                            results[4].add(pos)
                            positions[4] = None
                        elif c >= pos.entry_price + stop_dist:
                            pos.close(pos.entry_price + stop_dist, dt)
                            results[4].add(pos)
                            positions[4] = None
                else:
                    # Mean reversion exit logic (like S1/S2)
                    if pos.direction == "LONG" and c >= vwap:
                        pos.close(c, dt)
                        results[4].add(pos)
                        positions[4] = None
                    elif pos.direction == "SHORT" and c <= vwap:
                        pos.close(c, dt)
                        results[4].add(pos)
                        positions[4] = None
                    elif pos.direction == "LONG" and c <= pos.entry_price - 8.0:
                        pos.close(pos.entry_price - 8.0, dt)
                        results[4].add(pos)
                        positions[4] = None
                    elif pos.direction == "SHORT" and c >= pos.entry_price + 8.0:
                        pos.close(pos.entry_price + 8.0, dt)
                        results[4].add(pos)
                        positions[4] = None

            elif daily_trade_count[4] < s5_max_trades:
                # Entry logic based on time of day + ADX
                if time(9, 45) <= t < time(10, 30):
                    # Early morning: momentum (S4 logic)
                    if adx > 25:
                        if c > vwap and (c - vwap) < 0.5 * vwap_sd:
                            positions[4] = Trade("LONG", c, dt)
                            daily_trade_count[4] += 1
                        elif c < vwap and (vwap - c) < 0.5 * vwap_sd:
                            positions[4] = Trade("SHORT", c, dt)
                            daily_trade_count[4] += 1
                elif time(10, 30) <= t < time(14, 0):
                    # Midday: ADX decides
                    if adx < 25:
                        # Mean reversion (S1 logic)
                        if c < vwap - 2.0 * vwap_sd:
                            positions[4] = Trade("LONG", c, dt)
                            daily_trade_count[4] += 1
                        elif c > vwap + 2.0 * vwap_sd:
                            positions[4] = Trade("SHORT", c, dt)
                            daily_trade_count[4] += 1
                    else:
                        # Momentum even midday if trending
                        if c > vwap and (c - vwap) < 0.5 * vwap_sd:
                            positions[4] = Trade("LONG", c, dt)
                            daily_trade_count[4] += 1
                        elif c < vwap and (vwap - c) < 0.5 * vwap_sd:
                            positions[4] = Trade("SHORT", c, dt)
                            daily_trade_count[4] += 1
                elif time(14, 0) <= t <= time(15, 30):
                    # Afternoon: momentum (S4 logic)
                    if adx > 25:
                        if c > vwap and (c - vwap) < 0.5 * vwap_sd:
                            positions[4] = Trade("LONG", c, dt)
                            daily_trade_count[4] += 1
                        elif c < vwap and (vwap - c) < 0.5 * vwap_sd:
                            positions[4] = Trade("SHORT", c, dt)
                            daily_trade_count[4] += 1

        # End of day: flatten all open positions at last bar close
        if bars:
            last_bar = bars[-1]
            last_dt = last_bar["dt"]
            last_c = last_bar["c"]
            for i in range(5):
                if positions[i] is not None:
                    positions[i].close(last_c, last_dt)
                    results[i].add(positions[i])
                    positions[i] = None

    print(f"\nTotal bars processed: {total_bars}")
    print(f"Total trading days: {len(sorted_days)}")
    return results


# ── Output formatting ─────────────────────────────────────────────

def print_comparison(results: List[StrategyResult]) -> None:
    stats_list = [r.stats() for r in results]

    # Sort by total P&L descending
    stats_list.sort(key=lambda s: s["total_pnl"], reverse=True)

    print("\n" + "=" * 120)
    print("  MES STRATEGY COMPARISON — 90 Days, 5-min RTH Bars, $5/point, 1 contract")
    print("=" * 120)

    header = (
        f"{'Rank':<5}"
        f"{'Strategy':<22}"
        f"{'Trades':>7}"
        f"{'Wins':>6}"
        f"{'Losses':>7}"
        f"{'WinRate':>8}"
        f"{'PF':>7}"
        f"{'Total P&L':>11}"
        f"{'MaxDD':>9}"
        f"{'Avg/Day':>8}"
        f"{'BestDay':>10}"
        f"{'WorstDay':>10}"
        f"{'Sharpe':>8}"
    )
    print(header)
    print("-" * 120)

    for rank, s in enumerate(stats_list, 1):
        line = (
            f"{rank:<5}"
            f"{s['name']:<22}"
            f"{s['total']:>7}"
            f"{s['wins']:>6}"
            f"{s['losses']:>7}"
            f"{s['win_rate']:>7.1f}%"
            f"{s['pf']:>7.2f}"
            f"${s['total_pnl']:>9.2f}"
            f"${s['max_dd']:>7.2f}"
            f"{s['avg_trades_day']:>8.1f}"
            f"${s['best_day']:>8.2f}"
            f"${s['worst_day']:>8.2f}"
            f"{s['sharpe']:>8.2f}"
        )
        print(line)

    print("=" * 120)

    # Monthly breakdown for Composite (S5)
    composite_stats = None
    for s in stats_list:
        if "Composite" in s["name"]:
            composite_stats = s
            break

    if composite_stats and composite_stats.get("daily_pnl"):
        print("\n" + "=" * 80)
        print("  S5: COMPOSITE — Monthly Breakdown")
        print("=" * 80)
        monthly: Dict[str, float] = defaultdict(float)
        monthly_trades: Dict[str, int] = defaultdict(int)
        daily_pnl = composite_stats["daily_pnl"]

        for day_str, pnl in daily_pnl.items():
            month_key = day_str[:7]  # YYYY-MM
            monthly[month_key] += pnl
            monthly_trades[month_key] += 1

        print(f"{'Month':<12}{'P&L':>12}{'TradingDays':>14}{'AvgDaily':>12}")
        print("-" * 50)
        for m in sorted(monthly.keys()):
            avg_d = monthly[m] / monthly_trades[m] if monthly_trades[m] > 0 else 0
            print(f"{m:<12}${monthly[m]:>10.2f}{monthly_trades[m]:>14}${avg_d:>10.2f}")
        print("-" * 50)
        print(f"{'TOTAL':<12}${sum(monthly.values()):>10.2f}"
              f"{sum(monthly_trades.values()):>14}"
              f"${sum(monthly.values()) / max(sum(monthly_trades.values()), 1):>10.2f}")
        print("=" * 80)

    # Individual strategy details
    for s in stats_list:
        print(f"\n--- {s['name']} ---")
        print(f"  Total Trades: {s['total']}")
        print(f"  Win Rate: {s['win_rate']:.1f}% ({s['wins']}W / {s['losses']}L)")
        print(f"  Profit Factor: {s['pf']:.2f}")
        print(f"  Total P&L: ${s['total_pnl']:.2f}")
        print(f"  Max Drawdown: ${s['max_dd']:.2f}")
        print(f"  Sharpe Ratio: {s['sharpe']:.2f}")
        if s['total_pnl'] > 0 and s['max_dd'] > 0:
            print(f"  Return/MaxDD: {s['total_pnl']/s['max_dd']:.2f}x")
        pnl_per_trade = s['total_pnl'] / s['total'] if s['total'] > 0 else 0
        print(f"  Avg P&L/Trade: ${pnl_per_trade:.2f}")
        # For $2K account
        if s['max_dd'] > 0:
            acct_dd_pct = s['max_dd'] / 2000 * 100
            print(f"  MaxDD as % of $2K acct: {acct_dd_pct:.1f}%")


# ── Main ──────────────────────────────────────────────────────────

async def main():
    print("=" * 60)
    print("  MES COMPREHENSIVE STRATEGY BACKTEST")
    print("  90 days, 5-min RTH bars, $2K account, 1 contract")
    print("=" * 60)

    # Calculate date range
    end_date = date.today()
    start_date = end_date - timedelta(days=90)
    trading_days = get_trading_days(start_date, end_date)
    print(f"\nDate range: {start_date} to {end_date}")
    print(f"Trading days: {len(trading_days)}")

    # Connect to IB
    print("\nConnecting to IB Gateway...")
    ib = IB()
    await ib.connectAsync("127.0.0.1", 4002, clientId=77, timeout=15)
    print(f"Connected: {ib.isConnected()}")

    try:
        print(f"\nFetching 5-min bars for MES ({len(trading_days)} days)...")
        all_data = await fetch_all_data(ib, trading_days)
        total_bars = sum(len(v) for v in all_data.values())
        print(f"\nData loaded: {len(all_data)} days, {total_bars} bars")

        if total_bars == 0:
            print("ERROR: No data fetched. Aborting.")
            return

        print("\nRunning all 5 strategies...")
        results = run_all_strategies(all_data)
        print_comparison(results)

    finally:
        ib.disconnect()
        print("\nDisconnected from IB.")


if __name__ == "__main__":
    asyncio.run(main())
