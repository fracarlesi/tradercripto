"""
IB Backtesting - Configuration
===============================

Backtest-specific configuration dataclass.
Loads defaults from ib_bot/config/trading.yaml with CLI overrides.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_YAML_PATH = Path(__file__).parent.parent / "config" / "trading.yaml"


@dataclass
class IBBacktestConfig:
    """Backtest configuration with defaults matching trading.yaml."""

    # Symbols
    symbols: list[str] = field(default_factory=lambda: ["MES"])

    # Account
    account_size: float = 10_000.0
    commission_per_contract: float = 0.62  # IB MES commission (round-trip ~$1.24)
    slippage_ticks: int = 1  # 1 tick adverse slippage per fill

    # Opening Range
    or_start: str = "09:30"
    or_end: str = "09:45"
    min_range_ticks: int = 8
    max_range_ticks: int = 80

    # Strategy
    breakout_buffer_ticks: int = 2
    vwap_confirmation: bool = True
    min_atr_ticks: int = 4
    max_entry_time: str = "11:30"
    allow_short: bool = True
    no_reentry_after_stop: bool = True

    # Stops
    stop_type: str = "or_midpoint"
    stop_buffer_ticks: int = 2
    reward_risk_ratio: float = 1.5
    eod_flatten_time: str = "15:45"

    # Risk
    max_risk_per_trade_usd: float = 500.0
    max_daily_loss_usd: float = 1_000.0
    max_contracts_per_trade: int = 2
    max_trades_per_day: int = 2
    consecutive_stops_halt: int = 2

    # Backtest-specific
    lookback_days: int = 30
    cache_dir: str = "ib_bot/backtesting/cache"

    # Filters (disabled by default for backward compat)
    ema_trend_filter: bool = False
    ema_period: int = 20
    atr_percentile_filter: bool = False
    atr_low_pct: float = 20.0
    atr_high_pct: float = 80.0
    vwap_slope_filter: bool = False
    vwap_min_slope_ticks: float = 0.5


def _read_yaml(path: Path) -> dict[str, Any]:
    """Read and parse YAML config file."""
    if not path.exists():
        logger.warning("Config file not found: %s — using defaults", path)
        return {}
    with open(path) as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def _extract_from_yaml(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract backtest-relevant fields from trading.yaml structure.

    Maps nested YAML sections (opening_range, strategy, stops, risk)
    into flat IBBacktestConfig field names.
    """
    flat: dict[str, Any] = {}

    # Contracts -> symbols
    contracts = raw.get("contracts", [])
    enabled = [c["symbol"] for c in contracts if c.get("enabled", False)]
    if enabled:
        flat["symbols"] = enabled

    # Opening range
    orng = raw.get("opening_range", {})
    for key in ("or_start", "or_end", "min_range_ticks", "max_range_ticks"):
        if key in orng:
            flat[key] = orng[key]

    # Strategy
    strat = raw.get("strategy", {})
    for key in (
        "breakout_buffer_ticks",
        "vwap_confirmation",
        "min_atr_ticks",
        "max_entry_time",
        "allow_short",
        "no_reentry_after_stop",
    ):
        if key in strat:
            flat[key] = strat[key]

    # Stops
    stops = raw.get("stops", {})
    for key in ("stop_type", "stop_buffer_ticks", "eod_flatten_time"):
        if key in stops:
            flat[key] = stops[key]
    if "reward_risk_ratio" in stops:
        flat["reward_risk_ratio"] = float(stops["reward_risk_ratio"])

    # Risk
    risk = raw.get("risk", {})
    if "max_risk_per_trade_usd" in risk:
        flat["max_risk_per_trade_usd"] = float(risk["max_risk_per_trade_usd"])
    if "max_daily_loss_usd" in risk:
        flat["max_daily_loss_usd"] = float(risk["max_daily_loss_usd"])
    for key in ("max_contracts_per_trade", "max_trades_per_day", "consecutive_stops_halt"):
        if key in risk:
            flat[key] = risk[key]

    return flat


def load_backtest_config(
    yaml_path: str | Path | None = None,
    **overrides: Any,
) -> IBBacktestConfig:
    """Load backtest config from trading.yaml with CLI overrides.

    Priority (highest first):
        1. Keyword overrides (CLI args)
        2. trading.yaml values
        3. IBBacktestConfig defaults

    Args:
        yaml_path: Path to trading.yaml. Defaults to ib_bot/config/trading.yaml.
        **overrides: Field-name keyword arguments that override YAML values.

    Returns:
        Populated IBBacktestConfig instance.
    """
    path = Path(yaml_path) if yaml_path else _YAML_PATH
    raw = _read_yaml(path)
    yaml_fields = _extract_from_yaml(raw)

    # Merge: YAML base, then overrides on top
    merged = {**yaml_fields, **{k: v for k, v in overrides.items() if v is not None}}

    cfg = IBBacktestConfig(**merged)

    logger.info(
        "Backtest config loaded: symbols=%s, lookback=%dd, account=$%.0f",
        cfg.symbols,
        cfg.lookback_days,
        cfg.account_size,
    )
    return cfg
