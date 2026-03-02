"""PortfolioSimulator: multi-symbol concurrent position management.

Promoted from backtest_sizing.py with BacktestConfig instead of globals.
"""

from __future__ import annotations

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
        fees = pos["notional"] * self.cfg.fee_pct * 2
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
        })
