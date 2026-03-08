"""PortfolioSimulator: multi-symbol concurrent position management.

Promoted from backtest_sizing.py with BacktestConfig instead of globals.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from backtesting.config import BacktestConfig

# Small fee offset added to breakeven SL to cover round-trip fees
_BREAKEVEN_FEE_OFFSET = 0.0015  # 0.15%


class PortfolioSimulator:
    """Simulates a portfolio with concurrent multi-symbol positions."""

    def __init__(self, cfg: BacktestConfig, label: str = "",
                 position_pct: float | None = None,
                 leverage: int | None = None,
                 max_positions: int | None = None,
                 leverage_caps: dict[str, int] | None = None):
        self.cfg = cfg
        self.label = label
        self._pct = position_pct if position_pct is not None else cfg.position_pct
        self._lev = leverage if leverage is not None else cfg.leverage
        self._max_pos = max_positions if max_positions is not None else cfg.max_positions
        self._leverage_caps = leverage_caps or {}
        self.equity = cfg.account_size
        self.open_positions: dict[str, dict] = {}
        self.trades: list[dict] = []
        self.daily_counts: dict[str, int] = {}
        self.equity_curve: list[float] = [cfg.account_size]

    def check_exits(self, symbol: str, candle: dict) -> None:
        """Check if open position on symbol hits TP, SL, breakeven, or momentum fade."""
        if symbol not in self.open_positions:
            return
        pos = self.open_positions[symbol]
        d = pos["direction"]
        exit_price = exit_reason = None

        slip = self.cfg.slippage_pct

        # --- 1. Hard TP/SL check (intra-bar using high/low) ---
        if d == 1:  # LONG -- exit = sell, slippage hurts (lower fill)
            if candle["l"] <= pos["sl"]:
                exit_price = pos["sl"] * (1 - slip)
                exit_reason = "SL"
            elif candle["h"] >= pos["tp"]:
                exit_price = pos["tp"] * (1 - slip)
                exit_reason = "TP"
        else:  # SHORT -- exit = buy to cover, slippage hurts (higher fill)
            if candle["h"] >= pos["sl"]:
                exit_price = pos["sl"] * (1 + slip)
                exit_reason = "SL"
            elif candle["l"] <= pos["tp"]:
                exit_price = pos["tp"] * (1 + slip)
                exit_reason = "TP"

        if exit_price is not None:
            self._close(symbol, exit_price, exit_reason, candle["t"])
            return

        # --- 2. Breakeven SL adjustment (evaluated on close) ---
        close = candle["c"]
        entry = pos["entry"]
        be_thresh = self.cfg.breakeven_threshold_pct

        if be_thresh > 0 and not pos.get("breakeven_hit", False):
            if d == 1:
                unrealized_pct = (close - entry) / entry
            else:
                unrealized_pct = (entry - close) / entry

            if unrealized_pct >= be_thresh:
                # Move SL to entry + small offset to cover fees
                if d == 1:
                    pos["sl"] = entry * (1 + _BREAKEVEN_FEE_OFFSET)
                else:
                    pos["sl"] = entry * (1 - _BREAKEVEN_FEE_OFFSET)
                pos["breakeven_hit"] = True

        # --- 3. Momentum fade exit (simplified: close vs prev_close) ---
        mom_min = self.cfg.momentum_exit_min_profit_pct
        prev_close = pos.get("prev_close")

        if mom_min > 0 and prev_close is not None:
            if d == 1:
                unrealized_pct = (close - entry) / entry
                fading = close < prev_close
            else:
                unrealized_pct = (entry - close) / entry
                fading = close > prev_close

            if unrealized_pct >= mom_min and fading:
                # Exit at close with slippage
                if d == 1:
                    exit_price = close * (1 - slip)
                else:
                    exit_price = close * (1 + slip)
                self._close(symbol, exit_price, "MOM_FADE", candle["t"])
                return

        # Update prev_close for next candle's momentum fade check
        pos["prev_close"] = close

    def try_open(self, symbol: str, direction: int, entry_price: float,
                 bar_time: int) -> bool:
        """Try to open a position. Returns True if opened."""
        if symbol in self.open_positions:
            return False
        if len(self.open_positions) >= self._max_pos:
            return False
        day = datetime.fromtimestamp(bar_time / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        # max_daily_trades=0 means unlimited (matches live bot behavior)
        if self.cfg.max_daily_trades > 0 and self.daily_counts.get(day, 0) >= self.cfg.max_daily_trades:
            return False
        effective_lev = min(self._lev, self._leverage_caps.get(symbol, self._lev))
        notional = self.equity * self._pct * effective_lev
        if notional <= 0 or entry_price <= 0:
            return False

        # Adverse slippage on entry
        slip = self.cfg.slippage_pct
        if direction == 1:  # LONG -- fill higher
            entry_price *= (1 + slip)
        else:  # SHORT -- fill lower
            entry_price *= (1 - slip)

        tp_pct = self.cfg.tp_pct
        sl_pct = self.cfg.sl_pct
        if direction == 1:
            tp = entry_price * (1 + tp_pct)
            sl = entry_price * (1 - sl_pct)
        else:
            tp = entry_price * (1 - tp_pct)
            sl = entry_price * (1 + sl_pct)

        self.open_positions[symbol] = {
            "direction": direction, "entry": entry_price,
            "tp": tp, "sl": sl, "notional": notional, "t_entry": bar_time,
            "breakeven_hit": False, "prev_close": None,
        }
        self.daily_counts[day] = self.daily_counts.get(day, 0) + 1
        return True

    def force_close_all(self, all_candles: dict[str, list[dict]]) -> None:
        """Close all open positions at last candle close."""
        for sym in list(self.open_positions.keys()):
            if sym in all_candles and all_candles[sym]:
                last = all_candles[sym][-1]
                self._close(sym, last["c"], "CLOSE", last["t"])

    def _close(self, symbol: str, exit_price: float, reason: str,
               t_exit: int) -> None:
        pos = self.open_positions.pop(symbol)
        qty = pos["notional"] / pos["entry"]
        if pos["direction"] == 1:
            gross = (exit_price - pos["entry"]) * qty
        else:
            gross = (pos["entry"] - exit_price) * qty
        # Use per-position entry fee if set (execution friction), else config default
        entry_fee = pos.get("entry_fee_pct", self.cfg.entry_fee_pct)
        fees = pos["notional"] * (entry_fee + self.cfg.exit_fee_pct)
        net = gross - fees
        self.equity += net
        self.equity_curve.append(self.equity)
        self.trades.append({
            "symbol": symbol, "direction": pos["direction"],
            "entry": pos["entry"], "exit": exit_price,
            "notional": pos["notional"], "gross": gross, "fees": fees,
            "net": net, "reason": reason,
            "t_entry": pos["t_entry"], "t_exit": t_exit,
            "effective_leverage": min(self._lev, self._leverage_caps.get(symbol, self._lev)),
            "fill_type": pos.get("fill_type", "maker"),
            "intended_entry": pos.get("intended_entry", pos["entry"]),
            "slippage_cost": pos.get("slippage_cost", 0.0),
        })


# ---------------------------------------------------------------------------
# Replay helpers
# ---------------------------------------------------------------------------

_CORR_GROUPS: dict[str, list[str]] = {
    "btc_ecosystem": ["BTC", "STX", "ORDI", "RUNE"],
    "eth_ecosystem": ["ETH", "ARB", "OP", "STRK", "ZK", "SCROLL", "BLAST"],
    "layer1": ["SOL", "AVAX", "SUI", "APT", "SEI", "TIA", "NEAR", "ADA", "DOT"],
    "defi": ["UNI", "AAVE", "CRV", "SNX", "DYDX", "GMX", "PENDLE", "JUP"],
    "meme": ["DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI", "MEME"],
    "ai": ["FET", "TAO", "ARKM", "WLD", "NEAR"],
}


def _bar_duration_minutes(timeframe: str) -> int:
    """Return bar duration in minutes for a given timeframe string."""
    return {"5m": 5, "15m": 15, "1h": 60, "4h": 240}.get(timeframe, 15)


def _find_roi_target(minimal_roi: dict[str, float], time_in_min: float) -> float | None:
    """Find the active ROI target for the given elapsed time.

    ``minimal_roi`` maps minute thresholds (as strings) to ROI fractions.
    Returns the target ROI for the largest threshold <= *time_in_min*, or
    ``None`` if no threshold applies.
    """
    if not minimal_roi:
        return None
    target: float | None = None
    for k in sorted(minimal_roi, key=lambda x: int(x)):
        if time_in_min >= int(k):
            target = minimal_roi[k]
        else:
            break
    return target


# ---------------------------------------------------------------------------
# ReplaySimulator — high-fidelity simulation matching the live bot
# ---------------------------------------------------------------------------

class ReplaySimulator(PortfolioSimulator):
    """Extended simulator that mirrors the live bot's full exit pipeline.

    Adds:
    - Trailing stop (ATR-based, activates after breakeven)
    - Graduated ROI exits (time-based from minimal_roi config)
    - Momentum fade via RSI slope (not simple close<prev_close)
    - Regime-change exit with grace period
    - Per-symbol cooldown (10 min standard, 30 min after SL)
    - Daily per-symbol trade cap
    - Correlation filter (max 1 per sector group)
    - Optional Kelly sizing
    """

    def __init__(
        self,
        cfg: BacktestConfig,
        label: str = "",
        position_pct: float | None = None,
        leverage: int | None = None,
        max_positions: int | None = None,
        leverage_caps: dict[str, int] | None = None,
        timeframe: str = "15m",
        use_kelly: bool = False,
    ) -> None:
        super().__init__(
            cfg, label=label, position_pct=position_pct,
            leverage=leverage, max_positions=max_positions,
            leverage_caps=leverage_caps,
        )
        self._timeframe = timeframe
        self._bar_min = _bar_duration_minutes(timeframe)
        self._use_kelly = use_kelly

        # Per-symbol cooldown tracking: symbol -> earliest allowed entry (ms)
        self._cooldown_until: dict[str, int] = {}
        # Per-symbol per-day trade counts: "SYMBOL:YYYY-MM-DD" -> count
        self._symbol_day_counts: dict[str, int] = {}
        # Exit reason counters
        self.exit_reasons: dict[str, int] = {}
        # Execution friction tracking
        self.skipped_entries: int = 0
        self.maker_fills: int = 0
        self.taker_fallbacks: int = 0

    # ------------------------------------------------------------------
    # check_exits — full 7-stage exit pipeline
    # ------------------------------------------------------------------
    def check_exits(  # type: ignore[override]
        self,
        symbol: str,
        candle: dict,
        current_regime: bool = True,
        current_rsi_slope: float = 0.0,
    ) -> None:
        """Full exit pipeline matching the live bot.

        Exit order (first match wins):
        1. SL  (intra-bar, candle low/high)
        2. TP  (intra-bar, candle high/low)
        3. Breakeven  (at close, moves SL to entry+0.15%)
        4. Trailing stop  (after breakeven, ATR-based)
        5. ROI graduated  (time-based)
        6. Momentum fade  (RSI slope)
        7. Regime change  (with grace period)
        """
        if symbol not in self.open_positions:
            return

        pos = self.open_positions[symbol]
        d = pos["direction"]
        close = candle["c"]
        slip = self.cfg.slippage_pct

        # Increment bar count
        pos["bar_count"] = pos.get("bar_count", 0) + 1

        # --- 1. Hard SL (intra-bar) ---
        if d == 1:
            if candle["l"] <= pos["sl"]:
                exit_price = pos["sl"] * (1 - slip)
                self._close_replay(symbol, exit_price, "SL", candle["t"])
                return
        else:
            if candle["h"] >= pos["sl"]:
                exit_price = pos["sl"] * (1 + slip)
                self._close_replay(symbol, exit_price, "SL", candle["t"])
                return

        # --- 2. Hard TP (intra-bar) ---
        if d == 1:
            if candle["h"] >= pos["tp"]:
                exit_price = pos["tp"] * (1 - slip)
                self._close_replay(symbol, exit_price, "TP", candle["t"])
                return
        else:
            if candle["l"] <= pos["tp"]:
                exit_price = pos["tp"] * (1 + slip)
                self._close_replay(symbol, exit_price, "TP", candle["t"])
                return

        # --- Unrealised P&L at close ---
        entry = pos["entry"]
        if d == 1:
            unrealized_pct = (close - entry) / entry
        else:
            unrealized_pct = (entry - close) / entry

        # --- 3. Breakeven (moves SL to entry + fee offset) ---
        be_thresh = self.cfg.breakeven_threshold_pct
        if be_thresh > 0 and not pos.get("breakeven_hit", False):
            if unrealized_pct >= be_thresh:
                if d == 1:
                    pos["sl"] = entry * (1 + _BREAKEVEN_FEE_OFFSET)
                else:
                    pos["sl"] = entry * (1 - _BREAKEVEN_FEE_OFFSET)
                pos["breakeven_hit"] = True
                pos["trailing_active"] = True
                pos["peak_price"] = close

        # --- 4. Trailing stop (ATR-based, after breakeven) ---
        if pos.get("trailing_active", False):
            peak = pos.get("peak_price", close)
            atr_pct = pos.get("entry_atr_pct", 1.0) / 100.0
            trail_dist = atr_pct * self.cfg.trailing_atr_mult

            if d == 1:
                if close > peak:
                    pos["peak_price"] = close
                    peak = close
                trail_sl = peak * (1 - trail_dist)
                if trail_sl > pos["sl"]:
                    pos["sl"] = trail_sl
                if close <= pos["sl"]:
                    exit_price = pos["sl"] * (1 - slip)
                    self._close_replay(symbol, exit_price, "TRAIL", candle["t"])
                    return
            else:
                if close < peak:
                    pos["peak_price"] = close
                    peak = close
                trail_sl = peak * (1 + trail_dist)
                if trail_sl < pos["sl"]:
                    pos["sl"] = trail_sl
                if close >= pos["sl"]:
                    exit_price = pos["sl"] * (1 + slip)
                    self._close_replay(symbol, exit_price, "TRAIL", candle["t"])
                    return

        # --- 5. Graduated ROI exit (time-based) ---
        elapsed_min = (candle["t"] - pos["t_entry"]) / 60_000
        roi_target = _find_roi_target(self.cfg.minimal_roi, elapsed_min)
        if roi_target is not None and unrealized_pct >= roi_target:
            if d == 1:
                exit_price = close * (1 - slip)
            else:
                exit_price = close * (1 + slip)
            self._close_replay(symbol, exit_price, "ROI", candle["t"])
            return

        # --- 6. Momentum fade (RSI slope check) ---
        min_age = self.cfg.momentum_exit_min_age_bars
        mom_min = self.cfg.momentum_exit_min_profit_pct
        if (pos.get("bar_count", 0) >= min_age
                and mom_min > 0
                and unrealized_pct >= mom_min):
            rsi_thresh = self.cfg.momentum_rsi_slope_threshold
            if d == 1 and current_rsi_slope < rsi_thresh:
                exit_price = close * (1 - slip)
                self._close_replay(symbol, exit_price, "MOM_FADE", candle["t"])
                return
            elif d == -1 and current_rsi_slope > -rsi_thresh:
                exit_price = close * (1 + slip)
                self._close_replay(symbol, exit_price, "MOM_FADE", candle["t"])
                return

        # --- 7. Regime change exit (with grace period) ---
        entry_regime = pos.get("entry_regime", True)
        grace = self.cfg.regime_exit_grace_bars
        if (current_regime != entry_regime
                and pos.get("bar_count", 0) >= grace):
            if d == 1:
                exit_price = close * (1 - slip)
            else:
                exit_price = close * (1 + slip)
            self._close_replay(symbol, exit_price, "REGIME", candle["t"])
            return

        # --- 8. Max hold time (dead trade cleanup) ---
        # Matches live bot: close if held > N hours AND abs(pnl%) < 0.3%
        max_hold_h = self.cfg.max_hold_hours
        if max_hold_h > 0:
            hold_minutes = (candle["t"] - pos["t_entry"]) / 60_000
            if hold_minutes >= max_hold_h * 60:
                if abs(unrealized_pct) < self.cfg.max_hold_dead_pnl_pct:
                    if d == 1:
                        exit_price = close * (1 - slip)
                    else:
                        exit_price = close * (1 + slip)
                    self._close_replay(symbol, exit_price, "MAX_HOLD", candle["t"])
                    return

    # ------------------------------------------------------------------
    # try_open — enriched entry with cooldowns, daily cap, correlation
    # ------------------------------------------------------------------
    def try_open(  # type: ignore[override]
        self,
        symbol: str,
        direction: int,
        entry_price: float,
        bar_time: int,
        ml_proba: float = 1.0,
        entry_regime: bool = True,
        _entry_rsi_slope: float = 0.0,
        entry_atr_pct: float = 1.0,
    ) -> bool:
        """Try to open a position with full replay checks.

        Additional checks vs PortfolioSimulator.try_open():
        - Per-symbol cooldown (10 min, 30 min after SL)
        - Daily per-symbol cap (max_trades_per_symbol_per_day)
        - Correlation filter (max 1 position per sector group)
        - Optional Kelly sizing
        """
        if symbol in self.open_positions:
            return False
        if len(self.open_positions) >= self._max_pos:
            return False

        # Global daily trade limit
        day = datetime.fromtimestamp(bar_time / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if (self.cfg.max_daily_trades > 0
                and self.daily_counts.get(day, 0) >= self.cfg.max_daily_trades):
            return False

        # Per-symbol cooldown
        if bar_time < self._cooldown_until.get(symbol, 0):
            return False

        # Per-symbol per-day cap
        sym_day_key = f"{symbol}:{day}"
        if self._symbol_day_counts.get(sym_day_key, 0) >= self.cfg.max_trades_per_symbol_per_day:
            return False

        # Correlation filter: max 1 open position per sector group
        for _, members in _CORR_GROUPS.items():
            if symbol in members:
                for open_sym in self.open_positions:
                    if open_sym in members:
                        return False  # Already have a position in this group

        # Position sizing
        effective_lev = min(self._lev, self._leverage_caps.get(symbol, self._lev))

        if self._use_kelly and ml_proba > 0.5:
            b = self.cfg.tp_pct / self.cfg.sl_pct if self.cfg.sl_pct > 0 else 2.0
            kelly_f = (ml_proba * (b + 1) - 1) / b
            kelly_f = max(0.01, min(kelly_f, self._pct))
            notional = self.equity * kelly_f * effective_lev
        else:
            notional = self.equity * self._pct * effective_lev

        if notional <= 0 or entry_price <= 0:
            return False

        intended_entry = entry_price

        # --- Execution friction: simulate maker fill probability ---
        fill_rate = self.cfg.maker_fill_rate
        if fill_rate < 1.0:
            # Deterministic pseudo-random based on trade identity
            h = hashlib.md5(f"{symbol}:{bar_time}:{direction}".encode()).digest()
            roll = int.from_bytes(h[:4], "big") / 0xFFFFFFFF
            maker_filled = roll < fill_rate
        else:
            maker_filled = True

        if maker_filled:
            # Maker fill: lower fee, normal slippage
            slip = self.cfg.slippage_pct
            entry_fee_pct = self.cfg.maker_entry_fee_pct
            fill_type = "maker"
            self.maker_fills += 1
        else:
            # Maker failed
            if self.cfg.maker_fail_action == "skip":
                self.skipped_entries += 1
                return False
            else:
                # Taker fallback: higher fee + extra slippage
                slip = self.cfg.slippage_pct + self.cfg.taker_extra_slippage_pct
                entry_fee_pct = self.cfg.taker_entry_fee_pct
                fill_type = "taker"
                self.taker_fallbacks += 1

        if direction == 1:
            entry_price *= (1 + slip)
        else:
            entry_price *= (1 - slip)

        slippage_cost = abs(entry_price - intended_entry) * (notional / entry_price)

        tp_pct = self.cfg.tp_pct
        sl_pct = self.cfg.sl_pct
        if direction == 1:
            tp = entry_price * (1 + tp_pct)
            sl = entry_price * (1 - sl_pct)
        else:
            tp = entry_price * (1 - tp_pct)
            sl = entry_price * (1 + sl_pct)

        self.open_positions[symbol] = {
            "direction": direction,
            "entry": entry_price,
            "tp": tp,
            "sl": sl,
            "notional": notional,
            "t_entry": bar_time,
            "breakeven_hit": False,
            "trailing_active": False,
            "peak_price": entry_price,
            "prev_close": None,
            "bar_count": 0,
            "entry_regime": entry_regime,
            "entry_atr_pct": entry_atr_pct,
            "ml_proba": ml_proba,
            "entry_fee_pct": entry_fee_pct,
            "fill_type": fill_type,
            "intended_entry": intended_entry,
            "slippage_cost": slippage_cost,
        }

        self.daily_counts[day] = self.daily_counts.get(day, 0) + 1
        self._symbol_day_counts[sym_day_key] = (
            self._symbol_day_counts.get(sym_day_key, 0) + 1
        )
        return True

    # ------------------------------------------------------------------
    # _close_replay — wraps _close and updates cooldown tracking
    # ------------------------------------------------------------------
    def _close_replay(self, symbol: str, exit_price: float, reason: str,
                      t_exit: int) -> None:
        """Close position, record exit reason, and set cooldown on SL."""
        self.exit_reasons[reason] = self.exit_reasons.get(reason, 0) + 1

        # Determine cooldown BEFORE _close pops the position
        if reason == "SL":
            cooldown_ms = self.cfg.cooldown_after_sl_minutes * 60_000
        else:
            cooldown_ms = self.cfg.cooldown_minutes * 60_000
        self._cooldown_until[symbol] = t_exit + cooldown_ms

        self._close(symbol, exit_price, reason, t_exit)
