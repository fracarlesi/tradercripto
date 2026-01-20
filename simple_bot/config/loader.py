"""
HLQuantBot v2.0 - Configuration Loader

Provides:
- YAML configuration loading with validation
- Environment variable override support (${VAR} or ${VAR:default})
- Pydantic v2 models for type safety
- Hot-reload support for runtime updates
- Comprehensive validation with clear error messages
"""

from __future__ import annotations

import os
import re
import logging
from pathlib import Path
from typing import Any, Literal, Callable
from datetime import datetime

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
        extra="forbid",  # Raise error on unknown fields
        validate_default=True,
        str_strip_whitespace=True,
    )


class SystemConfig(BaseConfig):
    """System-level configuration."""
    
    name: str = Field(default="HLQuantBot-v2", description="Bot instance name")
    mode: Literal["testnet", "mainnet"] = Field(
        default="testnet",
        description="Trading mode: testnet for testing, mainnet for live trading"
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Logging level"
    )
    log_file: str = Field(
        default="logs/hlquantbot_v2.log",
        description="Path to log file"
    )
    
    @field_validator("log_level", mode="before")
    @classmethod
    def uppercase_log_level(cls, v: str) -> str:
        return v.upper() if isinstance(v, str) else v


class HyperliquidConfig(BaseConfig):
    """Hyperliquid exchange configuration."""
    
    testnet: bool = Field(default=True, description="Use testnet API")
    api_timeout: int = Field(
        default=30,
        ge=5,
        le=120,
        description="API request timeout in seconds"
    )
    max_retries: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum retry attempts for failed requests"
    )
    rate_limit_orders_per_sec: int = Field(
        default=10,
        ge=1,
        le=20,
        description="Order rate limit per second"
    )
    rate_limit_info_per_min: int = Field(
        default=100,
        ge=10,
        le=200,
        description="Info API rate limit per minute"
    )


class DatabaseConfig(BaseConfig):
    """Database connection configuration."""
    
    host: str = Field(default="localhost", description="Database host")
    port: int = Field(default=5432, ge=1, le=65535, description="Database port")
    name: str = Field(default="trading_db", description="Database name")
    user: str = Field(default="trader", description="Database user")
    password: str = Field(default="trader_password", description="Database password")
    pool_min: int = Field(
        default=2,
        ge=1,
        le=10,
        description="Minimum connection pool size"
    )
    pool_max: int = Field(
        default=10,
        ge=2,
        le=50,
        description="Maximum connection pool size"
    )
    
    @model_validator(mode="after")
    def validate_pool_sizes(self) -> "DatabaseConfig":
        if self.pool_min > self.pool_max:
            raise ValueError(
                f"pool_min ({self.pool_min}) cannot be greater than "
                f"pool_max ({self.pool_max})"
            )
        return self
    
    @property
    def dsn(self) -> str:
        """Generate PostgreSQL connection string."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"


# =============================================================================
# Service Configurations
# =============================================================================

class MarketScannerConfig(BaseConfig):
    """Market scanner service configuration."""
    
    enabled: bool = Field(default=True, description="Enable market scanner")
    interval_seconds: int = Field(
        default=300,
        ge=60,
        le=3600,
        description="Scan interval in seconds"
    )
    coins_limit: int = Field(
        default=200,
        ge=10,
        le=500,
        description="Maximum coins to scan"
    )
    min_volume_24h: float = Field(
        default=1_000_000,
        ge=0,
        description="Minimum 24h volume filter"
    )
    exclude_symbols: list[str] = Field(
        default_factory=lambda: ["USDC", "USDT", "DAI"],
        description="Symbols to exclude from scanning"
    )


class OpportunityWeights(BaseConfig):
    """Weights for opportunity ranking."""
    
    trend_strength: float = Field(default=0.25, ge=0, le=1)
    volatility: float = Field(default=0.20, ge=0, le=1)
    volume: float = Field(default=0.15, ge=0, le=1)
    funding: float = Field(default=0.15, ge=0, le=1)
    liquidity: float = Field(default=0.15, ge=0, le=1)
    momentum: float = Field(default=0.10, ge=0, le=1)
    
    @model_validator(mode="after")
    def validate_weights_sum(self) -> "OpportunityWeights":
        total = (
            self.trend_strength + self.volatility + self.volume +
            self.funding + self.liquidity + self.momentum
        )
        if not (0.99 <= total <= 1.01):
            raise ValueError(
                f"Opportunity weights must sum to 1.0, got {total:.2f}. "
                f"Adjust weights to balance correctly."
            )
        return self


class OpportunityRankerConfig(BaseConfig):
    """Opportunity ranker service configuration."""
    
    enabled: bool = Field(default=True, description="Enable opportunity ranker")
    top_n: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Number of top opportunities to track"
    )
    min_score: float = Field(
        default=0.6,
        ge=0,
        le=1,
        description="Minimum score threshold"
    )
    weights: OpportunityWeights = Field(default_factory=OpportunityWeights)


class StrategySelectorConfig(BaseConfig):
    """Strategy selector service configuration."""
    
    enabled: bool = Field(default=True, description="Enable strategy selector")
    use_llm: bool = Field(
        default=True,
        description="Use LLM for strategy selection"
    )
    fallback_strategy: Literal["momentum", "mean_reversion", "breakout", "funding_arb"] = Field(
        default="momentum",
        description="Fallback strategy if LLM unavailable"
    )
    reselect_interval_minutes: int = Field(
        default=60,
        ge=5,
        le=1440,
        description="Strategy reselection interval in minutes"
    )


class CapitalAllocatorConfig(BaseConfig):
    """Capital allocator service configuration."""
    
    enabled: bool = Field(default=True, description="Enable capital allocator")
    max_positions: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum concurrent positions"
    )
    reserve_pct: float = Field(
        default=0.20,
        ge=0,
        le=0.5,
        description="Reserve capital percentage"
    )
    max_position_pct: float = Field(
        default=0.20,
        ge=0.05,
        le=0.5,
        description="Maximum position size as percentage of capital"
    )
    max_correlated_pct: float = Field(
        default=0.30,
        ge=0.1,
        le=0.8,
        description="Maximum allocation to correlated assets"
    )
    rebalance_threshold_pct: float = Field(
        default=0.10,
        ge=0.01,
        le=0.5,
        description="Threshold to trigger rebalancing"
    )


class ExecutionEngineConfig(BaseConfig):
    """Execution engine service configuration."""
    
    enabled: bool = Field(default=True, description="Enable execution engine")
    order_type: Literal["limit", "market", "smart"] = Field(
        default="smart",
        description="Order type: limit, market, or smart (adaptive)"
    )
    max_slippage_pct: float = Field(
        default=0.5,
        ge=0,
        le=2.0,
        description="Maximum allowed slippage percentage"
    )
    retry_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Retry attempts for failed orders"
    )
    retry_delay_seconds: float = Field(
        default=1.0,
        ge=0.1,
        le=10,
        description="Delay between retry attempts"
    )
    use_reduce_only: bool = Field(
        default=True,
        description="Use reduce-only for closing positions"
    )


class LearningModuleConfig(BaseConfig):
    """Learning module service configuration."""
    
    enabled: bool = Field(default=True, description="Enable learning module")
    optimization_interval_hours: int = Field(
        default=1,
        ge=1,
        le=24,
        description="Optimization cycle interval in hours"
    )
    min_trades_for_optimization: int = Field(
        default=10,
        ge=5,
        le=100,
        description="Minimum trades required for optimization"
    )
    performance_window_hours: int = Field(
        default=24,
        ge=1,
        le=168,
        description="Performance evaluation window in hours"
    )
    rollback_threshold_pct: float = Field(
        default=-5.0,
        le=0,
        description="Performance threshold to trigger rollback"
    )


class ServicesConfig(BaseConfig):
    """All services configuration."""
    
    market_scanner: MarketScannerConfig = Field(default_factory=MarketScannerConfig)
    opportunity_ranker: OpportunityRankerConfig = Field(default_factory=OpportunityRankerConfig)
    strategy_selector: StrategySelectorConfig = Field(default_factory=StrategySelectorConfig)
    capital_allocator: CapitalAllocatorConfig = Field(default_factory=CapitalAllocatorConfig)
    execution_engine: ExecutionEngineConfig = Field(default_factory=ExecutionEngineConfig)
    learning_module: LearningModuleConfig = Field(default_factory=LearningModuleConfig)


# =============================================================================
# Risk Configuration
# =============================================================================

class RiskConfig(BaseConfig):
    """Risk management configuration."""
    
    leverage: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Maximum leverage"
    )
    position_size_pct: float = Field(
        default=10,
        ge=1,
        le=100,
        description="Default position size as percentage of capital"
    )
    max_positions: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum concurrent positions"
    )
    max_drawdown_pct: float = Field(
        default=15,
        ge=1,
        le=50,
        description="Maximum allowed drawdown percentage"
    )
    daily_loss_limit_pct: float = Field(
        default=5,
        ge=1,
        le=20,
        description="Daily loss limit percentage"
    )
    target_monthly_return: float = Field(
        default=12,
        ge=0,
        le=100,
        description="Target monthly return percentage"
    )
    max_correlation: float = Field(
        default=0.7,
        ge=0,
        le=1,
        description="Maximum correlation between positions"
    )
    stop_loss_pct: float = Field(
        default=1.0,
        ge=0.1,
        le=10,
        description="Stop loss percentage"
    )
    take_profit_pct: float = Field(
        default=1.5,
        ge=0.1,
        le=20,
        description="Take profit percentage"
    )
    trailing_stop_pct: float = Field(
        default=0.8,
        ge=0.1,
        le=10,
        description="Trailing stop percentage"
    )
    trailing_stop_activation_pct: float = Field(
        default=0.5,
        ge=0,
        le=10,
        description="Profit percentage to activate trailing stop"
    )
    max_daily_trades: int = Field(
        default=3,
        ge=1,
        le=50,
        description="Maximum number of trades allowed per day (resets at UTC midnight)"
    )

    @model_validator(mode="after")
    def validate_risk_reward(self) -> "RiskConfig":
        if self.take_profit_pct < self.stop_loss_pct:
            logger.warning(
                f"Risk/Reward warning: take_profit_pct ({self.take_profit_pct}) "
                f"is less than stop_loss_pct ({self.stop_loss_pct}). "
                f"This results in negative expected value."
            )
        return self


# =============================================================================
# LLM Configuration
# =============================================================================

class LLMConfig(BaseConfig):
    """LLM provider configuration."""
    
    provider: Literal["deepseek", "openai", "anthropic"] = Field(
        default="deepseek",
        description="LLM provider"
    )
    model: str = Field(
        default="deepseek-chat",
        description="Model name"
    )
    api_key_env: str = Field(
        default="DEEPSEEK_API_KEY",
        description="Environment variable name for API key"
    )
    temperature: float = Field(
        default=0.3,
        ge=0,
        le=2,
        description="LLM temperature"
    )
    max_tokens: int = Field(
        default=2000,
        ge=100,
        le=16000,
        description="Maximum tokens in response"
    )
    timeout: int = Field(
        default=120,
        ge=10,
        le=300,
        description="Request timeout in seconds"
    )
    decisions_per_day: int = Field(
        default=300,
        ge=1,
        le=1000,
        description="Maximum LLM decisions per day"
    )
    retry_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Retry attempts for failed requests"
    )
    retry_delay_seconds: float = Field(
        default=5,
        ge=1,
        le=60,
        description="Delay between retries"
    )
    
    @property
    def api_key(self) -> str | None:
        """Get API key from environment."""
        return os.environ.get(self.api_key_env)


# =============================================================================
# Strategy Configurations
# =============================================================================

class MomentumStrategyConfig(BaseConfig):
    """Momentum strategy configuration."""
    
    enabled: bool = Field(default=True, description="Enable strategy")
    weight: float = Field(
        default=0.35,
        ge=0,
        le=1,
        description="Strategy weight in portfolio"
    )
    timeframe: str = Field(default="4h", description="Candlestick timeframe")
    ema_fast: int = Field(default=20, ge=5, le=50, description="Fast EMA period")
    ema_slow: int = Field(default=50, ge=20, le=200, description="Slow EMA period")
    adx_threshold: int = Field(
        default=25,
        ge=10,
        le=50,
        description="ADX threshold for trend strength"
    )
    adx_period: int = Field(default=14, ge=5, le=50, description="ADX period")
    rsi_period: int = Field(default=14, ge=5, le=50, description="RSI period")
    rsi_long_threshold: int = Field(
        default=55,
        ge=50,
        le=70,
        description="RSI threshold for long entry"
    )
    rsi_short_threshold: int = Field(
        default=45,
        ge=30,
        le=50,
        description="RSI threshold for short entry"
    )
    volume_confirmation: bool = Field(
        default=True,
        description="Require volume confirmation"
    )
    
    @model_validator(mode="after")
    def validate_ema_periods(self) -> "MomentumStrategyConfig":
        if self.ema_fast >= self.ema_slow:
            raise ValueError(
                f"ema_fast ({self.ema_fast}) must be less than "
                f"ema_slow ({self.ema_slow})"
            )
        return self


class MeanReversionStrategyConfig(BaseConfig):
    """Mean reversion strategy configuration."""
    
    enabled: bool = Field(default=True, description="Enable strategy")
    weight: float = Field(
        default=0.25,
        ge=0,
        le=1,
        description="Strategy weight in portfolio"
    )
    timeframe: str = Field(default="15m", description="Candlestick timeframe")
    rsi_period: int = Field(default=14, ge=5, le=50, description="RSI period")
    rsi_oversold: int = Field(
        default=20,
        ge=5,
        le=40,
        description="RSI oversold threshold"
    )
    rsi_overbought: int = Field(
        default=80,
        ge=60,
        le=95,
        description="RSI overbought threshold"
    )
    bb_period: int = Field(default=20, ge=10, le=50, description="Bollinger Bands period")
    bb_std: float = Field(
        default=2.0,
        ge=1.0,
        le=3.0,
        description="Bollinger Bands standard deviation"
    )
    min_bb_width: float = Field(
        default=0.02,
        ge=0.01,
        le=0.1,
        description="Minimum Bollinger Band width"
    )


class BreakoutStrategyConfig(BaseConfig):
    """Breakout strategy configuration."""
    
    enabled: bool = Field(default=True, description="Enable strategy")
    weight: float = Field(
        default=0.25,
        ge=0,
        le=1,
        description="Strategy weight in portfolio"
    )
    timeframe: str = Field(default="1h", description="Candlestick timeframe")
    lookback_periods: int = Field(
        default=20,
        ge=5,
        le=100,
        description="Lookback periods for high/low"
    )
    min_breakout_pct: float = Field(
        default=1.5,
        ge=0.1,
        le=5,
        description="Minimum breakout percentage"
    )
    atr_multiplier: float = Field(
        default=2.0,
        ge=1,
        le=5,
        description="ATR multiplier for stop loss"
    )
    atr_period: int = Field(default=14, ge=5, le=50, description="ATR period")
    volume_surge_multiplier: float = Field(
        default=1.5,
        ge=1,
        le=5,
        description="Volume surge multiplier for confirmation"
    )


class FundingArbStrategyConfig(BaseConfig):
    """Funding arbitrage strategy configuration."""
    
    enabled: bool = Field(default=True, description="Enable strategy")
    weight: float = Field(
        default=0.15,
        ge=0,
        le=1,
        description="Strategy weight in portfolio"
    )
    min_funding_rate: float = Field(
        default=0.05,
        ge=0.01,
        le=0.2,
        description="Minimum funding rate percentage"
    )
    max_funding_rate: float = Field(
        default=0.5,
        ge=0.1,
        le=2,
        description="Maximum funding rate percentage (risk cap)"
    )
    holding_period_hours: int = Field(
        default=8,
        ge=1,
        le=24,
        description="Holding period in hours"
    )
    hedge_spot: bool = Field(
        default=False,
        description="Hedge with spot position"
    )


class StrategiesConfig(BaseConfig):
    """All strategies configuration."""
    
    momentum: MomentumStrategyConfig = Field(default_factory=MomentumStrategyConfig)
    mean_reversion: MeanReversionStrategyConfig = Field(default_factory=MeanReversionStrategyConfig)
    breakout: BreakoutStrategyConfig = Field(default_factory=BreakoutStrategyConfig)
    funding_arb: FundingArbStrategyConfig = Field(default_factory=FundingArbStrategyConfig)
    
    @model_validator(mode="after")
    def validate_strategy_weights(self) -> "StrategiesConfig":
        enabled_weights = []
        for strategy_name in ["momentum", "mean_reversion", "breakout", "funding_arb"]:
            strategy = getattr(self, strategy_name)
            if strategy.enabled:
                enabled_weights.append((strategy_name, strategy.weight))
        
        if enabled_weights:
            total = sum(w for _, w in enabled_weights)
            if not (0.99 <= total <= 1.01):
                logger.warning(
                    f"Strategy weights for enabled strategies sum to {total:.2f}, "
                    f"not 1.0. Weights will be normalized at runtime."
                )
        return self


# =============================================================================
# Optional Configurations
# =============================================================================

class TelegramConfig(BaseConfig):
    """Telegram notification configuration."""
    
    enabled: bool = Field(default=False, description="Enable Telegram notifications")
    bot_token_env: str = Field(
        default="TELEGRAM_BOT_TOKEN",
        description="Environment variable for bot token"
    )
    chat_id_env: str = Field(
        default="TELEGRAM_CHAT_ID",
        description="Environment variable for chat ID"
    )
    notify_on: list[str] = Field(
        default_factory=lambda: [
            "trade_open", "trade_close", "daily_summary", "error", "drawdown_warning"
        ],
        description="Events to notify on"
    )
    
    @property
    def bot_token(self) -> str | None:
        """Get bot token from environment."""
        return os.environ.get(self.bot_token_env)
    
    @property
    def chat_id(self) -> str | None:
        """Get chat ID from environment."""
        return os.environ.get(self.chat_id_env)


class HealthConfig(BaseConfig):
    """Health monitoring configuration."""
    
    enabled: bool = Field(default=True, description="Enable health endpoint")
    port: int = Field(
        default=8080,
        ge=1024,
        le=65535,
        description="Health endpoint port"
    )
    check_interval_seconds: int = Field(
        default=30,
        ge=5,
        le=300,
        description="Health check interval"
    )
    unhealthy_threshold: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Consecutive failures before unhealthy"
    )


# =============================================================================
# Main Configuration
# =============================================================================

class Config(BaseConfig):
    """Main configuration container for HLQuantBot v2.0."""
    
    system: SystemConfig = Field(default_factory=SystemConfig)
    hyperliquid: HyperliquidConfig = Field(default_factory=HyperliquidConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    services: ServicesConfig = Field(default_factory=ServicesConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    strategies: StrategiesConfig = Field(default_factory=StrategiesConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    
    # Metadata
    _loaded_at: datetime | None = None
    _config_path: Path | None = None
    
    @model_validator(mode="after")
    def validate_cross_config(self) -> "Config":
        """Validate cross-configuration dependencies."""
        # Ensure max_positions is consistent
        if self.services.capital_allocator.max_positions != self.risk.max_positions:
            logger.warning(
                f"max_positions mismatch: capital_allocator has "
                f"{self.services.capital_allocator.max_positions}, risk has "
                f"{self.risk.max_positions}. Using risk.max_positions."
            )
            self.services.capital_allocator.max_positions = self.risk.max_positions
        
        # Validate mode consistency
        if self.system.mode == "mainnet" and self.hyperliquid.testnet:
            raise ValueError(
                "Configuration error: system.mode is 'mainnet' but "
                "hyperliquid.testnet is True. Set hyperliquid.testnet to False "
                "for mainnet trading."
            )
        
        return self


# =============================================================================
# Configuration Loader
# =============================================================================

class ConfigLoader:
    """
    Configuration loader with hot-reload support.
    
    Features:
    - Load from YAML file
    - Environment variable resolution
    - Validation with Pydantic
    - Hot-reload with callbacks
    - Version tracking
    """
    
    def __init__(self, config_path: str | Path):
        """
        Initialize config loader.
        
        Args:
            config_path: Path to YAML configuration file
        """
        self.config_path = Path(config_path)
        self._config: Config | None = None
        self._reload_callbacks: list[Callable[[Config], None]] = []
        self._last_modified: float = 0
    
    def load(self) -> Config:
        """
        Load configuration from YAML file.
        
        Returns:
            Validated Config object
            
        Raises:
            FileNotFoundError: If config file doesn't exist
            yaml.YAMLError: If YAML parsing fails
            pydantic.ValidationError: If validation fails
        """
        if not self.config_path.exists():
            raise FileNotFoundError(
                f"Configuration file not found: {self.config_path}. "
                f"Create it or specify a valid path."
            )
        
        logger.info(f"Loading configuration from {self.config_path}")
        
        with open(self.config_path, "r") as f:
            raw_config = yaml.safe_load(f)
        
        if raw_config is None:
            raw_config = {}
        
        # Resolve environment variables
        resolved_config = resolve_env_vars(raw_config)
        
        # Validate with Pydantic
        self._config = Config(**resolved_config)
        self._config._loaded_at = datetime.now()
        self._config._config_path = self.config_path
        self._last_modified = self.config_path.stat().st_mtime
        
        logger.info(
            f"Configuration loaded successfully: mode={self._config.system.mode}, "
            f"testnet={self._config.hyperliquid.testnet}"
        )
        
        return self._config
    
    @property
    def config(self) -> Config:
        """Get current configuration, loading if necessary."""
        if self._config is None:
            return self.load()
        return self._config
    
    def reload(self) -> Config:
        """
        Reload configuration and notify callbacks.
        
        Returns:
            Newly loaded Config object
        """
        old_config = self._config
        new_config = self.load()
        
        # Notify callbacks
        for callback in self._reload_callbacks:
            try:
                callback(new_config)
            except Exception as e:
                logger.error(f"Config reload callback failed: {e}")
        
        logger.info("Configuration reloaded successfully")
        return new_config
    
    def check_for_changes(self) -> bool:
        """
        Check if config file has been modified.
        
        Returns:
            True if file has been modified since last load
        """
        if not self.config_path.exists():
            return False
        
        current_mtime = self.config_path.stat().st_mtime
        return current_mtime > self._last_modified
    
    def on_reload(self, callback: Callable[[Config], None]) -> None:
        """
        Register a callback for configuration reloads.
        
        Args:
            callback: Function to call with new config on reload
        """
        self._reload_callbacks.append(callback)
    
    def reload_if_changed(self) -> Config | None:
        """
        Reload configuration if file has been modified.
        
        Returns:
            New Config if reloaded, None if unchanged
        """
        if self.check_for_changes():
            return self.reload()
        return None


# =============================================================================
# Convenience Functions
# =============================================================================

_default_loader: ConfigLoader | None = None


def load_config(path: str | Path = "simple_bot/config/intelligent_bot.yaml") -> Config:
    """
    Load configuration from file.
    
    This is the main entry point for configuration loading.
    
    Args:
        path: Path to YAML configuration file
        
    Returns:
        Validated Config object
        
    Example:
        >>> from simple_bot.config import load_config
        >>> config = load_config()
        >>> print(config.system.mode)
        testnet
    """
    global _default_loader
    _default_loader = ConfigLoader(path)
    return _default_loader.load()


def get_config() -> Config:
    """
    Get the currently loaded configuration.
    
    Returns:
        Current Config object
        
    Raises:
        RuntimeError: If configuration hasn't been loaded yet
    """
    if _default_loader is None or _default_loader._config is None:
        raise RuntimeError(
            "Configuration not loaded. Call load_config() first."
        )
    return _default_loader.config


def reload_config() -> Config:
    """
    Reload the configuration from file.
    
    Returns:
        Newly loaded Config object
        
    Raises:
        RuntimeError: If configuration hasn't been loaded yet
    """
    if _default_loader is None:
        raise RuntimeError(
            "Configuration not loaded. Call load_config() first."
        )
    return _default_loader.reload()
