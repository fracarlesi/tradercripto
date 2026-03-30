"""
IB Trading Bot - Configuration Loader

Provides:
- YAML configuration loading with validation
- Environment variable override support (${VAR} or ${VAR:default})
- Pydantic v2 models for type safety
- Comprehensive validation with clear error messages
"""

from __future__ import annotations

import os
import re
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    Field,
    field_validator,
    model_validator,
    ConfigDict,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Environment Variable Resolution
# =============================================================================

ENV_VAR_PATTERN = re.compile(r"\$\{([^}:]+)(?::([^}]*))?\}")


def resolve_env_vars(value: Any) -> Any:
    """
    Recursively resolve environment variables in configuration values.

    Supports syntax:
    - ${VAR_NAME} - Required variable, raises if not set
    - ${VAR_NAME:default} - Optional variable with default value

    Args:
        value: Configuration value (str, dict, list, or primitive)

    Returns:
        Value with environment variables resolved

    Raises:
        ValueError: If required environment variable is not set
    """
    if isinstance(value, str):
        def replace_env_var(match: re.Match) -> str:
            var_name = match.group(1)
            default = match.group(2)
            env_value = os.environ.get(var_name)

            if env_value is not None:
                return env_value
            elif default is not None:
                return default
            else:
                raise ValueError(
                    f"Environment variable '{var_name}' is required but not set. "
                    f"Set it with: export {var_name}=<value>"
                )

        resolved = ENV_VAR_PATTERN.sub(replace_env_var, value)

        # Try to convert to appropriate type
        if resolved.lower() == "true":
            return True
        elif resolved.lower() == "false":
            return False
        elif resolved.isdigit():
            return int(resolved)
        try:
            return float(resolved)
        except ValueError:
            return resolved

    elif isinstance(value, dict):
        return {k: resolve_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [resolve_env_vars(item) for item in value]
    else:
        return value


# =============================================================================
# Pydantic Configuration Models
# =============================================================================

class BaseConfig(BaseModel):
    """Base configuration with common settings."""

    model_config = ConfigDict(
        extra="forbid",
        validate_default=True,
        str_strip_whitespace=True,
    )


class IBConnectionConfig(BaseConfig):
    """Interactive Brokers connection configuration."""

    host: str = Field(
        default="127.0.0.1",
        description="TWS/IB Gateway host address",
    )
    port: int = Field(
        default=7497,
        ge=1,
        le=65535,
        description="TWS/IB Gateway port (TWS paper: 7497, live: 7496, Gateway paper: 4002, live: 4001)",
    )
    client_id: int = Field(
        default=1,
        ge=0,
        le=999,
        description="Unique client ID for this connection",
    )
    timeout: int = Field(
        default=30,
        ge=5,
        le=120,
        description="Connection timeout in seconds",
    )
    readonly: bool = Field(
        default=False,
        description="Read-only mode (no order placement)",
    )
    reconnect_delay: int = Field(
        default=5,
        ge=1,
        le=60,
        description="Delay between reconnection attempts in seconds",
    )
    max_reconnect_attempts: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum number of reconnection attempts",
    )

    def __init__(self, **data: Any) -> None:
        # Override from env vars if set
        if "IB_HOST" in os.environ:
            data.setdefault("host", os.environ["IB_HOST"])
        if "IB_PORT" in os.environ:
            data.setdefault("port", int(os.environ["IB_PORT"]))
        if "IB_CLIENT_ID" in os.environ:
            data.setdefault("client_id", int(os.environ["IB_CLIENT_ID"]))
        super().__init__(**data)


class ContractConfig(BaseConfig):
    """Futures contract configuration."""

    symbol: str = Field(description="Contract symbol (e.g. MES, MNQ, ES, NQ)")
    enabled: bool = Field(default=False, description="Whether to trade this contract")

    @field_validator("symbol", mode="before")
    @classmethod
    def uppercase_symbol(cls, v: str) -> str:
        return v.upper() if isinstance(v, str) else v


class OpeningRangeConfig(BaseConfig):
    """Opening range time window and filter configuration."""

    or_start: str = Field(
        default="09:30",
        description="Opening range start time (ET)",
    )
    or_end: str = Field(
        default="09:45",
        description="Opening range end time (ET)",
    )
    bar_size: str = Field(
        default="1 min",
        description="Bar size for opening range calculation",
    )
    min_range_ticks: int = Field(
        default=8,
        ge=1,
        le=200,
        description="Minimum range in ticks (skip too-flat ranges)",
    )
    max_range_ticks: int = Field(
        default=80,
        ge=10,
        le=500,
        description="Maximum range in ticks (skip too-volatile ranges)",
    )
    timezone: str = Field(
        default="US/Eastern",
        description="Timezone for time-based rules",
    )

    @model_validator(mode="after")
    def validate_range_ticks(self) -> "OpeningRangeConfig":
        if self.min_range_ticks >= self.max_range_ticks:
            raise ValueError(
                f"min_range_ticks ({self.min_range_ticks}) must be less than "
                f"max_range_ticks ({self.max_range_ticks})"
            )
        return self


class StrategyConfig(BaseConfig):
    """ORB strategy parameters."""

    name: Literal["orb", "ema_momentum", "rsi2_connors"] = Field(
        default="orb",
        description="Strategy name (orb, ema_momentum, or rsi2_connors)",
    )
    breakout_buffer_ticks: int = Field(
        default=2,
        ge=0,
        le=20,
        description="Ticks above/below OR high/low for breakout confirmation",
    )
    vwap_confirmation: bool = Field(
        default=True,
        description="Require price above/below VWAP for entry confirmation",
    )
    min_atr_ticks: int = Field(
        default=4,
        ge=1,
        le=50,
        description="Minimum ATR in ticks for volatility filter",
    )
    max_entry_time: str = Field(
        default="11:30",
        description="No new entries after this time (ET)",
    )
    allow_short: bool = Field(
        default=True,
        description="Allow short entries",
    )
    no_reentry_after_stop: bool = Field(
        default=True,
        description="Prevent re-entry in same direction after being stopped out",
    )


class StopsConfig(BaseConfig):
    """Stop loss and take profit configuration."""

    stop_type: Literal["or_midpoint", "or_opposite", "atr_based"] = Field(
        default="or_midpoint",
        description="Stop placement method",
    )
    stop_buffer_ticks: int = Field(
        default=2,
        ge=0,
        le=20,
        description="Additional buffer ticks below stop level",
    )
    reward_risk_ratio: Decimal = Field(
        default=Decimal("1.5"),
        ge=Decimal("0.5"),
        le=Decimal("5.0"),
        description="Reward-to-risk ratio for take profit",
    )
    trailing_enabled: bool = Field(
        default=False,
        description="Enable trailing stop",
    )
    eod_flatten_time: str = Field(
        default="15:45",
        description="Flatten all positions before this time (ET)",
    )


class EMAStrategyConfig(BaseConfig):
    """EMA Momentum strategy parameters."""

    ema_fast: int = Field(
        default=9,
        ge=2,
        le=50,
        description="Fast EMA period",
    )
    ema_slow: int = Field(
        default=21,
        ge=5,
        le=200,
        description="Slow EMA period",
    )
    rsi_period: int = Field(
        default=14,
        ge=5,
        le=50,
        description="RSI lookback period",
    )
    rsi_long_min: float = Field(
        default=30.0,
        ge=0.0,
        le=100.0,
        description="Minimum RSI for long entry",
    )
    rsi_long_max: float = Field(
        default=65.0,
        ge=0.0,
        le=100.0,
        description="Maximum RSI for long entry",
    )
    rsi_short_min: float = Field(
        default=35.0,
        ge=0.0,
        le=100.0,
        description="Minimum RSI for short entry",
    )
    rsi_short_max: float = Field(
        default=70.0,
        ge=0.0,
        le=100.0,
        description="Maximum RSI for short entry",
    )
    atr_stop_multiplier: float = Field(
        default=2.0,
        ge=0.5,
        le=10.0,
        description="ATR multiplier for stop distance",
    )
    reward_risk_ratio: str = Field(
        default="1.5",
        description="Reward-to-risk ratio (string for Decimal compatibility)",
    )

    @field_validator("reward_risk_ratio", mode="before")
    @classmethod
    def coerce_rr_to_str(cls, v: object) -> str:
        return str(v) if not isinstance(v, str) else v
    max_trades_per_day: int = Field(
        default=4,
        ge=1,
        le=20,
        description="Maximum EMA trades per day",
    )
    max_entry_time: str = Field(
        default="15:00",
        description="No new EMA entries after this time (ET)",
    )
    allow_short: bool = Field(
        default=True,
        description="Allow short entries",
    )


class RSIMeanReversionConfig(BaseConfig):
    """RSI Mean Reversion intraday strategy parameters."""

    enabled: bool = Field(
        default=False,
        description="Enable RSI Mean Reversion strategy",
    )
    rsi_period: int = Field(
        default=14,
        ge=2,
        le=50,
        description="RSI lookback period on 5-min bars",
    )
    rsi_entry_long: float = Field(
        default=25.0,
        ge=0.0,
        le=100.0,
        description="RSI threshold for long entry (below this)",
    )
    rsi_entry_short: float = Field(
        default=75.0,
        ge=0.0,
        le=100.0,
        description="RSI threshold for short entry (above this)",
    )
    rsi_exit: float = Field(
        default=50.0,
        ge=0.0,
        le=100.0,
        description="RSI level for mean reversion exit",
    )
    stop_points: float = Field(
        default=6.0,
        ge=1.0,
        le=50.0,
        description="Fixed stop loss in points",
    )
    max_daily_trades: int = Field(
        default=4,
        ge=1,
        le=20,
        description="Maximum trades per day for this strategy",
    )
    start_time: str = Field(
        default="10:00",
        description="Earliest entry time ET (skip opening volatility)",
    )
    end_time: str = Field(
        default="15:30",
        description="Latest entry time ET (skip close)",
    )


class RSI2ConnorsConfig(BaseConfig):
    """RSI(2) Connors Mean Reversion strategy parameters.

    Daily timeframe. Long side: enters when RSI(2) < threshold and price > SMA(200).
    Short side: enters when RSI(2) > short threshold and price < SMA(200).
    Exits when RSI(2) mean-reverts or max hold / catastrophe stop is hit.
    """

    enabled: bool = Field(
        default=False,
        description="Enable RSI2 Connors strategy",
    )
    rsi_period: int = Field(
        default=2,
        ge=2,
        le=14,
        description="RSI lookback period (standard Connors uses 2)",
    )
    rsi_entry_threshold: float = Field(
        default=10.0,
        ge=1.0,
        le=50.0,
        description="Enter long when RSI closes below this level",
    )
    rsi_exit_threshold: float = Field(
        default=70.0,
        ge=50.0,
        le=99.0,
        description="Exit long when RSI closes above this level",
    )
    sma_period: int = Field(
        default=200,
        ge=50,
        le=500,
        description="SMA trend filter period (price must be above)",
    )
    max_hold_days: int = Field(
        default=7,
        ge=1,
        le=30,
        description="Force exit after N trading days if RSI hasn't recovered",
    )
    stop_points: int = Field(
        default=20,
        ge=5,
        le=100,
        description="Catastrophe stop in points (20 pts = $100 on MES)",
    )
    rsi_short_entry_threshold: float = Field(
        default=95.0,
        ge=50.0,
        le=99.0,
        description="Enter short when RSI closes above this level (overbought)",
    )
    rsi_short_exit_threshold: float = Field(
        default=30.0,
        ge=1.0,
        le=50.0,
        description="Exit short when RSI closes below this level (mean reverted)",
    )
    direction: str = Field(
        default="long_only",
        description="Trade direction: 'long_only', 'short_only', or 'both'",
    )


class RiskConfig(BaseConfig):
    """Risk management configuration."""

    max_risk_per_trade_usd: Decimal = Field(
        default=Decimal("500"),
        ge=Decimal("10"),
        le=Decimal("10000"),
        description="Maximum risk per trade in USD",
    )
    max_daily_loss_usd: Decimal = Field(
        default=Decimal("1000"),
        ge=Decimal("50"),
        le=Decimal("50000"),
        description="Maximum daily loss in USD before halting",
    )
    max_contracts_per_trade: int = Field(
        default=2,
        ge=1,
        le=50,
        description="Maximum contracts per single trade",
    )
    max_trades_per_day: int = Field(
        default=2,
        ge=1,
        le=20,
        description="Maximum number of trades per day",
    )
    consecutive_stops_halt: int = Field(
        default=2,
        ge=1,
        le=10,
        description="Halt trading after N consecutive stops",
    )

    @model_validator(mode="after")
    def validate_daily_loss(self) -> "RiskConfig":
        if self.max_daily_loss_usd < self.max_risk_per_trade_usd:
            raise ValueError(
                f"max_daily_loss_usd ({self.max_daily_loss_usd}) must be >= "
                f"max_risk_per_trade_usd ({self.max_risk_per_trade_usd})"
            )
        return self


class NotificationsConfig(BaseConfig):
    """Notification configuration."""

    enabled: bool = Field(default=True, description="Enable notifications")
    ntfy_topic: str = Field(
        default="",
        description="ntfy.sh topic for push notifications",
    )

    @property
    def ntfy_topic_resolved(self) -> str:
        """Get ntfy topic, falling back to env var."""
        return self.ntfy_topic or os.environ.get("NTFY_TOPIC_IB", "")


class ATRFilterConfig(BaseConfig):
    """ATR percentile filter configuration.

    Skips trading on days where the OR-period ATR is in extreme
    percentiles of recent history (too quiet or too volatile).
    """

    enabled: bool = Field(
        default=False,
        description="Enable ATR percentile filter",
    )
    lookback_days: int = Field(
        default=20,
        ge=5,
        le=100,
        description="Rolling window of daily ATR values for percentile calculation",
    )
    low_percentile: float = Field(
        default=20.0,
        ge=0.0,
        le=50.0,
        description="Skip if ATR below this percentile (too quiet)",
    )
    high_percentile: float = Field(
        default=80.0,
        ge=50.0,
        le=100.0,
        description="Skip if ATR above this percentile (too volatile)",
    )

    @model_validator(mode="after")
    def validate_percentiles(self) -> "ATRFilterConfig":
        if self.low_percentile >= self.high_percentile:
            raise ValueError(
                f"low_percentile ({self.low_percentile}) must be less than "
                f"high_percentile ({self.high_percentile})"
            )
        return self


class RegimeConfig(BaseConfig):
    """Regime detection configuration."""

    enabled: bool = Field(
        default=True,
        description="Enable regime detection (observation-only by default)",
    )
    atr_lookback: int = Field(
        default=20,
        ge=5,
        le=100,
        description="ATR history for average calculation",
    )
    price_window: int = Field(
        default=10,
        ge=5,
        le=50,
        description="Bars for trend/chop detection",
    )
    high_vol_multiplier: float = Field(
        default=1.5,
        ge=1.0,
        le=5.0,
        description="ATR multiplier for high volatility classification",
    )
    low_vol_multiplier: float = Field(
        default=0.5,
        ge=0.1,
        le=1.0,
        description="ATR multiplier for low volatility classification",
    )


class OptionsSpreadsConfig(BaseConfig):
    """SPY Credit Put Spread strategy configuration."""

    enabled: bool = Field(
        default=False,
        description="Enable the credit put spread strategy",
    )
    underlying: str = Field(
        default="SPY",
        description="Underlying ticker for spreads",
    )
    spread_width: float = Field(
        default=5.0,
        ge=1.0,
        le=20.0,
        description="Width of the spread in dollars",
    )
    target_delta: float = Field(
        default=0.20,
        ge=0.05,
        le=0.40,
        description="Target delta for the short put leg",
    )
    target_dte: int = Field(
        default=45,
        ge=20,
        le=90,
        description="Target days to expiration at entry",
    )
    profit_target_pct: float = Field(
        default=50.0,
        ge=10.0,
        le=90.0,
        description="Close when spread can be bought back at this % of credit",
    )
    stop_loss_mult: float = Field(
        default=2.0,
        ge=1.5,
        le=5.0,
        description="Close when cost to close reaches this multiple of credit",
    )
    dte_exit: int = Field(
        default=21,
        ge=5,
        le=30,
        description="Close if DTE reaches this level",
    )
    delta_exit: float = Field(
        default=0.30,
        ge=0.20,
        le=0.50,
        description="Close if short leg delta reaches this level",
    )
    max_positions: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum concurrent open spreads",
    )
    entry_frequency_days: int = Field(
        default=14,
        ge=7,
        le=30,
        description="Minimum days between new entries",
    )


class ScorecardConfig(BaseConfig):
    """Paper-trading scorecard configuration."""

    enabled: bool = Field(
        default=True,
        description="Enable EOD scorecard evaluation",
    )
    halt_dd_usd: int = Field(
        default=400,
        ge=50,
        le=10000,
        description="Max drawdown (USD) before HALT state",
    )
    halt_5s_loss_usd: int = Field(
        default=150,
        ge=25,
        le=5000,
        description="5-session loss (USD) before HALT state",
    )
    candidate_pf: float = Field(
        default=1.2,
        ge=0.5,
        le=5.0,
        description="Min profit factor (20-session) for CANDIDATE",
    )
    candidate_min_trades: int = Field(
        default=30,
        ge=5,
        le=200,
        description="Min trades for CANDIDATE promotion",
    )
    candidate_max_dd: int = Field(
        default=300,
        ge=50,
        le=5000,
        description="Max drawdown (USD) for CANDIDATE",
    )
    candidate_min_wr: float = Field(
        default=35.0,
        ge=10.0,
        le=80.0,
        description="Min win rate (%) for CANDIDATE",
    )


class ETFRotationConfig(BaseConfig):
    """Vigilant Asset Allocation (VAA-G4) ETF rotation configuration.

    Monthly rebalance: always 100% in one ETF based on momentum scoring.
    """

    enabled: bool = Field(
        default=False,
        description="Enable ETF rotation strategy",
    )
    strategy: str = Field(
        default="vaa_g4",
        description="Rotation strategy name",
    )
    offensive: list[str] = Field(
        default=["SPY", "EFA", "EEM", "AGG"],
        description="Offensive ETF universe",
    )
    defensive: list[str] = Field(
        default=["BIL", "IEF", "LQD"],
        description="Defensive ETF universe",
    )
    rebalance_day: str = Field(
        default="last_trading_day",
        description="When to rebalance (last_trading_day of month)",
    )
    check_time: str = Field(
        default="15:50",
        description="Time (ET) to check for rebalance",
    )
    client_id: int = Field(
        default=2,
        ge=0,
        le=999,
        description="Separate IB client ID for ETF rotation (avoids conflict with futures bot)",
    )


class ScannerConfig(BaseConfig):
    """Universal scanner configuration for stocks/ETFs.

    Batch EOD scanner that ranks candidates by composite score
    and passes top N to the LLM equity model for trade decisions.
    """

    enabled: bool = Field(
        default=False,
        description="Enable the universal scanner",
    )
    max_candidates: int = Field(
        default=20,
        ge=5,
        le=100,
        description="Maximum candidates to pass to LLM after ranking",
    )
    confidence_threshold: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="Minimum LLM confidence to generate a trade intent",
    )
    max_risk_per_trade_usd: Decimal = Field(
        default=Decimal("200"),
        ge=Decimal("10"),
        le=Decimal("10000"),
        description="Maximum risk in USD per stock/ETF trade",
    )
    max_open_positions: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum concurrent open stock/ETF positions",
    )
    max_shares_per_trade: int = Field(
        default=500,
        ge=1,
        le=10000,
        description="Maximum shares per single trade",
    )
    scan_time: str = Field(
        default="16:15",
        description="Time (ET) to run the EOD scan (after market close)",
    )
    scan_interval_seconds: int = Field(
        default=300,
        ge=60,
        le=3600,
        description="Seconds between live LLM scan cycles during market hours",
    )
    client_id: int = Field(
        default=3,
        ge=0,
        le=999,
        description="Separate IB client ID for scanner (avoids conflict with other bots)",
    )


class LoggingConfig(BaseConfig):
    """Logging configuration."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Logging level",
    )
    file: str = Field(
        default="logs/ib_bot.log",
        description="Path to log file",
    )

    @field_validator("level", mode="before")
    @classmethod
    def uppercase_level(cls, v: str) -> str:
        return v.upper() if isinstance(v, str) else v


# =============================================================================
# Root Configuration
# =============================================================================

class TradingConfig(BaseConfig):
    """Root configuration for IB Trading Bot."""

    ib_connection: IBConnectionConfig = Field(default_factory=IBConnectionConfig)
    contracts: list[ContractConfig] = Field(
        default_factory=lambda: [ContractConfig(symbol="MES", enabled=True)],
        description="List of futures contracts to trade",
    )
    opening_range: OpeningRangeConfig = Field(default_factory=OpeningRangeConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    stops: StopsConfig = Field(default_factory=StopsConfig)
    ema_strategy: EMAStrategyConfig = Field(default_factory=EMAStrategyConfig)
    rsi2_connors: RSI2ConnorsConfig = Field(default_factory=RSI2ConnorsConfig)
    rsi_mean_reversion: RSIMeanReversionConfig = Field(default_factory=RSIMeanReversionConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    atr_filter: ATRFilterConfig = Field(default_factory=ATRFilterConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    regime: RegimeConfig = Field(default_factory=RegimeConfig)
    options_spreads: OptionsSpreadsConfig = Field(default_factory=OptionsSpreadsConfig)
    scorecard: ScorecardConfig = Field(default_factory=ScorecardConfig)
    etf_rotation: ETFRotationConfig = Field(default_factory=ETFRotationConfig)
    scanner_universal: ScannerConfig = Field(default_factory=ScannerConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @property
    def enabled_contracts(self) -> list[ContractConfig]:
        """Get list of enabled contracts."""
        return [c for c in self.contracts if c.enabled]


# =============================================================================
# Configuration Loader
# =============================================================================

_CONFIG_DIR = Path(__file__).parent


def load_config(path: str | Path | None = None) -> TradingConfig:
    """
    Load and validate trading configuration from YAML.

    Args:
        path: Path to YAML config file. Defaults to ib_bot/config/trading.yaml.

    Returns:
        Validated TradingConfig object.

    Raises:
        FileNotFoundError: If config file doesn't exist.
        yaml.YAMLError: If YAML parsing fails.
        pydantic.ValidationError: If validation fails.
    """
    if path is None:
        config_path = _CONFIG_DIR / "trading.yaml"
    else:
        config_path = Path(path)

    if not config_path.exists():
        raise FileNotFoundError(
            f"Configuration file not found: {config_path}. "
            f"Create it or specify a valid path."
        )

    logger.info("Loading IB bot configuration from %s", config_path)

    with open(config_path, "r") as f:
        raw_config = yaml.safe_load(f)

    if raw_config is None:
        raw_config = {}

    # Resolve environment variables
    resolved_config = resolve_env_vars(raw_config)

    config = TradingConfig(**resolved_config)

    enabled = [c.symbol for c in config.enabled_contracts]
    logger.info(
        "Configuration loaded: strategy=%s, enabled_contracts=%s",
        config.strategy.name,
        enabled,
    )

    return config
