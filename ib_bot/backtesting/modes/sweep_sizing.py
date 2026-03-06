"""
IB Backtesting - Position Sizing Sweep
========================================

Tests Config F (best ORB config) with different account sizes and
position sizing parameters to find the right size for a $2K account.

Usage:
    python -m ib_bot.backtesting sweep-sizing --days 90
    python -m ib_bot.backtesting sweep-sizing --days 90 --verbose
"""

from __future__ import annotations

import logging
from argparse import Namespace
from dataclasses import replace
from datetime import date, timedelta
from typing import Any, Dict, List, Tuple

from ..config import IBBacktestConfig, load_backtest_config
from ..data import IBDataFetcher
from ..orb_detector import detect_opening_range
from ..simulator import ORBSimulator
from ..stats import IBBacktestResult

logger = logging.getLogger(__name__)

# =========================================================================
# Config F base params (best ORB config from parameter sweep)
# =========================================================================

CONFIG_F_BASE: Dict[str, Any] = {
    "min_range_ticks": 4,
    "max_range_ticks": 120,
    "max_entry_time": "14:00",
    "breakout_buffer_ticks": 1,
    "min_atr_ticks": 2,
    "reward_risk_ratio": 2.0,
}

# =========================================================================
# Sizing configurations to sweep
# =========================================================================

SIZING_CONFIGS: List[Tuple[str, Dict[str, Any]]] = [
    (
        "Size-A: $10K/2c",
        {"account_size": 10_000.0, "max_risk_per_trade_usd": 500.0, "max_contracts_per_trade": 2, "max_daily_loss_usd": 1_000.0},
    ),
    (
        "Size-B: $10K/1c",
        {"account_size": 10_000.0, "max_risk_per_trade_usd": 250.0, "max_contracts_per_trade": 1, "max_daily_loss_usd": 500.0},
    ),
    (
        "Size-C: $5K/1c",
        {"account_size": 5_000.0, "max_risk_per_trade_usd": 250.0, "max_contracts_per_trade": 1, "max_daily_loss_usd": 500.0},
    ),
    (
        "Size-D: $2K/1c",
        {"account_size": 2_000.0, "max_risk_per_trade_usd": 100.0, "max_contracts_per_trade": 1, "max_daily_loss_usd": 200.0},
    ),
    (
        "Size-E: $2K/micro",
        {"account_size": 2_000.0, "max_risk_per_trade_usd": 50.0, "max_contracts_per_trade": 1, "max_daily_loss_usd": 100.0},
    ),
    (
        "Size-F: $2K/2%",
        {"account_size": 2_000.0, "max_risk_per_trade_usd": 40.0, "max_contracts_per_trade": 1, "max_daily_loss_usd": 80.0},
    ),
]


def _build_configs(
    base: IBBacktestConfig,
) -> List[Tuple[str, IBBacktestConfig]]:
    """Build all sizing configs from Config F base."""
    configs: List[Tuple[str, IBBacktestConfig]] = []
    for name, overrides in SIZING_CONFIGS:
        # Start with Config F params, then layer sizing overrides
        merged = {**CONFIG_F_BASE, **overrides}
        cfg = replace(base, **merged)
        configs.append((name, cfg))
    return configs


def _print_comparison_table(
    results: List[Tuple[str, IBBacktestResult, int]],
) -> None:
    """Print a side-by-side comparison table of all sizing configs.

    Args:
        results: List of (name, result, skipped_trades) tuples.
                 skipped_trades = setups that got 0 contracts from size_trade().
    """
    print()
    print("=" * 130)
    print("  POSITION SIZING SWEEP RESULTS  (Config F base: ORB min4/max120, entry<14:00, R:R=2.0)")
    print("=" * 130)
    print()

    # Header
    header = (
        f"  {'Config':<20s} | {'Account':>8s} | {'MaxC':>4s} | "
        f"{'#Trades':>7s} | {'Win%':>6s} | {'Net P&L':>10s} | "
        f"{'Return%':>8s} | {'MaxDD':>10s} | {'MaxDD%':>7s} | {'Sharpe':>7s} | {'Skip':>5s}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    best_sharpe = -999.0
    best_sharpe_name = ""
    best_dd_pct = 999.0
    best_dd_name = ""

    for name, r, skipped in results:
        # Find account size and max_contracts from the config name
        acct = ""
        maxc = ""
        for cname, overrides in SIZING_CONFIGS:
            if cname == name:
                acct = f"${overrides['account_size']:,.0f}"
                maxc = str(overrides["max_contracts_per_trade"])
                break

        line = (
            f"  {name:<20s} | {acct:>8s} | {maxc:>4s} | "
            f"{r.count:>7d} | {r.win_rate:>5.1f}% | "
            f"${r.net_pnl:>+9,.2f} | "
            f"{r.return_pct:>+7.2f}% | ${r.max_drawdown:>9,.2f} | "
            f"{r.max_drawdown_pct:>6.1f}% | {r.sharpe:>7.2f} | {skipped:>5d}"
        )
        print(line)

        if r.count >= 3 and r.net_pnl > 0:
            if r.sharpe > best_sharpe:
                best_sharpe = r.sharpe
                best_sharpe_name = name
            if r.max_drawdown_pct < best_dd_pct:
                best_dd_pct = r.max_drawdown_pct
                best_dd_name = name

    print()
    print(f"  >>> Best Sharpe (positive P&L): {best_sharpe_name} (Sharpe={best_sharpe:.2f})")
    print(f"  >>> Lowest DD%  (positive P&L): {best_dd_name} (MaxDD={best_dd_pct:.1f}%)")
    print()

    # Recommendation for $2K account
    two_k_results = [(n, r, s) for n, r, s in results if "$2K" in n and r.count > 0]
    if two_k_results:
        print("  --- $2K Account Analysis ---")
        for name, r, skipped in two_k_results:
            viable = "OK" if r.max_drawdown_pct < 30 else "RISKY" if r.max_drawdown_pct < 50 else "BLOW UP"
            print(
                f"  {name}: DD={r.max_drawdown_pct:.1f}%, Return={r.return_pct:+.1f}%, "
                f"Trades={r.count}, Skipped={skipped} -> [{viable}]"
            )
        print()

    print("  Skip = setups where size_trade() returned 0 contracts (risk too large for max_risk)")
    print()
    print("=" * 130)


async def run_sweep_sizing(args: Namespace) -> None:
    """Run position sizing sweep: Config F with different account/risk sizes.

    Steps:
        1. Load base config
        2. Build sizing variants (all using Config F strategy params)
        3. Fetch data ONCE for MES + MNQ
        4. Run ORBSimulator for each sizing config
        5. Print comparison table with sizing-specific metrics
    """
    symbols = ["MES", "MNQ"]

    base_cfg = load_backtest_config(
        symbols=symbols,
        lookback_days=args.days,
    )

    configs = _build_configs(base_cfg)

    end_date = date.today()
    start_date = end_date - timedelta(days=base_cfg.lookback_days)

    print(f"\n  Sizing Sweep: {len(configs)} configs x {len(symbols)} symbols x {base_cfg.lookback_days}d")
    print(f"  Symbols: {symbols}")
    print(f"  Period: {start_date} -> {end_date}")
    print(f"  Base: Config F (min_range=4, max_range=120, entry<14:00, R:R=2.0)\n")

    # --- Fetch data ONCE ---
    fetcher = IBDataFetcher(
        host=args.ib_host,
        port=args.ib_port,
        client_id=2,
        cache_dir=base_cfg.cache_dir,
    )

    await fetcher.connect()

    try:
        bars_by_day: Dict[str, Dict[str, list]] = {}
        for symbol in symbols:
            symbol_bars = await fetcher.fetch_days(symbol, start_date, end_date)
            for day_str, bars in symbol_bars.items():
                bars_by_day.setdefault(day_str, {})[symbol] = bars
    finally:
        await fetcher.disconnect()

    logger.info("Data ready: %d trading days", len(bars_by_day))

    if not bars_by_day:
        logger.error("No data fetched. Check IB connection and cache.")
        return

    # --- Run each sizing config on the same data ---
    results: List[Tuple[str, IBBacktestResult, int]] = []

    for name, cfg in configs:
        cfg = replace(cfg, symbols=symbols)

        logger.info("Running config: %s (account=$%.0f, max_risk=$%.0f, max_contracts=%d)",
                     name, cfg.account_size, cfg.max_risk_per_trade_usd, cfg.max_contracts_per_trade)

        sim = ORBSimulator(cfg)
        sim.run(bars_by_day, detect_opening_range)

        # Count skipped setups (where size_trade returned 0)
        # We track this by running a second pass just for counting
        skipped = _count_skipped_setups(bars_by_day, cfg)

        result = IBBacktestResult(
            label=name,
            trades=sim.trades,
            equity_curve=sim.equity_curve,
            daily_results=sim.daily_results,
            initial_equity=cfg.account_size,
        )
        results.append((name, result, skipped))

        logger.info(
            "  %s: %d trades (skipped=%d), P&L=$%.2f, DD=$%.2f (%.1f%%)",
            name, result.count, skipped, result.net_pnl,
            result.max_drawdown, result.max_drawdown_pct,
        )

    # --- Print comparison ---
    _print_comparison_table(results)

    # Optionally print individual trade logs
    if getattr(args, "trades", False):
        from ..stats import print_summary, print_trade_log

        for name, result, _ in results:
            print_summary(result)
            print_trade_log(result)


def _count_skipped_setups(
    bars_by_day: Dict[str, Dict[str, list]],
    cfg: IBBacktestConfig,
) -> int:
    """Count how many valid ORB setups would be skipped due to position sizing.

    Runs the full detection pipeline but only counts setups where
    size_trade() would return 0 contracts. This tells us how many
    trading opportunities are lost due to tight risk limits.
    """
    from decimal import Decimal
    from datetime import time as dt_time

    from ..orb_detector import detect_opening_range
    from ..simulator import size_trade, DailyRiskState
    from ...core.contracts import CONTRACTS
    from ...core.enums import Direction, SessionPhase
    from ...core.models import FuturesMarketState
    from ...strategies.orb import ORBStrategy
    from ...services.market_data import VWAPCalculator, ATRCalculator
    from ...config.loader import StrategyConfig, StopsConfig

    strategy_config = StrategyConfig(
        name="orb",
        breakout_buffer_ticks=cfg.breakout_buffer_ticks,
        vwap_confirmation=cfg.vwap_confirmation,
        min_atr_ticks=cfg.min_atr_ticks,
        max_entry_time=cfg.max_entry_time,
        allow_short=cfg.allow_short,
        no_reentry_after_stop=cfg.no_reentry_after_stop,
    )
    stops_config = StopsConfig(
        stop_type=cfg.stop_type,
        stop_buffer_ticks=cfg.stop_buffer_ticks,
        reward_risk_ratio=Decimal(str(cfg.reward_risk_ratio)),
        trailing_enabled=False,
        eod_flatten_time=cfg.eod_flatten_time,
    )
    strategy = ORBStrategy(strategy_config, stops_config)

    max_entry = dt_time.fromisoformat(cfg.max_entry_time)
    or_end = dt_time.fromisoformat(cfg.or_end)

    skipped = 0

    for date_str in sorted(bars_by_day.keys()):
        day_bars = bars_by_day[date_str]
        for symbol in cfg.symbols:
            bars = day_bars.get(symbol, [])
            if not bars:
                continue
            spec = CONTRACTS.get(symbol)
            if not spec:
                continue

            or_range = detect_opening_range(bars, spec, cfg)
            if or_range is None or not or_range.valid:
                continue

            vwap_calc = VWAPCalculator()
            atr_calc = ATRCalculator()

            for bar in bars:
                h = Decimal(str(bar["h"])) if not isinstance(bar["h"], Decimal) else bar["h"]
                l = Decimal(str(bar["l"])) if not isinstance(bar["l"], Decimal) else bar["l"]
                c = Decimal(str(bar["c"])) if not isinstance(bar["c"], Decimal) else bar["c"]
                v = Decimal(str(bar["v"])) if not isinstance(bar["v"], Decimal) else bar["v"]

                vwap = vwap_calc.update(h, l, c, v)
                atr = atr_calc.update(h, l, c)

                bar_time = bar["dt"]
                t = bar_time.time()

                if t < or_end or t >= max_entry:
                    continue

                state = FuturesMarketState(
                    symbol=symbol,
                    last_price=c,
                    vwap=vwap,
                    atr_14=atr,
                    volume=v,
                    session_phase=SessionPhase.ACTIVE_TRADING,
                    timestamp=bar_time,
                )

                result = strategy.evaluate(state, or_range)
                if result.has_setup and result.setup:
                    contracts = size_trade(result.setup, spec, cfg)
                    if contracts == 0:
                        skipped += 1

    return skipped
