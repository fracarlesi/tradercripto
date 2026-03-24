"""
FLAG-Trader Replay Engine
=========================
Simulates the full FLAG-Trader pipeline on historical candle data.
Uses the SAME model, prompt builder, and inference as the live bot.

Usage:
    python -m crypto_bot.scripts.replay_flag_trader \
        --days 7 --assets BTC ETH SOL \
        --checkpoint models/flag_trader_deepseek/final_model.pt
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

# Ensure project root is on sys.path when run as module
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from crypto_bot.flag_trader.data_collector import HyperliquidDataCollector
from crypto_bot.flag_trader.model import FlagTraderModel
from crypto_bot.flag_trader.prompt import PromptBuilder

logger = logging.getLogger(__name__)

ACTION_NAMES = {0: "SELL", 1: "HOLD", 2: "BUY"}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ReplayTrade:
    symbol: str
    direction: str  # "long" or "short"
    entry_price: float
    entry_time: int  # candle index
    tp_pct: float
    sl_pct: float
    tp_price: float
    sl_price: float
    exit_price: float = 0.0
    exit_time: int = 0
    exit_reason: str = ""  # "tp", "sl", "max_hold", "end"
    pnl_pct: float = 0.0
    pnl_usd: float = 0.0
    fees_usd: float = 0.0


@dataclass
class ReplayResult:
    trades: list[ReplayTrade]
    initial_capital: float
    final_equity: float
    total_pnl: float
    total_fees: float
    win_rate: float
    profit_factor: float
    max_drawdown_pct: float
    sharpe_ratio: float
    total_trades: int
    avg_trade_duration_bars: float
    equity_curve: list[float] = field(default_factory=list)

    def print_report(self, days: int, assets: list[str], period_start: str = "", period_end: str = "") -> None:
        print()
        print("=" * 55)
        print(f"  FLAG-Trader Replay Report -- {days} days")
        print("=" * 55)
        print(f"  Assets:        {', '.join(assets)}")
        if period_start and period_end:
            print(f"  Period:        {period_start} -> {period_end}")
        print(f"  Initial:       ${self.initial_capital:.2f}")
        print(f"  Final:         ${self.final_equity:.2f}")
        sign = "+" if self.total_pnl >= 0 else ""
        pnl_pct = (self.total_pnl / self.initial_capital * 100) if self.initial_capital else 0
        print(f"  Net P&L:       {sign}${self.total_pnl:.2f} ({sign}{pnl_pct:.2f}%)")
        print(f"  Total Fees:    ${self.total_fees:.2f}")
        print("-" * 55)
        print(f"  Trades:        {self.total_trades}")
        print(f"  Win Rate:      {self.win_rate:.1f}%")
        print(f"  Profit Factor: {self.profit_factor:.2f}")
        print(f"  Max Drawdown:  {self.max_drawdown_pct:.1f}%")
        print(f"  Sharpe (daily):{self.sharpe_ratio:.2f}")
        print(f"  Avg Duration:  {self.avg_trade_duration_bars:.1f} bars ({self.avg_trade_duration_bars * 15 / 60:.1f}h)")
        print("-" * 55)
        print("  Trade Log:")
        for i, t in enumerate(self.trades, 1):
            sign = "+" if t.pnl_usd >= 0 else ""
            dur = t.exit_time - t.entry_time
            print(
                f"  #{i:<3} {t.direction.upper():<5} {t.symbol:<6} "
                f"entry=${t.entry_price:<10.2f} tp={t.tp_pct:.1f}% sl={t.sl_pct:.1f}% "
                f"-> {t.exit_reason.upper():<8} {sign}${t.pnl_usd:.2f} ({dur} bars)"
            )
        print("=" * 55)


# ---------------------------------------------------------------------------
# Replay engine
# ---------------------------------------------------------------------------

class FlagTraderReplay:
    """Replays FLAG-Trader decisions on historical data."""

    def __init__(
        self,
        model: FlagTraderModel,
        prompt_builder: PromptBuilder,
        config: dict,
    ) -> None:
        self.model = model
        self.prompt_builder = prompt_builder
        self.initial_capital: float = config.get("initial_capital", 100.0)
        self.max_positions: int = config.get("max_positions", 1)
        self.confidence_threshold: float = config.get("confidence_threshold", 0.6)
        self.leverage: int = config.get("leverage", 3)
        self.max_hold_bars: int = config.get("max_hold_bars", 24)
        self.candle_window: int = config.get("candle_window", 20)
        self.position_pct: float = config.get("position_pct", 0.25)
        self.maker_fee: float = 0.0002  # 0.02%
        self.taker_fee: float = 0.0005  # 0.05%
        self._action_history: list[str] = []

    def run(
        self,
        candles_by_symbol: dict[str, list[dict]],
        scan_every: int = 1,
    ) -> ReplayResult:
        """Run replay on historical candles.

        Args:
            candles_by_symbol: {"BTC": [{open,high,low,close,volume}, ...], ...}
            scan_every: Evaluate every N bars (default 1 = every 15min bar).
        """
        equity = self.initial_capital
        peak_equity = equity
        max_dd_pct = 0.0
        equity_curve: list[float] = [equity]

        open_trades: list[ReplayTrade] = []
        closed_trades: list[ReplayTrade] = []

        # Find the common bar count (use shortest series, offset by candle_window)
        min_bars = min(len(c) for c in candles_by_symbol.values())
        if min_bars <= self.candle_window:
            logger.warning("Not enough bars for replay (need > %d, got %d)", self.candle_window, min_bars)
            return self._build_result(closed_trades, equity, equity_curve)

        total_steps = min_bars - self.candle_window
        logger.info("Replay: %d steps across %d assets", total_steps, len(candles_by_symbol))

        for step in range(total_steps):
            bar_idx = self.candle_window + step

            # 1. Check open positions for TP/SL/max_hold
            still_open: list[ReplayTrade] = []
            for trade in open_trades:
                candle = candles_by_symbol[trade.symbol][bar_idx]
                closed = self._check_tp_sl(trade, candle, bar_idx)
                if closed:
                    # Apply fees and update equity
                    notional = abs(trade.pnl_usd / (trade.pnl_pct / 100)) if trade.pnl_pct != 0 else equity * self.position_pct * self.leverage
                    exit_fee = notional * self.taker_fee
                    trade.fees_usd += exit_fee
                    trade.pnl_usd -= exit_fee
                    equity += trade.pnl_usd
                    closed_trades.append(trade)
                    self._action_history.append(f"CLOSE_{trade.direction.upper()}")
                elif (bar_idx - trade.entry_time) >= self.max_hold_bars:
                    # Max hold reached -- close at current close
                    close_price = candle["close"]
                    self._close_trade(trade, close_price, bar_idx, "max_hold")
                    notional = equity * self.position_pct * self.leverage
                    exit_fee = notional * self.taker_fee
                    trade.fees_usd += exit_fee
                    trade.pnl_usd -= exit_fee
                    equity += trade.pnl_usd
                    closed_trades.append(trade)
                    self._action_history.append(f"MAX_HOLD_{trade.direction.upper()}")
                else:
                    still_open.append(trade)
            open_trades = still_open

            # 2. Track equity/drawdown
            equity_curve.append(equity)
            if equity > peak_equity:
                peak_equity = equity
            dd = (peak_equity - equity) / peak_equity * 100 if peak_equity > 0 else 0
            if dd > max_dd_pct:
                max_dd_pct = dd

            # 3. Evaluate new entries (only on scan intervals, and if we have room)
            if step % scan_every != 0:
                continue
            if len(open_trades) >= self.max_positions:
                continue

            for symbol, all_candles in candles_by_symbol.items():
                if len(open_trades) >= self.max_positions:
                    break
                # Skip if already in a position on this symbol
                if any(t.symbol == symbol for t in open_trades):
                    continue

                window = all_candles[bar_idx - self.candle_window + 1 : bar_idx + 1]
                candles_prompt = [
                    {
                        "open": c["open"],
                        "high": c["high"],
                        "low": c["low"],
                        "close": c["close"],
                        "volume": c["volume"],
                    }
                    for c in window
                ]

                action_id, confidence, tp_pct, sl_pct = self._evaluate_symbol(candles_prompt)

                # Only act on BUY/SELL above threshold
                if action_id == 1:  # HOLD
                    continue
                if abs(confidence) < self.confidence_threshold:
                    continue

                entry_price = all_candles[bar_idx]["close"]
                direction = "long" if action_id == 2 else "short"

                if direction == "long":
                    tp_price = entry_price * (1 + tp_pct / 100)
                    sl_price = entry_price * (1 - sl_pct / 100)
                else:
                    tp_price = entry_price * (1 - tp_pct / 100)
                    sl_price = entry_price * (1 + sl_pct / 100)

                notional = equity * self.position_pct * self.leverage
                entry_fee = notional * self.maker_fee

                trade = ReplayTrade(
                    symbol=symbol,
                    direction=direction,
                    entry_price=entry_price,
                    entry_time=bar_idx,
                    tp_pct=tp_pct,
                    sl_pct=sl_pct,
                    tp_price=tp_price,
                    sl_price=sl_price,
                    fees_usd=entry_fee,
                )
                equity -= entry_fee  # pay entry fee immediately
                open_trades.append(trade)
                self._action_history.append(ACTION_NAMES[action_id])

                if step % 50 == 0:
                    logger.info(
                        "Step %d/%d | %s %s @ %.2f | equity=$%.2f | tp=%.1f%% sl=%.1f%%",
                        step, total_steps, direction.upper(), symbol, entry_price, equity, tp_pct, sl_pct,
                    )

        # Close any remaining open trades at last bar close
        for trade in open_trades:
            last_candle = candles_by_symbol[trade.symbol][-1]
            self._close_trade(trade, last_candle["close"], min_bars - 1, "end")
            notional = equity * self.position_pct * self.leverage
            exit_fee = notional * self.taker_fee
            trade.fees_usd += exit_fee
            trade.pnl_usd -= exit_fee
            equity += trade.pnl_usd
            closed_trades.append(trade)

        return self._build_result(closed_trades, equity, equity_curve, max_dd_pct)

    def _evaluate_symbol(self, candles: list[dict[str, float]]) -> tuple[int, float, float, float]:
        """Run model inference, same as live agent."""
        portfolio = {
            "cash_balance": 0.0,
            "asset_position": 0.0,
            "total_account_value": 0.0,
        }
        history = {
            "recent_rewards": [],
            "net_values": [],
            "actions": self._action_history[-10:],
        }
        prompt = self.prompt_builder.build_prompt(candles, portfolio, history)
        action_id, state_value, _log_prob, tp_pct, sl_pct = self.model.get_action(prompt)
        return action_id, state_value, tp_pct, sl_pct

    def _check_tp_sl(self, trade: ReplayTrade, candle: dict, bar_idx: int) -> bool:
        """Check if TP or SL hit on this candle. Returns True if trade closed."""
        if trade.direction == "long":
            sl_hit = candle["low"] <= trade.sl_price
            tp_hit = candle["high"] >= trade.tp_price
        else:
            sl_hit = candle["high"] >= trade.sl_price
            tp_hit = candle["low"] <= trade.tp_price

        # Both hit same candle: SL wins (conservative)
        if sl_hit:
            self._close_trade(trade, trade.sl_price, bar_idx, "sl")
            return True
        if tp_hit:
            self._close_trade(trade, trade.tp_price, bar_idx, "tp")
            return True
        return False

    @staticmethod
    def _close_trade(trade: ReplayTrade, exit_price: float, bar_idx: int, reason: str) -> None:
        trade.exit_price = exit_price
        trade.exit_time = bar_idx
        trade.exit_reason = reason
        if trade.direction == "long":
            trade.pnl_pct = (exit_price - trade.entry_price) / trade.entry_price * 100
        else:
            trade.pnl_pct = (trade.entry_price - exit_price) / trade.entry_price * 100
        # Derive notional from entry fee (entry_fee = notional * maker_fee)
        if trade.fees_usd > 0:
            notional = trade.fees_usd / 0.0002  # maker_fee
        else:
            notional = 100.0  # fallback
        trade.pnl_usd = notional * trade.pnl_pct / 100

    def _build_result(
        self,
        trades: list[ReplayTrade],
        final_equity: float,
        equity_curve: list[float],
        max_dd_pct: float = 0.0,
    ) -> ReplayResult:
        total_pnl = final_equity - self.initial_capital
        total_fees = sum(t.fees_usd for t in trades)
        winners = [t for t in trades if t.pnl_usd > 0]
        losers = [t for t in trades if t.pnl_usd <= 0]
        win_rate = (len(winners) / len(trades) * 100) if trades else 0.0

        gross_profit = sum(t.pnl_usd for t in winners)
        gross_loss = abs(sum(t.pnl_usd for t in losers))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0

        durations = [t.exit_time - t.entry_time for t in trades if t.exit_time > 0]
        avg_dur = sum(durations) / len(durations) if durations else 0.0

        # Daily Sharpe from equity curve (approx 96 bars = 1 day for 15m candles)
        bars_per_day = 96
        daily_returns: list[float] = []
        for i in range(bars_per_day, len(equity_curve), bars_per_day):
            prev = equity_curve[i - bars_per_day]
            if prev > 0:
                daily_returns.append((equity_curve[i] - prev) / prev)
        if len(daily_returns) >= 2:
            mean_r = sum(daily_returns) / len(daily_returns)
            var_r = sum((r - mean_r) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
            std_r = math.sqrt(var_r) if var_r > 0 else 1e-9
            sharpe = (mean_r / std_r) * math.sqrt(252)
        else:
            sharpe = 0.0

        return ReplayResult(
            trades=trades,
            initial_capital=self.initial_capital,
            final_equity=final_equity,
            total_pnl=total_pnl,
            total_fees=total_fees,
            win_rate=win_rate,
            profit_factor=profit_factor,
            max_drawdown_pct=max_dd_pct,
            sharpe_ratio=sharpe,
            total_trades=len(trades),
            avg_trade_duration_bars=avg_dur,
            equity_curve=equity_curve,
        )


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def df_to_candle_list(df: pd.DataFrame) -> list[dict]:
    """Convert a DataFrame (from HyperliquidDataCollector) to list of dicts."""
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


def resolve_assets(assets: list[str], data_dir: Path, interval: str = "15m") -> list[str]:
    """Resolve asset list. If ['all'], scan data_dir for cached parquets."""
    if len(assets) == 1 and assets[0].lower() == "all":
        collector = HyperliquidDataCollector(data_dir=data_dir)
        available = collector.list_available()
        if not available:
            logger.warning("No cached parquet files found in %s", data_dir)
            return []
        logger.info("Found %d cached assets: %s", len(available), ", ".join(available))
        return available
    return assets


async def fetch_or_load_candles(
    assets: list[str],
    days: int,
    data_dir: Path,
    interval: str = "15m",
) -> dict[str, list[dict]]:
    """Load candles from cache or fetch from API."""
    collector = HyperliquidDataCollector(data_dir=data_dir)
    result: dict[str, list[dict]] = {}

    for symbol in assets:
        try:
            df = collector.load_candles(symbol, interval)
            logger.info("Loaded cached %s: %d candles", symbol, len(df))
        except FileNotFoundError:
            logger.info("Fetching %s from API (%d days)...", symbol, days)
            df = await collector.fetch_candles(symbol, interval, days)

        if df.empty:
            logger.warning("No data for %s, skipping", symbol)
            continue

        # Trim to requested days (last N days)
        bars_per_day = {"1m": 1440, "5m": 288, "15m": 96, "1h": 24, "4h": 6, "1d": 1}
        max_bars = days * bars_per_day.get(interval, 96)
        if len(df) > max_bars:
            df = df.tail(max_bars).reset_index(drop=True)

        result[symbol] = df_to_candle_list(df)
        logger.info("Using %s: %d bars (%.1f days)", symbol, len(result[symbol]), len(result[symbol]) / bars_per_day.get(interval, 96))

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FLAG-Trader Replay Engine -- backtest on historical candles"
    )
    parser.add_argument("--days", type=int, default=7, help="Days of history to replay")
    parser.add_argument("--assets", nargs="+", default=["all"], help="Assets to replay (default: 'all' = all cached parquets in data-dir)")
    parser.add_argument("--model", default="HuggingFaceTB/SmolLM2-135M-Instruct", help="HuggingFace model name")
    parser.add_argument("--checkpoint", default="models/flag_trader_deepseek/final_model.pt", help="Checkpoint path")
    parser.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda", "mps"])
    parser.add_argument("--capital", type=float, default=100.0, help="Initial capital ($)")
    parser.add_argument("--confidence", type=float, default=0.6, help="Min confidence threshold")
    parser.add_argument("--leverage", type=int, default=3, help="Leverage multiplier")
    parser.add_argument("--max-positions", type=int, default=1, help="Max concurrent positions")
    parser.add_argument("--scan-every", type=int, default=1, help="Evaluate every N bars")
    parser.add_argument("--max-hold", type=int, default=24, help="Max hold bars before forced close")
    parser.add_argument("--data-dir", default="data/candles", help="Candle data directory")
    parser.add_argument("--position-pct", type=float, default=0.25, help="Position size as fraction of equity")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    checkpoint_path = Path(args.checkpoint)
    if not checkpoint_path.is_absolute():
        checkpoint_path = PROJECT_ROOT / checkpoint_path
    if not checkpoint_path.exists():
        logger.error("Checkpoint not found: %s", checkpoint_path)
        logger.error("If the scp transfer is still in progress, wait and retry.")
        sys.exit(1)

    # Resolve data dir
    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir

    # 1. Load model — auto-detect model_name from checkpoint if --model not explicitly set
    import torch as _torch
    model_name = args.model
    ckpt_meta = _torch.load(checkpoint_path, map_location="cpu", weights_only=True)
    if "model_name" in ckpt_meta:
        saved_name = ckpt_meta["model_name"]
        if model_name == "HuggingFaceTB/SmolLM2-135M-Instruct" and saved_name != model_name:
            logger.info("Auto-detected model from checkpoint: %s", saved_name)
            model_name = saved_name
    del ckpt_meta  # free memory before loading full model

    logger.info("Loading model %s on device=%s ...", model_name, args.device)
    model = FlagTraderModel(model_name=model_name, device=args.device)
    logger.info("Loading checkpoint %s ...", checkpoint_path)
    model.load_trainable(checkpoint_path)
    # Set model to inference mode
    model.training = False
    for module in model.modules():
        if hasattr(module, 'training'):
            module.training = False
    logger.info("Model loaded successfully")

    # 2. Resolve assets and fetch/load candles
    assets = resolve_assets(args.assets, data_dir, interval="15m")
    if not assets:
        logger.error("No assets to replay. Add parquet files to %s or specify --assets.", data_dir)
        sys.exit(1)

    candles_by_symbol = await fetch_or_load_candles(
        assets=assets,
        days=args.days,
        data_dir=data_dir,
        interval="15m",
    )

    if not candles_by_symbol:
        logger.error("No candle data available. Exiting.")
        sys.exit(1)

    # 3. Run replay
    prompt_builder = PromptBuilder(candle_window=20)
    replay = FlagTraderReplay(
        model=model,
        prompt_builder=prompt_builder,
        config={
            "initial_capital": args.capital,
            "max_positions": args.max_positions,
            "confidence_threshold": args.confidence,
            "leverage": args.leverage,
            "max_hold_bars": args.max_hold,
            "candle_window": 20,
            "position_pct": args.position_pct,
        },
    )

    logger.info("Starting replay: %d assets, scan_every=%d ...", len(candles_by_symbol), args.scan_every)
    result = replay.run(candles_by_symbol, scan_every=args.scan_every)

    # 4. Print report
    result.print_report(
        days=args.days,
        assets=list(candles_by_symbol.keys()),
    )


if __name__ == "__main__":
    asyncio.run(main())
