"""PortfolioSimulator: multi-symbol concurrent position management.

Promoted from backtest_sizing.py with BacktestConfig instead of globals.
"""

from __future__ import annotations

from datetime import datetime, timezone

from backtesting.config import BacktestConfig


class PortfolioSimulator:
    """Simulates a portfolio with concurrent multi-symbol positions."""

    def __init__(self, cfg: BacktestConfig, label: str = "",
                 position_pct: float | None = None,
                 leverage: int | None = None,
                 max_positions: int | None = None):
        self.cfg = cfg
        self.label = label
        self._pct = position_pct if position_pct is not None else cfg.position_pct
        self._lev = leverage if leverage is not None else cfg.leverage
        self._max_pos = max_positions if max_positions is not None else cfg.max_positions
        self.equity = cfg.account_size
        self.open_positions: dict[str, dict] = {}
        self.trades: list[dict] = []
        self.daily_counts: dict[str, int] = {}
        self.equity_curve: list[float] = [cfg.account_size]

    def check_exits(self, symbol: str, candle: dict) -> None:
        """Check if open position on symbol hits TP or SL."""
        if symbol not in self.open_positions:
            return
        pos = self.open_positions[symbol]
        d = pos["direction"]
        exit_price = exit_reason = None

        if d == 1:  # LONG
            if candle["l"] <= pos["sl"]:
                exit_price, exit_reason = pos["sl"], "SL"
            elif candle["h"] >= pos["tp"]:
                exit_price, exit_reason = pos["tp"], "TP"
        else:  # SHORT
            if candle["h"] >= pos["sl"]:
                exit_price, exit_reason = pos["sl"], "SL"
            elif candle["l"] <= pos["tp"]:
                exit_price, exit_reason = pos["tp"], "TP"

        if exit_price is not None:
            self._close(symbol, exit_price, exit_reason, candle["t"])

    def try_open(self, symbol: str, direction: int, entry_price: float,
                 bar_time: int) -> bool:
        """Try to open a position. Returns True if opened."""
        if symbol in self.open_positions:
            return False
        if len(self.open_positions) >= self._max_pos:
            return False
        day = datetime.fromtimestamp(bar_time / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if self.daily_counts.get(day, 0) >= self.cfg.max_daily_trades:
            return False
        notional = self.equity * self._pct * self._lev
        if notional <= 0 or entry_price <= 0:
            return False

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
        })
