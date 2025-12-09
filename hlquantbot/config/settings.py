"""Settings management using Pydantic and YAML config files."""

import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Any

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings

from ..core.enums import Environment, StrategyId


# Base paths
BASE_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = BASE_DIR / "hlquantbot" / "config"


class HyperliquidConfig(BaseModel):
    """Hyperliquid API configuration."""
    testnet_url: str = "https://api.hyperliquid-testnet.xyz"
    mainnet_url: str = "https://api.hyperliquid.xyz"
    testnet_ws_url: str = "wss://api.hyperliquid-testnet.xyz/ws"
    mainnet_ws_url: str = "wss://api.hyperliquid.xyz/ws"

    # Rate limits (conservative)
    max_requests_per_minute: int = 100
    max_orders_per_second: int = 10


class DatabaseConfig(BaseModel):
    """Database configuration."""
    host: str = "postgres"  # Docker service name
    port: int = 5432  # Production default
    name: str = "trader_db"
    user: str = "trader"
    password: str = "password"

    @property
    def url(self) -> str:
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"

    @property
    def async_url(self) -> str:
        return f"postgresql+asyncpg://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"


class TelegramConfig(BaseModel):
    """Telegram alerting configuration."""
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""
    alert_on_trade: bool = True
    alert_on_error: bool = True
    alert_on_daily_summary: bool = True
    alert_on_circuit_breaker: bool = True


class OpenAIConfig(BaseModel):
    """OpenAI/DeepSeek API configuration."""
    enabled: bool = True
    api_key: str = ""
    model: str = "deepseek-reasoner"  # DeepSeek V3.2-Speciale
    # Endpoint standard DeepSeek API
    base_url: str = "https://api.deepseek.com"
    regime_detection_interval_minutes: int = 15
    param_tuning_interval_hours: int = 24
    max_tokens: int = 8000  # DeepSeek limit
    temperature: float = 0.3


class TemporalRiskLevelConfig(BaseModel):
    """Configuration for a single temporal kill-switch level."""
    window_seconds: int
    max_drawdown_pct: Decimal
    cooldown_seconds: int

    class Config:
        arbitrary_types_allowed = True


class TemporalRiskConfig(BaseModel):
    """Temporal kill-switch configuration with multiple levels.

    Updated for aggressive P&L targeting with wider thresholds
    and shorter cooldowns to maintain trading activity.
    """
    enabled: bool = True

    # Level 1: Rapid (5 min window) - was 30s/0.7%/15min
    level_1: Optional[TemporalRiskLevelConfig] = TemporalRiskLevelConfig(
        window_seconds=300,  # 5 min (was 30s)
        max_drawdown_pct=Decimal("0.015"),  # 1.5% (was 0.7%)
        cooldown_seconds=300,  # 5 min (was 15 min)
    )

    # Level 2: Medium (30 min window) - was 10min/2%/1h
    level_2: Optional[TemporalRiskLevelConfig] = TemporalRiskLevelConfig(
        window_seconds=1800,  # 30 min (was 10 min)
        max_drawdown_pct=Decimal("0.04"),  # 4% (was 2%)
        cooldown_seconds=1800,  # 30 min (was 1h)
    )

    # Level 3: Slow (4h window) - was 1h/4.5%/6h
    level_3: Optional[TemporalRiskLevelConfig] = TemporalRiskLevelConfig(
        window_seconds=14400,  # 4 hours (was 1h)
        max_drawdown_pct=Decimal("0.045"),  # 4.5% (reduced from 10%)
        cooldown_seconds=21600,  # 6h (reduced from 24h)
    )

    class Config:
        arbitrary_types_allowed = True


class RiskConfig(BaseModel):
    """Risk management configuration."""
    # Portfolio level - AGGRESSIVE for P&L maximization
    max_portfolio_leverage: Decimal = Decimal("8.0")  # 8x (was 4x)
    max_daily_loss_pct: Decimal = Decimal("0.10")  # 10%
    max_total_drawdown_pct: Decimal = Decimal("0.35")  # 35% (was 50%)

    # Per trade - AGGRESSIVE for HFT
    max_risk_per_trade_pct: Decimal = Decimal("0.012")  # 1.2% (was 0.7%)
    default_leverage: Decimal = Decimal("5.0")  # 5x (was 3x)
    max_position_leverage: Decimal = Decimal("25.0")  # 25x for HFT micro-positions

    # Per asset
    max_exposure_per_asset_pct: Decimal = Decimal("0.65")  # 65% (was 40%)

    # Position limits
    max_open_positions: int = 15  # Max concurrent positions

    # Circuit breaker (hard stop - exits process)
    circuit_breaker_enabled: bool = True
    auto_restart_after_circuit_breaker: bool = False  # MUST be manual

    # Temporal kill-switch (soft stop - cooldown then resume)
    temporal_risk: Optional[TemporalRiskConfig] = TemporalRiskConfig()

    class Config:
        arbitrary_types_allowed = True


class TradeParamsConfig(BaseModel):
    """Standardized trade parameters for fee-aware P&L optimization.

    Updated for aggressive P&L targeting per Context Pack requirements:
    - min_tp_pct = 0.35% (was too low before)
    - max_sl_pct = 0.20% (tighter stops)
    - RR >= 1.75:1
    """
    # TP/SL thresholds - AGGRESSIVE for P&L maximization
    min_tp_pct: Decimal = Decimal("0.0008")   # 0.08% minimum TP (lowered for HFT)
    max_sl_pct: Decimal = Decimal("0.005")    # 0.50% maximum SL (raised for volatility)

    # Fee structure (Hyperliquid)
    maker_fee_pct: Decimal = Decimal("0.0002")  # 0.02% maker
    taker_fee_pct: Decimal = Decimal("0.0005")  # 0.05% taker

    # Risk/Reward targets
    min_risk_reward: Decimal = Decimal("1.75")  # 1.75:1 minimum RR (TP/SL)

    class Config:
        arbitrary_types_allowed = True


class TradingModeConfig(BaseModel):
    """Trading mode for dry run testing."""
    enabled: bool = True           # False = dry run (no real orders)
    size_multiplier: Decimal = Decimal("1.0")  # 0.1 = 10% of normal size
    max_leverage_override: Optional[Decimal] = None  # Override max leverage

    class Config:
        arbitrary_types_allowed = True


class AggressionLevelMultipliers(BaseModel):
    """Multipliers for a single aggression level."""
    risk_mult: Decimal = Decimal("1.0")
    leverage_mult: Decimal = Decimal("1.0")

    class Config:
        arbitrary_types_allowed = True


class AggressionProfileConfig(BaseModel):
    """Aggression level profiles for dynamic risk adjustment."""
    paused: AggressionLevelMultipliers = AggressionLevelMultipliers(
        risk_mult=Decimal("0"), leverage_mult=Decimal("0")
    )
    conservative: AggressionLevelMultipliers = AggressionLevelMultipliers(
        risk_mult=Decimal("0.5"), leverage_mult=Decimal("0.5")
    )
    normal: AggressionLevelMultipliers = AggressionLevelMultipliers(
        risk_mult=Decimal("1.0"), leverage_mult=Decimal("1.0")
    )
    aggressive: AggressionLevelMultipliers = AggressionLevelMultipliers(
        risk_mult=Decimal("1.5"), leverage_mult=Decimal("1.5")
    )
    very_aggressive: AggressionLevelMultipliers = AggressionLevelMultipliers(
        risk_mult=Decimal("2.0"), leverage_mult=Decimal("2.0")
    )


class SymbolConfig(BaseModel):
    """Configuration for a trading symbol."""
    name: str
    enabled: bool = True
    max_leverage: Decimal = Decimal("4.0")
    max_position_pct: Decimal = Decimal("0.50")  # Max % of equity
    min_size: Decimal = Decimal("0.001")
    size_decimals: int = 3
    price_decimals: int = 1

    class Config:
        arbitrary_types_allowed = True


class StrategyConfigBase(BaseModel):
    """Base strategy configuration."""
    enabled: bool = True
    allocation_pct: Decimal = Decimal("0.33")  # % of equity allocated
    max_positions: int = 1  # Max concurrent positions per symbol
    symbols: List[str] = ["BTC", "ETH", "SOL", "DOGE", "AVAX", "LINK", "ARB", "XRP", "ADA", "APT", "SUI", "INJ"]
    signal_cooldown_seconds: int = 300  # Min seconds between signals (default 5 min)

    class Config:
        arbitrary_types_allowed = True


class FundingBiasConfig(StrategyConfigBase):
    """Funding Bias strategy configuration."""
    # Thresholds
    funding_threshold_high: Decimal = Decimal("0.0003")  # 0.03%
    funding_threshold_low: Decimal = Decimal("-0.0003")
    predicted_funding_weight: Decimal = Decimal("0.3")  # Weight for predicted vs current

    # Position management
    hold_through_funding: bool = True  # Hold position through funding payment
    max_hold_hours: int = 24

    # Entry filters
    min_open_interest_usd: Decimal = Decimal("10000000")  # $10M
    max_volatility_atr_pct: Decimal = Decimal("0.03")  # 3% ATR

    allocation_pct: Decimal = Decimal("0.30")


class LiquidationClusterConfig(StrategyConfigBase):
    """Liquidation Cluster strategy configuration."""
    # OI analysis
    oi_change_threshold_pct: Decimal = Decimal("0.05")  # 5% OI change
    oi_lookback_bars: int = 12  # Bars to calculate OI change

    # Price level detection
    swing_lookback_bars: int = 20
    support_resistance_touches: int = 2
    level_proximity_pct: Decimal = Decimal("0.005")  # 0.5% from level

    # Entry conditions
    min_rr_ratio: Decimal = Decimal("2.0")  # Minimum risk/reward
    volume_surge_multiplier: Decimal = Decimal("1.5")  # Volume vs avg

    # Risk
    stop_loss_atr_multiplier: Decimal = Decimal("1.0")
    take_profit_atr_multiplier: Decimal = Decimal("2.5")

    allocation_pct: Decimal = Decimal("0.40")

    # --- ENHANCED INDICATORS (v2) ---

    # Order book imbalance
    orderbook_imbalance_threshold: Decimal = Decimal("0.25")  # Min bid/ask imbalance

    # OI velocity (acceleration)
    oi_velocity_threshold: Decimal = Decimal("0.02")  # Min OI acceleration
    oi_spike_multiplier: Decimal = Decimal("2.0")  # OI change > 2x avg = spike

    # Market structure
    min_structure_score: Decimal = Decimal("0.4")  # Min score for entry
    consolidation_bars: int = 10  # Bars for consolidation detection

    # Funding timing filter
    avoid_funding_settlement_minutes: int = 15  # Skip N min before/after funding

    # Enhanced confidence weights
    funding_extreme_threshold: Decimal = Decimal("0.0005")  # 0.05% = extreme funding


class VolatilityExpansionConfig(StrategyConfigBase):
    """Volatility Expansion strategy configuration."""
    # Compression detection
    bb_period: int = 20
    bb_std: Decimal = Decimal("2.0")
    bb_width_threshold: Decimal = Decimal("0.02")  # 2% width
    atr_period: int = 14
    atr_percentile_threshold: int = 20  # Below 20th percentile

    # Breakout confirmation
    breakout_atr_multiplier: Decimal = Decimal("0.5")  # Price move > 0.5 ATR
    volume_confirmation_multiplier: Decimal = Decimal("1.3")

    # Position management
    stop_loss_atr_multiplier: Decimal = Decimal("0.75")
    take_profit_range_multiplier: Decimal = Decimal("2.0")  # 2x compression range

    # Timing
    active_hours_utc: List[int] = [13, 14, 15, 16, 17, 18, 19, 20, 21]  # US/EU overlap

    allocation_pct: Decimal = Decimal("0.30")


# =============================================================================
# HFT STRATEGIES (Phase C)
# =============================================================================

class HFTStrategyConfigBase(StrategyConfigBase):
    """Base configuration for HFT strategies."""
    # HFT-specific settings
    order_timeout_seconds: int = 2  # Auto-cancel after 2s
    max_position_hold_seconds: int = 60  # Max hold time
    min_signal_interval_ms: int = 500  # Min ms between signals (0.5 seconds)

    # Smaller allocations for HFT (many small trades)
    allocation_pct: Decimal = Decimal("0.15")  # 15% per HFT strategy
    signal_cooldown_seconds: int = 1  # Very short cooldown for HFT


class TimeframeParams(BaseModel):
    """Parameters specific to a timeframe."""
    deviation_threshold_pct: Decimal = Decimal("0.001")
    max_deviation_pct: Decimal = Decimal("0.003")
    take_profit_pct: Decimal = Decimal("0.002")
    stop_loss_pct: Decimal = Decimal("0.003")
    vwap_periods: int = 20

    class Config:
        arbitrary_types_allowed = True


class BreakoutTimeframeParams(BaseModel):
    """Breakout parameters specific to a timeframe."""
    range_threshold_pct: Decimal = Decimal("0.005")
    breakout_threshold_pct: Decimal = Decimal("0.002")
    take_profit_pct: Decimal = Decimal("0.003")
    stop_loss_pct: Decimal = Decimal("0.004")
    consolidation_bars: int = 10

    class Config:
        arbitrary_types_allowed = True


class MMRHFTConfig(HFTStrategyConfigBase):
    """MMR-HFT (Micro Mean Reversion) strategy configuration."""
    enabled: bool = True
    symbols: List[str] = ["BTC", "ETH", "SOL"]

    # Primary timeframe to use
    primary_timeframe: str = "M5"  # M1, M5, or M15

    # Timeframe-specific parameters - Context Pack 2.0 (CONSERVATIVE)
    # TP >= 0.35%, SL <= 0.15%, RR >= 1.5
    timeframe_m1: TimeframeParams = TimeframeParams(
        deviation_threshold_pct=Decimal("0.0005"),  # 0.05% - Context Pack 2.0
        max_deviation_pct=Decimal("0.003"),         # 0.3% max
        take_profit_pct=Decimal("0.0035"),          # 0.35% minimum
        stop_loss_pct=Decimal("0.0015"),            # 0.15% maximum
        vwap_periods=20,
    )
    timeframe_m5: TimeframeParams = TimeframeParams(
        deviation_threshold_pct=Decimal("0.0005"),  # 0.05% - Context Pack 2.0
        max_deviation_pct=Decimal("0.005"),         # 0.5% max
        take_profit_pct=Decimal("0.0035"),          # 0.35% minimum
        stop_loss_pct=Decimal("0.0015"),            # 0.15% maximum
        vwap_periods=15,
    )
    timeframe_m15: TimeframeParams = TimeframeParams(
        deviation_threshold_pct=Decimal("0.0005"),  # 0.05% - Context Pack 2.0
        max_deviation_pct=Decimal("0.008"),         # 0.8% max
        take_profit_pct=Decimal("0.0045"),          # 0.45% for wider TF
        stop_loss_pct=Decimal("0.002"),             # 0.20% max
        vwap_periods=12,
    )

    # Legacy (fallback) - Context Pack 2.0 values
    timeframe_seconds: int = 5
    deviation_threshold_pct: Decimal = Decimal("0.0005")  # 0.05%
    max_deviation_pct: Decimal = Decimal("0.005")         # 0.5%

    # Risk/Reward - Context Pack 2.0
    take_profit_pct: Decimal = Decimal("0.0035")  # 0.35% minimum
    stop_loss_pct: Decimal = Decimal("0.0015")    # 0.15% maximum

    # Leverage
    default_leverage: Decimal = Decimal("10.0")
    max_leverage: Decimal = Decimal("15.0")

    # Position sizing
    max_position_pct: Decimal = Decimal("0.02")  # 2% of equity per trade


class MicroBreakoutConfig(HFTStrategyConfigBase):
    """Micro-Breakout 2.0 strategy configuration.

    UPDATED per Context Pack:
    - Compression detection: compression_ratio < 0.7
    - Volume >= 2x average
    - ΔOI >= 5%
    - ATR-based TP/SL: TP = 1.2 × ATR_5, SL = 0.6 × ATR_5 (RR ≈ 2)
    """
    enabled: bool = True
    symbols: List[str] = ["BTC", "ETH", "SOL"]

    # Primary timeframe to use
    primary_timeframe: str = "M5"  # M1, M5, or M15

    # Timeframe-specific parameters - UPDATED with tighter SL
    timeframe_m1: BreakoutTimeframeParams = BreakoutTimeframeParams(
        range_threshold_pct=Decimal("0.0005"),  # 0.05% range = compressed
        breakout_threshold_pct=Decimal("0.0003"),  # 0.03% beyond range
        take_profit_pct=Decimal("0.0040"),  # 0.40% (ATR-based)
        stop_loss_pct=Decimal("0.0020"),  # 0.20% (ATR-based, RR=2)
        consolidation_bars=15,
    )
    timeframe_m5: BreakoutTimeframeParams = BreakoutTimeframeParams(
        range_threshold_pct=Decimal("0.002"),  # 0.2% range = compressed
        breakout_threshold_pct=Decimal("0.001"),  # 0.1% beyond range
        take_profit_pct=Decimal("0.0050"),  # 0.50% (ATR-based)
        stop_loss_pct=Decimal("0.0025"),  # 0.25% (ATR-based, RR=2)
        consolidation_bars=10,
    )
    timeframe_m15: BreakoutTimeframeParams = BreakoutTimeframeParams(
        range_threshold_pct=Decimal("0.005"),  # 0.5% range = compressed
        breakout_threshold_pct=Decimal("0.002"),  # 0.2% beyond range
        take_profit_pct=Decimal("0.0070"),  # 0.70% (ATR-based)
        stop_loss_pct=Decimal("0.0035"),  # 0.35% (ATR-based, RR=2)
        consolidation_bars=8,
    )

    # Compression detection - NEW
    compression_ratio_threshold: Decimal = Decimal("0.7")  # < 0.7 = consolidation

    # Volume/OI confirmation - UPDATED per Context Pack
    volume_surge_multiplier: Decimal = Decimal("2.0")  # 2x average (was 1.5x)
    oi_change_threshold_pct: Decimal = Decimal("0.01")  # ΔOI >= 1% (reduced from 5%)

    # Legacy (fallback)
    timeframe_seconds: int = 15
    consolidation_bars: int = 10
    range_threshold_pct: Decimal = Decimal("0.002")
    breakout_threshold_pct: Decimal = Decimal("0.001")

    # Risk/Reward - UPDATED per Context Pack (RR ≈ 2)
    take_profit_pct: Decimal = Decimal("0.0050")  # 0.50%
    stop_loss_pct: Decimal = Decimal("0.0025")  # 0.25%

    # ATR multipliers for dynamic TP/SL
    atr_tp_multiplier: Decimal = Decimal("1.2")  # TP = 1.2 × ATR_5
    atr_sl_multiplier: Decimal = Decimal("0.6")  # SL = 0.6 × ATR_5

    # Leverage
    default_leverage: Decimal = Decimal("10.0")
    max_leverage: Decimal = Decimal("15.0")

    # Position sizing
    max_position_pct: Decimal = Decimal("0.02")  # 2% of equity per trade


class PairTradingConfig(HFTStrategyConfigBase):
    """Pair Trading strategy configuration."""
    enabled: bool = True

    # Pairs to trade (long first, short second when spread diverges)
    pairs: List[List[str]] = [["BTC", "ETH"], ["ETH", "SOL"]]

    # Correlation/spread parameters
    lookback_seconds: int = 300  # 5 min for spread calculation
    zscore_entry_threshold: Decimal = Decimal("1.5")  # Enter when z > 1.5 (reduced from 2.0)
    zscore_exit_threshold: Decimal = Decimal("0.5")  # Exit when z < 0.5
    rebalance_interval_seconds: int = 60  # Rebalance every 1 min

    # Risk/Reward
    take_profit_spread_pct: Decimal = Decimal("0.002")  # 0.2% spread convergence
    stop_loss_spread_pct: Decimal = Decimal("0.003")  # 0.3% spread divergence

    # Leverage (per leg)
    default_leverage: Decimal = Decimal("15.0")
    max_leverage: Decimal = Decimal("20.0")

    # Position sizing (per leg)
    max_position_pct: Decimal = Decimal("0.015")  # 1.5% per leg

    # Override base settings
    symbols: List[str] = ["BTC", "ETH", "SOL"]


class LiquidationSnipingConfig(HFTStrategyConfigBase):
    """Liquidation Sniping 2.0 strategy configuration.

    CORE STRATEGY per Context Pack - triggers only when:
    - OI spike > 10%
    - Price spike > ±0.35% in < 60 seconds
    - Volume spike > 150% of average

    TP = 0.40-0.60%, SL = 0.20% max
    """
    enabled: bool = True
    symbols: List[str] = ["BTC", "ETH"]

    # Detection parameters - UPDATED per Context Pack
    oi_spike_threshold_pct: Decimal = Decimal("0.10")  # 10% OI change (was 2%)
    oi_spike_window_seconds: int = 60  # Detect OI spike in 1 min
    price_spike_threshold_pct: Decimal = Decimal("0.0035")  # 0.35% price move (was 0.5%)
    volume_spike_threshold_pct: Decimal = Decimal("1.2")  # 120% of average volume (reduced from 150%)

    # Entry timing
    entry_delay_ms: int = 100  # Wait 100ms after spike detection

    # Risk/Reward - UPDATED per Context Pack
    take_profit_pct: Decimal = Decimal("0.005")  # 0.50% (range 0.40-0.60%)
    stop_loss_pct: Decimal = Decimal("0.002")  # 0.20% max

    # Leverage (aggressive)
    default_leverage: Decimal = Decimal("20.0")
    max_leverage: Decimal = Decimal("25.0")

    # Position sizing
    max_position_pct: Decimal = Decimal("0.01")  # 1% per trade

    # Position hold
    max_position_hold_seconds: int = 30  # Very short hold


class MomentumScalpingConfig(HFTStrategyConfigBase):
    """Momentum Scalping strategy configuration - Trend following for HFT."""
    enabled: bool = True
    symbols: List[str] = ["BTC", "ETH", "SOL"]
    allocation_pct: Decimal = Decimal("0.20")

    # Indicators
    min_rsi_up: Decimal = Decimal("60")  # Min RSI for long
    max_rsi_down: Decimal = Decimal("40")  # Max RSI for short
    min_volume_ratio: Decimal = Decimal("1.2")  # Volume vs 20-bar avg
    ema_fast: int = 20  # Fast EMA period
    ema_slow: int = 50  # Slow EMA period

    # TP/SL
    take_profit_pct: Decimal = Decimal("0.004")   # 0.4%
    stop_loss_pct: Decimal = Decimal("0.0018")    # 0.18%

    # Leverage (moderate for trend following)
    default_leverage: Decimal = Decimal("12.0")
    max_leverage: Decimal = Decimal("15.0")

    # Position sizing
    max_position_pct: Decimal = Decimal("0.015")  # 1.5% per trade


class HFTStrategiesConfig(BaseModel):
    """All HFT strategies configuration."""
    mmr_hft: MMRHFTConfig = MMRHFTConfig()
    micro_breakout: MicroBreakoutConfig = MicroBreakoutConfig()
    pair_trading: PairTradingConfig = PairTradingConfig()
    liquidation_sniping: LiquidationSnipingConfig = LiquidationSnipingConfig()
    momentum_scalping: MomentumScalpingConfig = MomentumScalpingConfig()


class StrategiesConfig(BaseModel):
    """All strategies configuration."""
    # Legacy strategies (to be removed)
    funding_bias: FundingBiasConfig = FundingBiasConfig()
    liquidation_cluster: LiquidationClusterConfig = LiquidationClusterConfig()
    volatility_expansion: VolatilityExpansionConfig = VolatilityExpansionConfig()

    # HFT strategies (Phase C)
    hft: HFTStrategiesConfig = HFTStrategiesConfig()


class Settings(BaseSettings):
    """Main application settings."""

    # Environment
    environment: Environment = Environment.TESTNET

    # API Keys (from .env)
    hl_private_key: str = Field("", alias="PRIVATE_KEY")
    hl_wallet_address: str = Field("", alias="WALLET_ADDRESS")
    deepseek_api_key: str = Field("", alias="DEEPSEEK_API_KEY")
    telegram_bot_token: str = Field("", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field("", alias="TELEGRAM_CHAT_ID")

    # Database (can be overridden by env)
    db_host: str = Field("postgres", alias="DB_HOST")
    db_port: int = Field(5432, alias="DB_PORT")
    db_name: str = Field("trader_db", alias="DB_NAME")
    db_user: str = Field("trader", alias="DB_USER")
    db_password: str = Field("password", alias="DB_PASSWORD")

    # Components (loaded from YAML)
    hyperliquid: HyperliquidConfig = HyperliquidConfig()
    database: DatabaseConfig = DatabaseConfig()
    telegram: TelegramConfig = TelegramConfig()
    openai: OpenAIConfig = OpenAIConfig()
    risk: RiskConfig = RiskConfig()
    strategies: StrategiesConfig = StrategiesConfig()
    symbols: Dict[str, SymbolConfig] = {}

    # New components for P&L optimization
    trade_params: TradeParamsConfig = TradeParamsConfig()
    trading_mode: TradingModeConfig = TradingModeConfig()
    aggression_profile: AggressionProfileConfig = AggressionProfileConfig()

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._load_yaml_config()
        self._sync_env_to_components()

    def _load_yaml_config(self):
        """Load configuration from YAML files.

        Supports CONFIG_PROFILE env var:
        - "default" or "" -> config.yaml (conservative)
        - "aggressive" -> config_aggressive.yaml (Phase B/C testing)
        """
        profile = os.getenv("CONFIG_PROFILE", "default").lower()

        if profile == "aggressive":
            config_file = CONFIG_DIR / "config_aggressive.yaml"
        else:
            config_file = CONFIG_DIR / "config.yaml"

        if config_file.exists():
            with open(config_file) as f:
                yaml_config = yaml.safe_load(f) or {}

            # Update components from YAML
            if "risk" in yaml_config:
                self.risk = RiskConfig(**yaml_config["risk"])
            if "strategies" in yaml_config:
                strat_config = yaml_config["strategies"]

                # Parse HFT strategies if present
                hft_config = HFTStrategiesConfig()
                if "hft" in strat_config:
                    hft_yaml = strat_config["hft"]
                    hft_config = HFTStrategiesConfig(
                        mmr_hft=MMRHFTConfig(**hft_yaml.get("mmr_hft", {})),
                        micro_breakout=MicroBreakoutConfig(**hft_yaml.get("micro_breakout", {})),
                        pair_trading=PairTradingConfig(**hft_yaml.get("pair_trading", {})),
                        liquidation_sniping=LiquidationSnipingConfig(**hft_yaml.get("liquidation_sniping", {})),
                        momentum_scalping=MomentumScalpingConfig(**hft_yaml.get("momentum_scalping", {})),
                    )

                self.strategies = StrategiesConfig(
                    funding_bias=FundingBiasConfig(**strat_config.get("funding_bias", {})),
                    liquidation_cluster=LiquidationClusterConfig(**strat_config.get("liquidation_cluster", {})),
                    volatility_expansion=VolatilityExpansionConfig(**strat_config.get("volatility_expansion", {})),
                    hft=hft_config,
                )
            if "telegram" in yaml_config:
                self.telegram = TelegramConfig(**yaml_config["telegram"])
            if "openai" in yaml_config:
                self.openai = OpenAIConfig(**yaml_config["openai"])

            # Load symbols from config.yaml
            if "symbols" in yaml_config:
                symbols_config = yaml_config["symbols"]
                self.symbols = {
                    name: SymbolConfig(name=name, **config) if isinstance(config, dict) else SymbolConfig(name=name, enabled=True)
                    for name, config in symbols_config.items()
                }

        # Fallback to symbols.yaml if not loaded yet
        if not self.symbols:
            symbols_file = CONFIG_DIR / "symbols.yaml"
            if symbols_file.exists():
                with open(symbols_file) as f:
                    symbols_yaml = yaml.safe_load(f) or {}
                symbols_config = symbols_yaml.get("symbols", {})
                if symbols_config:
                    self.symbols = {
                        name: SymbolConfig(name=name, **config) if isinstance(config, dict) else SymbolConfig(name=name, enabled=True)
                        for name, config in symbols_config.items()
                    }

    def _sync_env_to_components(self):
        """Sync environment variables to component configs."""
        # Database
        self.database = DatabaseConfig(
            host=self.db_host,
            port=self.db_port,
            name=self.db_name,
            user=self.db_user,
            password=self.db_password,
        )

        # DeepSeek
        if self.deepseek_api_key:
            self.openai.api_key = self.deepseek_api_key

        # Telegram
        if self.telegram_bot_token:
            self.telegram.bot_token = self.telegram_bot_token
            self.telegram.chat_id = self.telegram_chat_id
            self.telegram.enabled = bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def is_testnet(self) -> bool:
        return self.environment == Environment.TESTNET

    @property
    def hl_base_url(self) -> str:
        if self.is_testnet:
            return self.hyperliquid.testnet_url
        return self.hyperliquid.mainnet_url

    @property
    def hl_ws_url(self) -> str:
        if self.is_testnet:
            return self.hyperliquid.testnet_ws_url
        return self.hyperliquid.mainnet_ws_url

    @property
    def active_symbols(self) -> List[str]:
        """Get list of enabled trading symbols."""
        return [name for name, cfg in self.symbols.items() if cfg.enabled]

    def get_strategy_config(self, strategy_id: StrategyId) -> StrategyConfigBase:
        """Get configuration for a specific strategy."""
        mapping = {
            # Legacy strategies
            StrategyId.FUNDING_BIAS: self.strategies.funding_bias,
            StrategyId.LIQUIDATION_CLUSTER: self.strategies.liquidation_cluster,
            StrategyId.VOLATILITY_EXPANSION: self.strategies.volatility_expansion,
            # HFT strategies
            StrategyId.MMR_HFT: self.strategies.hft.mmr_hft,
            StrategyId.MICRO_BREAKOUT: self.strategies.hft.micro_breakout,
            StrategyId.PAIR_TRADING: self.strategies.hft.pair_trading,
            StrategyId.LIQUIDATION_SNIPING: self.strategies.hft.liquidation_sniping,
            StrategyId.MOMENTUM_SCALPING: self.strategies.hft.momentum_scalping,
        }
        return mapping[strategy_id]


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
