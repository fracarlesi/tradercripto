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
    entry_fee_pct: float = 0.00045  # 0.045% taker default; 0.015% if entry_mode=maker
    exit_fee_pct: float = 0.00045   # 0.045% taker always (TP/SL are server-side trigger orders)
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
    momentum_exit_min_profit_pct: float = 0.09  # 9.0% — effectively disabled (matches trading.yaml)
    breakeven_threshold_pct: float = 0.01        # 1.0% — profit at which SL moves to entry

    # Trailing stop (after breakeven)
    trailing_atr_mult: float = 1.5

    # Graduated ROI exits — keys are minutes as strings, values are ROI fraction
    minimal_roi: dict[str, float] = field(default_factory=lambda: {
        "0": 0.04, "60": 0.02, "120": 0.01, "240": 0.005, "360": 0.0
    })

    # Per-symbol cooldown
    cooldown_minutes: int = 10
    cooldown_after_sl_minutes: int = 30
    max_trades_per_symbol_per_day: int = 2

    # Momentum fade RSI slope threshold (matches live bot)
    momentum_rsi_slope_threshold: float = 1.0
    momentum_exit_min_age_bars: int = 1

    # Regime exit grace period in bars
    regime_exit_grace_bars: int = 1

    # ML model
    ml_threshold: float = 0.58  # ml_model.min_probability from trading.yaml

    # CLI overridable
    timeframe: str = "15m"
    lookback_days: int = 7
    warmup_bars: int = 200

    @property
    def fee_pct(self) -> float:
        """Backward-compat: returns entry_fee_pct (single-side fee for legacy code)."""
        return self.entry_fee_pct

    @property
    def total_fee_pct(self) -> float:
        """Round-trip fees: entry + exit."""
        return self.entry_fee_pct + self.exit_fee_pct


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
    execution = raw.get("execution", {})
    roi_raw = stops.get("minimal_roi", {})
    mom_raw = stops.get("momentum_exit", {})
    ml_cfg = raw.get("ml_model", {})

    # Determine entry fee based on execution.entry_mode
    entry_mode = execution.get("entry_mode", "taker")
    entry_fee = 0.00015 if entry_mode == "maker" else 0.00045  # Maker 0.015%, Taker 0.045%
    exit_fee = 0.00045  # TP/SL are always server-side trigger orders (taker)

    cfg = BacktestConfig(
        entry_fee_pct=entry_fee,
        exit_fee_pct=exit_fee,
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
        trailing_atr_mult=stops.get("trailing_atr_mult", 1.5),
        minimal_roi=(
            {str(k): float(v) for k, v in roi_raw.items()}
            if roi_raw
            else {"0": 0.04, "60": 0.02, "120": 0.01, "240": 0.005, "360": 0.0}
        ),
        cooldown_minutes=10,
        cooldown_after_sl_minutes=30,
        max_trades_per_symbol_per_day=2,
        momentum_exit_min_profit_pct=mom_raw.get("min_profit_pct", 9.0) / 100,
        momentum_rsi_slope_threshold=mom_raw.get("rsi_slope_threshold", 1.0),
        momentum_exit_min_age_bars=1,
        breakeven_threshold_pct=stops.get("breakeven_threshold_pct", 1.0) / 100,
        regime_exit_grace_bars=max(1, regime.get("regime_exit_grace_minutes", 5) // 15),
        ml_threshold=ml_cfg.get("min_probability", 0.58),
    )

    # Apply CLI overrides
    for key, val in overrides.items():
        if val is not None and hasattr(cfg, key):
            setattr(cfg, key, val)

    return cfg
