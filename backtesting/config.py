"""BacktestConfig: config-driven from trading.yaml instead of hardcoded constants."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


def _find_trading_yaml() -> Path:
    """Locate trading.yaml relative to this file or cwd."""
    candidates = [
        Path(__file__).resolve().parent.parent / "crypto_bot" / "config" / "trading.yaml",
        Path.cwd() / "crypto_bot" / "config" / "trading.yaml",
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError("Cannot find crypto_bot/config/trading.yaml")


@dataclass
class BacktestConfig:
    """All backtest parameters, loaded from trading.yaml with CLI overrides."""

    # Account
    account_size: float = 86.0
    fee_pct: float = 0.00045  # 0.045% per side (Hyperliquid perps taker fee, Tier 0)
    slippage_pct: float = 0.0005  # 0.05% adverse slippage on entry/exit
    spread_filter_pct: float = 0.30  # Max spread % (pre-trade filter)

    # From stops.*
    tp_pct: float = 0.008  # 0.8%
    sl_pct: float = 0.004  # 0.4%

    # From risk.*
    position_pct: float = 0.10  # 10% per trade
    leverage: int = 10
    max_positions: int = 3
    max_daily_trades: int = 8

    # From regime.* (level hysteresis)
    trend_adx_entry_min: float = 28.0  # Stricter threshold to enter TREND
    trend_adx_exit_min: float = 22.0   # Lenient threshold to stay in TREND
    confirmation_bars: int = 3

    # From strategies.trend_momentum.*
    rsi_long_min: float = 30.0
    rsi_long_max: float = 65.0
    rsi_short_min: float = 40.0
    rsi_short_max: float = 70.0
    min_atr_pct: float = 0.1

    # From universe.*
    exclude_symbols: set[str] = field(default_factory=set)

    # Exit management
    momentum_exit_min_profit_pct: float = 0.001  # 0.1% — min profit before momentum_fade exit
    breakeven_threshold_pct: float = 0.012        # 1.2% — profit at which SL moves to entry

    # CLI overridable
    timeframe: str = "15m"
    lookback_days: int = 7
    warmup_bars: int = 200


def load_config(**overrides: object) -> BacktestConfig:
    """Load config from trading.yaml, applying any CLI overrides."""
    yaml_path = _find_trading_yaml()
    with open(yaml_path) as f:
        raw = yaml.safe_load(f)

    stops = raw.get("stops", {})
    risk = raw.get("risk", {})
    regime = raw.get("regime", {})
    strat = raw.get("strategies", {}).get("trend_momentum", {})
    universe = raw.get("universe", {})

    cfg = BacktestConfig(
        tp_pct=stops.get("take_profit_pct", 0.8) / 100,
        sl_pct=stops.get("stop_loss_pct", 0.4) / 100,
        position_pct=risk.get("per_trade_pct", 10.0) / 100,
        leverage=risk.get("leverage", 10),
        max_positions=risk.get("max_positions", 3),
        max_daily_trades=risk.get("max_daily_trades", 8),
        trend_adx_entry_min=regime.get("trend_adx_entry_min", 28),
        trend_adx_exit_min=regime.get("trend_adx_exit_min", 22),
        confirmation_bars=regime.get("confirmation_bars", 3),
        rsi_long_min=strat.get("rsi_long_min", 30),
        rsi_long_max=strat.get("rsi_long_max", 65),
        rsi_short_min=strat.get("rsi_short_min", 40),
        rsi_short_max=strat.get("rsi_short_max", 70),
        min_atr_pct=strat.get("min_atr_pct", 0.1),
        exclude_symbols=set(universe.get("exclude_symbols", [])),
    )

    # Apply CLI overrides
    for key, val in overrides.items():
        if val is not None and hasattr(cfg, key):
            setattr(cfg, key, val)

    return cfg
