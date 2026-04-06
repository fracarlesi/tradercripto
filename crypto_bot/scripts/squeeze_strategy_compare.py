"""
Squeeze Strategy Comparison — EMA High vs FLAG-Trader LLM
==========================================================
Compares two directional strategies triggered on squeeze fire events:
  - Strategy A (EMA High Signal): fixed rules, LONG only
  - Strategy B (FLAG-Trader LLM): Qwen 0.5B model, LONG + SHORT

Usage:
    python -m crypto_bot.scripts.squeeze_strategy_compare \
        --symbols BTC ETH SOL \
        --checkpoint models/flag_trader_qwen/final_model.pt
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

# Ensure project root is on sys.path when run as module
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from crypto_bot.flag_trader.data_collector import HyperliquidDataCollector
from crypto_bot.flag_trader.market_context import compute_market_context
from crypto_bot.flag_trader.prompt import PromptBuilder
from crypto_bot.services.market_state import calculate_ema
from crypto_bot.services.squeeze_indicator import detect_squeeze_state

logger = logging.getLogger(__name__)

ACTION_NAMES = {0: "SELL", 1: "HOLD", 2: "BUY"}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SqueezeCompareConfig:
    symbols: list[str] = field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    data_dir: Path = Path("data/candles")
    candle_window: int = 50  # rolling window per squeeze
    bb_period: int = 20
    bb_std_mult: float = 2.0
    kc_ema_period: int = 20
    kc_atr_period: int = 14
    kc_atr_mult: float = 1.5
    squeeze_lookback: int = 3
    initial_capital: float = 100.0
    leverage: int = 3
    position_pct: float = 0.25
    maker_fee: float = 0.0002
    taker_fee: float = 0.0005
    # Strategy A
    ema_high_period: int = 4
    ema_trend_period: int = 21
    sma_trend_period: int = 50
    sma_rising_lookback: int = 5
    # Strategy B
    model_name: str = "Qwen/Qwen2.5-0.5B-Instruct"
    checkpoint: Path = Path("models/flag_trader_qwen/final_model.pt")
    device: str = "cpu"
    confidence_threshold: float = 0.6


@dataclass
class StrategyATrade:
    symbol: str
    entry_price: float
    entry_bar: int
    signal_low: float
    stop_price: float  # = signal_low
    exit_price: float = 0.0
    exit_bar: int = 0
    exit_reason: str = ""  # "stop", "max_hold_5", "end"
    pnl_pct: float = 0.0
    pnl_usd: float = 0.0


@dataclass
class StrategyBTrade:
    symbol: str
    direction: str  # "long" or "short"
    entry_price: float
    entry_bar: int
    tp_pct: float
    sl_pct: float
    tp_price: float
    sl_price: float
    exit_price: float = 0.0
    exit_bar: int = 0
    exit_reason: str = ""  # "tp", "sl", "max_hold", "end"
    pnl_pct: float = 0.0
    pnl_usd: float = 0.0


@dataclass
class StrategyStats:
    name: str
    direction_label: str
    total_trades: int = 0
    winners: int = 0
    win_rate: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_factor: float = 0.0
    expectancy_r: float = 0.0
    max_drawdown_pct: float = 0.0
    total_return_pct: float = 0.0
    avg_duration_bars: float = 0.0
    total_fires: int = 0


# ---------------------------------------------------------------------------
# Strategy A — EMA High Signal (pure function)
# ---------------------------------------------------------------------------

def compute_ema_high_signal(
    closes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    cfg: SqueezeCompareConfig,
) -> tuple[str | None, float, float]:
    """Compute EMA-High breakout signal (LONG only).

    Returns ("long", entry_close, signal_low) or (None, 0, 0).

    Signal LONG if:
    - close[-1] > EMA(highs, 4)[-1]        (breakout)
    - close[-2] < EMA(highs, 4)[-2]        (was below yesterday)
    - close[-3] < EMA(highs, 4)[-3]        (was below 2 days ago)
    - EMA(closes, 21)[-1] > SMA(closes, 50)[-1]  (trend filter)
    - SMA(closes, 50) rising for 5 consecutive bars

    Guard: if signal_low >= entry_close (doji), return None.
    """
    n = len(closes)
    min_required = max(cfg.sma_trend_period + cfg.sma_rising_lookback, cfg.ema_high_period + 3)
    if n < min_required:
        return None, 0.0, 0.0

    # EMA of highs (period 4) — full length array
    ema_high = calculate_ema(highs, cfg.ema_high_period)

    # Breakout: close crosses above EMA(highs)
    if not (closes[-1] > ema_high[-1]):
        return None, 0.0, 0.0
    if not (closes[-2] < ema_high[-2]):
        return None, 0.0, 0.0
    if not (closes[-3] < ema_high[-3]):
        return None, 0.0, 0.0

    # Trend filter: EMA(21) > SMA(50)
    ema_trend = calculate_ema(closes, cfg.ema_trend_period)
    # SMA via convolve — valid length is n - period + 1
    sma_trend = np.convolve(closes, np.ones(cfg.sma_trend_period) / cfg.sma_trend_period, mode="valid")

    if len(sma_trend) < cfg.sma_rising_lookback + 1:
        return None, 0.0, 0.0

    # EMA(21) at last bar vs SMA(50) at last bar
    # sma_trend[-1] corresponds to the last bar
    if not (ema_trend[-1] > sma_trend[-1]):
        return None, 0.0, 0.0

    # SMA(50) must be net rising over the lookback window
    # (adapted from daily Pine Script: on 15m candles, strict monotonic
    #  rising is too restrictive, so we check net direction instead)
    if not (sma_trend[-1] > sma_trend[-cfg.sma_rising_lookback - 1]):
        return None, 0.0, 0.0

    entry_close = float(closes[-1])
    signal_low = float(lows[-1])

    # Guard: doji or inverted bar
    if signal_low >= entry_close:
        return None, 0.0, 0.0

    return "long", entry_close, signal_low


# ---------------------------------------------------------------------------
# Data loading (reuses patterns from replay_flag_trader.py)
# ---------------------------------------------------------------------------

def df_to_candle_list(df: pd.DataFrame) -> list[dict]:
    """Convert a DataFrame to list of dicts."""
    records: list[dict] = []
    for _, row in df.iterrows():
        records.append({
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        })
    return records


def resample_to_daily(candles: list[dict], bars_per_day: int = 96) -> list[dict]:
    """Aggregate intraday candles into daily OHLCV bars."""
    daily: list[dict] = []
    for i in range(0, len(candles) - bars_per_day + 1, bars_per_day):
        chunk = candles[i : i + bars_per_day]
        daily.append({
            "open": chunk[0]["open"],
            "high": max(c["high"] for c in chunk),
            "low": min(c["low"] for c in chunk),
            "close": chunk[-1]["close"],
            "volume": sum(c["volume"] for c in chunk),
        })
    return daily


async def fetch_or_load_candles(
    assets: list[str],
    data_dir: Path,
    interval: str = "15m",
    resample: str | None = None,
) -> dict[str, list[dict]]:
    """Load candles from cache or fetch from API.

    If resample="1d", aggregates 15m candles into daily bars.
    """
    collector = HyperliquidDataCollector(data_dir=data_dir)
    result: dict[str, list[dict]] = {}

    bars_per_day_map = {"1m": 1440, "5m": 288, "15m": 96, "1h": 24, "4h": 6, "1d": 1}

    for symbol in assets:
        try:
            df = collector.load_candles(symbol, interval)
            logger.info("Loaded cached %s: %d candles", symbol, len(df))
        except FileNotFoundError:
            logger.info("Fetching %s from API (180 days)...", symbol)
            df = await collector.fetch_candles(symbol, interval, 180)

        if df.empty:
            logger.warning("No data for %s, skipping", symbol)
            continue

        candles = df_to_candle_list(df)

        if resample == "1d" and interval != "1d":
            bpd = bars_per_day_map.get(interval, 96)
            candles = resample_to_daily(candles, bpd)
            logger.info("Resampled %s to daily: %d bars (%.1f days)", symbol, len(candles), len(candles))
        else:
            logger.info(
                "Using %s: %d bars (%.1f days)",
                symbol, len(candles),
                len(candles) / bars_per_day_map.get(interval, 96),
            )

        result[symbol] = candles

    return result


# ---------------------------------------------------------------------------
# Main comparison engine
# ---------------------------------------------------------------------------

class SqueezeStrategyComparison:
    """Run Strategy A vs Strategy B on squeeze fire triggers."""

    def __init__(self, cfg: SqueezeCompareConfig, model=None, prompt_builder=None) -> None:
        self.cfg = cfg
        self.model = model  # FlagTraderModel or None
        self.prompt_builder = prompt_builder  # PromptBuilder or None
        self.has_model = model is not None

    def run(
        self,
        candles_by_symbol: dict[str, list[dict]],
    ) -> tuple[StrategyStats, StrategyStats]:
        """Run comparison backtest.

        Returns (stats_a, stats_b).
        """
        cfg = self.cfg

        # Equity tracking
        equity_a = cfg.initial_capital
        equity_b = cfg.initial_capital
        peak_a = equity_a
        peak_b = equity_b
        max_dd_a = 0.0
        max_dd_b = 0.0
        equity_curve_a: list[float] = [equity_a]
        equity_curve_b: list[float] = [equity_b]

        # Open and closed trades
        open_a: dict[str, StrategyATrade] = {}  # symbol -> trade
        open_b: dict[str, StrategyBTrade] = {}
        closed_a: list[StrategyATrade] = []
        closed_b: list[StrategyBTrade] = []

        # Track which bars had fires per symbol to deduplicate
        last_fire_bar: dict[str, int] = {}

        # Find common bar count
        min_bars = min(len(c) for c in candles_by_symbol.values())
        start_bar = 60  # need enough history for SMA(50) + rising lookback

        total_fires = 0
        action_history: list[str] = []

        logger.info(
            "Running comparison: %d symbols, %d bars (from bar %d)",
            len(candles_by_symbol), min_bars, start_bar,
        )

        for bar_idx in range(start_bar, min_bars):
            # ---------------------------------------------------------------
            # 1. Check exits for open trades
            # ---------------------------------------------------------------

            # Strategy A exits
            symbols_to_close_a = []
            for sym, trade in open_a.items():
                candle = candles_by_symbol[sym][bar_idx]
                bars_held = bar_idx - trade.entry_bar

                if candle["low"] <= trade.stop_price:
                    # Stop hit
                    trade.exit_price = trade.stop_price
                    trade.exit_bar = bar_idx
                    trade.exit_reason = "stop"
                    trade.pnl_pct = (trade.exit_price - trade.entry_price) / trade.entry_price * 100
                    notional = equity_a * cfg.position_pct * cfg.leverage
                    trade.pnl_usd = notional * trade.pnl_pct / 100
                    fee = notional * cfg.taker_fee
                    trade.pnl_usd -= fee
                    equity_a += trade.pnl_usd
                    closed_a.append(trade)
                    symbols_to_close_a.append(sym)
                elif bars_held >= 5:
                    # Max hold 5 bars
                    trade.exit_price = candle["close"]
                    trade.exit_bar = bar_idx
                    trade.exit_reason = "max_hold_5"
                    trade.pnl_pct = (trade.exit_price - trade.entry_price) / trade.entry_price * 100
                    notional = equity_a * cfg.position_pct * cfg.leverage
                    trade.pnl_usd = notional * trade.pnl_pct / 100
                    fee = notional * cfg.taker_fee
                    trade.pnl_usd -= fee
                    equity_a += trade.pnl_usd
                    closed_a.append(trade)
                    symbols_to_close_a.append(sym)

            for sym in symbols_to_close_a:
                del open_a[sym]

            # Strategy B exits
            symbols_to_close_b = []
            for sym, trade in open_b.items():
                candle = candles_by_symbol[sym][bar_idx]
                bars_held = bar_idx - trade.entry_bar

                # Check TP/SL
                if trade.direction == "long":
                    sl_hit = candle["low"] <= trade.sl_price
                    tp_hit = candle["high"] >= trade.tp_price
                else:
                    sl_hit = candle["high"] >= trade.sl_price
                    tp_hit = candle["low"] <= trade.tp_price

                # SL wins if both hit same candle (conservative)
                if sl_hit:
                    trade.exit_price = trade.sl_price
                    trade.exit_bar = bar_idx
                    trade.exit_reason = "sl"
                elif tp_hit:
                    trade.exit_price = trade.tp_price
                    trade.exit_bar = bar_idx
                    trade.exit_reason = "tp"
                elif bars_held >= 24:
                    trade.exit_price = candle["close"]
                    trade.exit_bar = bar_idx
                    trade.exit_reason = "max_hold"
                else:
                    continue  # still open

                if trade.direction == "long":
                    trade.pnl_pct = (trade.exit_price - trade.entry_price) / trade.entry_price * 100
                else:
                    trade.pnl_pct = (trade.entry_price - trade.exit_price) / trade.entry_price * 100

                notional = equity_b * cfg.position_pct * cfg.leverage
                trade.pnl_usd = notional * trade.pnl_pct / 100
                fee = notional * cfg.taker_fee
                trade.pnl_usd -= fee
                equity_b += trade.pnl_usd
                closed_b.append(trade)
                symbols_to_close_b.append(sym)

            for sym in symbols_to_close_b:
                del open_b[sym]

            # ---------------------------------------------------------------
            # 2. Check squeeze fire for each symbol without open position
            # ---------------------------------------------------------------
            for symbol, all_candles in candles_by_symbol.items():
                # Slice the last candle_window bars for squeeze detection
                start = max(0, bar_idx - cfg.candle_window + 1)
                window = all_candles[start : bar_idx + 1]

                closes = np.array([c["close"] for c in window])
                highs = np.array([c["high"] for c in window])
                lows = np.array([c["low"] for c in window])

                squeeze = detect_squeeze_state(
                    symbol=symbol,
                    close=closes,
                    high=highs,
                    low=lows,
                    bb_period=cfg.bb_period,
                    bb_std_mult=cfg.bb_std_mult,
                    kc_ema_period=cfg.kc_ema_period,
                    kc_atr_period=cfg.kc_atr_period,
                    kc_atr_mult=cfg.kc_atr_mult,
                    lookback=cfg.squeeze_lookback,
                )

                # --- Strategy A entry (independent trigger — runs every bar) ---
                if symbol not in open_a:
                    hist_start = max(0, bar_idx - cfg.sma_trend_period - cfg.sma_rising_lookback - 10)
                    hist = all_candles[hist_start : bar_idx + 1]
                    h_closes = np.array([c["close"] for c in hist])
                    h_highs = np.array([c["high"] for c in hist])
                    h_lows = np.array([c["low"] for c in hist])

                    signal, entry_price, signal_low = compute_ema_high_signal(
                        h_closes, h_highs, h_lows, cfg,
                    )
                    if signal == "long":
                        notional = equity_a * cfg.position_pct * cfg.leverage
                        entry_fee = notional * cfg.maker_fee
                        equity_a -= entry_fee

                        trade_a = StrategyATrade(
                            symbol=symbol,
                            entry_price=entry_price,
                            entry_bar=bar_idx,
                            signal_low=signal_low,
                            stop_price=signal_low,
                        )
                        open_a[symbol] = trade_a
                        logger.debug(
                            "A: LONG %s @ %.2f, stop=%.2f (bar %d)",
                            symbol, entry_price, signal_low, bar_idx,
                        )

                # --- Strategy B entry (only on squeeze fire) ---
                is_fire = squeeze.fired
                if is_fire:
                    if symbol in last_fire_bar and (bar_idx - last_fire_bar[symbol]) < cfg.candle_window:
                        is_fire = False
                    else:
                        last_fire_bar[symbol] = bar_idx
                        total_fires += 1

                if is_fire and symbol not in open_b and self.has_model:
                    # Build prompt from candle window
                    prompt_window = all_candles[max(0, bar_idx - 19) : bar_idx + 1]
                    candles_prompt = [
                        {
                            "open": c["open"],
                            "high": c["high"],
                            "low": c["low"],
                            "close": c["close"],
                            "volume": c["volume"],
                        }
                        for c in prompt_window
                    ]

                    portfolio = {
                        "cash_balance": 0.0,
                        "asset_position": 0.0,
                        "total_account_value": 0.0,
                    }
                    history = {
                        "recent_rewards": [],
                        "net_values": [],
                        "actions": action_history[-10:],
                    }

                    all_candles_to_now = all_candles[: bar_idx + 1]
                    market_ctx = compute_market_context(all_candles_to_now, symbol=symbol)

                    prompt = self.prompt_builder.build_prompt(  # pyright: ignore[reportOptionalMemberAccess]  # torch/SDK typing
                        candles_prompt, portfolio, history, market_context=market_ctx,
                    )

                    action_id, state_value, _log_prob, tp_pct, sl_pct = self.model.get_action(prompt)  # pyright: ignore[reportOptionalMemberAccess]  # torch/SDK typing
                    action_history.append(ACTION_NAMES[action_id])

                    if action_id == 1:  # HOLD
                        continue
                    if abs(state_value) < cfg.confidence_threshold:
                        continue

                    entry_price = all_candles[bar_idx]["close"]
                    direction = "long" if action_id == 2 else "short"

                    if direction == "long":
                        tp_price = entry_price * (1 + tp_pct / 100)
                        sl_price = entry_price * (1 - sl_pct / 100)
                    else:
                        tp_price = entry_price * (1 - tp_pct / 100)
                        sl_price = entry_price * (1 + sl_pct / 100)

                    notional = equity_b * cfg.position_pct * cfg.leverage
                    entry_fee = notional * cfg.maker_fee
                    equity_b -= entry_fee

                    trade_b = StrategyBTrade(
                        symbol=symbol,
                        direction=direction,
                        entry_price=entry_price,
                        entry_bar=bar_idx,
                        tp_pct=tp_pct,
                        sl_pct=sl_pct,
                        tp_price=tp_price,
                        sl_price=sl_price,
                    )
                    open_b[symbol] = trade_b
                    logger.debug(
                        "B: %s %s @ %.2f, tp=%.2f sl=%.2f (bar %d)",
                        direction.upper(), symbol, entry_price, tp_price, sl_price, bar_idx,
                    )

            # ---------------------------------------------------------------
            # 3. Update equity curves and drawdown
            # ---------------------------------------------------------------
            equity_curve_a.append(equity_a)
            equity_curve_b.append(equity_b)

            if equity_a > peak_a:
                peak_a = equity_a
            dd_a = (peak_a - equity_a) / peak_a * 100 if peak_a > 0 else 0
            if dd_a > max_dd_a:
                max_dd_a = dd_a

            if equity_b > peak_b:
                peak_b = equity_b
            dd_b = (peak_b - equity_b) / peak_b * 100 if peak_b > 0 else 0
            if dd_b > max_dd_b:
                max_dd_b = dd_b

        # ---------------------------------------------------------------
        # Close remaining open trades at last bar
        # ---------------------------------------------------------------
        last_bar = min_bars - 1
        for sym, trade in open_a.items():
            candle = candles_by_symbol[sym][last_bar]
            trade.exit_price = candle["close"]
            trade.exit_bar = last_bar
            trade.exit_reason = "end"
            trade.pnl_pct = (trade.exit_price - trade.entry_price) / trade.entry_price * 100
            notional = equity_a * cfg.position_pct * cfg.leverage
            trade.pnl_usd = notional * trade.pnl_pct / 100
            fee = notional * cfg.taker_fee
            trade.pnl_usd -= fee
            equity_a += trade.pnl_usd
            closed_a.append(trade)

        for sym, trade in open_b.items():
            candle = candles_by_symbol[sym][last_bar]
            trade.exit_price = candle["close"]
            trade.exit_bar = last_bar
            trade.exit_reason = "end"
            if trade.direction == "long":
                trade.pnl_pct = (trade.exit_price - trade.entry_price) / trade.entry_price * 100
            else:
                trade.pnl_pct = (trade.entry_price - trade.exit_price) / trade.entry_price * 100
            notional = equity_b * cfg.position_pct * cfg.leverage
            trade.pnl_usd = notional * trade.pnl_pct / 100
            fee = notional * cfg.taker_fee
            trade.pnl_usd -= fee
            equity_b += trade.pnl_usd
            closed_b.append(trade)

        # ---------------------------------------------------------------
        # Compute stats
        # ---------------------------------------------------------------
        stats_a = self._compute_stats(
            "Strategy A (EMA High)", "LONG only", closed_a, cfg.initial_capital,
            equity_a, max_dd_a, total_fires,
        )
        stats_b = self._compute_stats(
            "Strategy B (FLAG-Trader LLM)", "LONG + SHORT", closed_b, cfg.initial_capital,
            equity_b, max_dd_b, total_fires,
        )

        return stats_a, stats_b

    @staticmethod
    def _compute_stats(
        name: str,
        direction_label: str,
        trades: list,
        initial_capital: float,
        final_equity: float,
        max_dd: float,
        total_fires: int,
    ) -> StrategyStats:
        stats = StrategyStats(name=name, direction_label=direction_label)
        stats.total_trades = len(trades)
        stats.total_fires = total_fires
        stats.max_drawdown_pct = max_dd
        stats.total_return_pct = (final_equity - initial_capital) / initial_capital * 100

        if not trades:
            return stats

        winners = [t for t in trades if t.pnl_usd > 0]
        losers = [t for t in trades if t.pnl_usd <= 0]
        stats.winners = len(winners)
        stats.win_rate = len(winners) / len(trades) * 100

        gross_profit = sum(t.pnl_usd for t in winners)
        gross_loss = abs(sum(t.pnl_usd for t in losers))
        stats.profit_factor = (
            (gross_profit / gross_loss) if gross_loss > 0
            else float("inf") if gross_profit > 0
            else 0.0
        )

        # Avg win / avg loss in %
        stats.avg_win_pct = (sum(t.pnl_pct for t in winners) / len(winners)) if winners else 0.0
        stats.avg_loss_pct = (sum(t.pnl_pct for t in losers) / len(losers)) if losers else 0.0

        # Expectancy in R (avg risk = avg loss)
        avg_win = (gross_profit / len(winners)) if winners else 0.0
        avg_loss = (gross_loss / len(losers)) if losers else 1.0
        if avg_loss > 0:
            stats.expectancy_r = (
                stats.win_rate / 100 * (avg_win / avg_loss)
                - (1 - stats.win_rate / 100)
            )
        else:
            stats.expectancy_r = 0.0

        durations = [t.exit_bar - t.entry_bar for t in trades if t.exit_bar > 0]
        stats.avg_duration_bars = sum(durations) / len(durations) if durations else 0.0

        return stats


def print_comparison(stats_a: StrategyStats, stats_b: StrategyStats, symbols: list[str]) -> None:
    """Print formatted comparison table."""
    sym_str = ", ".join(symbols)
    print()
    print("=" * 65)
    print(f"  Squeeze Strategy Comparison -- {sym_str}")
    print("=" * 65)

    def fmt_pct(val: float) -> str:
        return f"{val:+.1f}%" if val != 0 else "0.0%"

    def fmt_pf(val: float) -> str:
        if val == float("inf"):
            return "inf"
        return f"{val:.2f}"

    rows = [
        ("Total Fires", f"{stats_a.total_fires}", f"{stats_b.total_fires}"),
        ("Total Trades", f"{stats_a.total_trades}", f"{stats_b.total_trades}"),
        ("Win Rate", f"{stats_a.win_rate:.1f}%", f"{stats_b.win_rate:.1f}%"),
        ("Avg Win", f"{stats_a.avg_win_pct:+.2f}%", f"{stats_b.avg_win_pct:+.2f}%"),
        ("Avg Loss", f"{stats_a.avg_loss_pct:+.2f}%", f"{stats_b.avg_loss_pct:+.2f}%"),
        ("Profit Factor", fmt_pf(stats_a.profit_factor), fmt_pf(stats_b.profit_factor)),
        ("Expectancy (R)", f"{stats_a.expectancy_r:+.2f}R", f"{stats_b.expectancy_r:+.2f}R"),
        ("Max Drawdown", f"-{stats_a.max_drawdown_pct:.1f}%", f"-{stats_b.max_drawdown_pct:.1f}%"),
        ("Total Return", fmt_pct(stats_a.total_return_pct), fmt_pct(stats_b.total_return_pct)),
        ("Avg Duration", f"{stats_a.avg_duration_bars:.1f} bars", f"{stats_b.avg_duration_bars:.1f} bars"),
        ("Direction", stats_a.direction_label, stats_b.direction_label),
    ]

    print(f"  {'Metric':<24s} {'Strategy A (EMA)':>18s}  {'Strategy B (LLM)':>18s}")
    print("-" * 65)
    for label, val_a, val_b in rows:
        print(f"  {label:<24s} {val_a:>18s}  {val_b:>18s}")
    print("=" * 65)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Squeeze Strategy Comparison -- EMA High vs FLAG-Trader LLM",
    )
    parser.add_argument("--symbols", nargs="+", default=["BTC", "ETH", "SOL"])
    parser.add_argument("--data-dir", default="data/candles")
    parser.add_argument("--checkpoint", default="models/flag_trader_qwen/final_model.pt")
    parser.add_argument("--device", default="cpu", choices=["auto", "cpu", "cuda", "mps"])
    parser.add_argument("--capital", type=float, default=100.0)
    parser.add_argument("--confidence", type=float, default=0.6)
    parser.add_argument("--leverage", type=int, default=3)
    parser.add_argument("--position-pct", type=float, default=0.25)
    parser.add_argument("--candle-window", type=int, default=50)
    parser.add_argument("--squeeze-lookback", type=int, default=3)
    parser.add_argument("--daily", action="store_true", help="Resample 15m candles to daily (for Strategy A comparison)")
    parser.add_argument("--strategy-a-only", action="store_true", help="Run only Strategy A (skip LLM)")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Resolve paths
    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir

    checkpoint_path = Path(args.checkpoint)
    if not checkpoint_path.is_absolute():
        checkpoint_path = PROJECT_ROOT / checkpoint_path

    # Load candles
    interval = "1d" if args.daily else "15m"
    candles_by_symbol = await fetch_or_load_candles(
        assets=args.symbols,
        data_dir=data_dir,
        interval=interval,
    )

    if not candles_by_symbol:
        logger.error("No candle data available. Exiting.")
        sys.exit(1)

    # Build config
    cfg = SqueezeCompareConfig(
        symbols=args.symbols,
        data_dir=data_dir,
        candle_window=args.candle_window,
        squeeze_lookback=args.squeeze_lookback,
        initial_capital=args.capital,
        leverage=args.leverage,
        position_pct=args.position_pct,
        checkpoint=checkpoint_path,
        device=args.device,
        confidence_threshold=args.confidence,
    )

    # Load model (Strategy B) — gracefully skip if unavailable or --strategy-a-only
    model = None
    prompt_builder = None

    if args.strategy_a_only:
        logger.info("Strategy A only mode — skipping LLM model loading")
    elif checkpoint_path.exists():
        try:
            import torch as _torch

            # Auto-detect model name from checkpoint
            model_name = cfg.model_name
            ckpt_meta = _torch.load(checkpoint_path, map_location="cpu", weights_only=True)
            if "model_name" in ckpt_meta:
                saved_name = ckpt_meta["model_name"]
                if saved_name != model_name:
                    logger.info("Auto-detected model from checkpoint: %s", saved_name)
                    model_name = saved_name
            del ckpt_meta

            from crypto_bot.flag_trader.model import FlagTraderModel

            logger.info("Loading model %s on device=%s ...", model_name, args.device)
            model = FlagTraderModel(model_name=model_name, device=args.device)
            model.load_trainable(checkpoint_path)
            model.training = False
            for module in model.modules():
                if hasattr(module, "training"):
                    module.training = False
            prompt_builder = PromptBuilder(candle_window=20)
            logger.info("Model loaded successfully — Strategy B enabled")
        except Exception as e:
            logger.warning("Failed to load model: %s — Strategy B will be skipped", e)
            model = None
            prompt_builder = None
    else:
        logger.warning(
            "Checkpoint not found: %s — Strategy B will be skipped (EMA-only mode)",
            checkpoint_path,
        )

    # Run comparison
    engine = SqueezeStrategyComparison(cfg, model=model, prompt_builder=prompt_builder)
    stats_a, stats_b = engine.run(candles_by_symbol)

    # Print report
    print_comparison(stats_a, stats_b, list(candles_by_symbol.keys()))


if __name__ == "__main__":
    asyncio.run(main())
