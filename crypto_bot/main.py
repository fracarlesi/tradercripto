#!/usr/bin/env python3
"""
HLQuantBot v5 - FLAG-Trader LLM Decisor
=========================================

Trading system with FLAG-Trader (LLM + PPO) trade decisions and strict risk controls.

Architecture:
    Candles -> FlagTraderModel.get_action(prompt) -> Buy/Sell/Hold -> RiskManager -> Execution

This orchestrator:
1. Loads configuration from trading.yaml
2. Initializes core services (message bus, risk, execution)
3. Loads pre-trained FLAG-Trader model (Qwen 0.5B + policy/value heads)
4. Scans top-N assets by volume, builds prompts from candle data
5. Executes actionable decisions through risk manager pipeline

Author: Francesco Carlesi
"""

from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()

import asyncio
import logging
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from .core.enums import Topic
from .core.models import Direction, Setup, SetupType

# Services
from .services.message_bus import MessageBus
from .services.market_state import (
    MarketStateService,
    MarketStateConfig,
    create_market_state_service,
)
from .services.risk_manager import (
    RiskConfig,
    create_risk_manager,
)
from .services.execution_engine import (
    ExecutionEngineService,
)
from .services.realtime_monitor import RealtimeMonitorService
from .services.telegram_service import TelegramService
from .services.whatsapp_service import WhatsAppService

# FLAG-Trader
from .flag_trader.agent import FlagTraderAgent, FlagTraderConfig, TradeDecision
from .flag_trader.model import FlagTraderModel
from .flag_trader.prompt import PromptBuilder
from .flag_trader.trade_logger import FlagTradeLogger

# API Client
from .api.hyperliquid import HyperliquidClient


# =============================================================================
# Logging
# =============================================================================

def setup_logging(log_level: str = "INFO") -> logging.Logger:
    """Configure logging for the bot."""
    level = getattr(logging, log_level.upper(), logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    # File handler
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    env_suffix = "_paper" if os.getenv("ENVIRONMENT", "mainnet").lower() == "testnet" else ""
    file_handler = logging.FileHandler(log_dir / f"bot{env_suffix}.log")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # Reduce noise
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return logging.getLogger("hlquantbot")


logger = logging.getLogger("hlquantbot.main")


# =============================================================================
# Configuration Loader
# =============================================================================

@dataclass
class ConservativeConfig:
    """Configuration for trading system."""

    # Universe
    assets: List[str]
    universe_mode: str  # "all" or "manual"
    min_volume_24h: float
    exclude_symbols: List[str]

    # Timeframes
    primary_timeframe: str
    bars_to_fetch: int
    scan_interval_minutes: int

    # Risk (dynamic, scaled by LLM confidence)
    min_per_trade_pct: float
    max_per_trade_pct: float
    max_exposure_pct: float
    max_position_pct: float
    min_leverage: int
    max_leverage: int

    # Stops
    initial_atr_mult: float
    trailing_atr_mult: float
    minimal_roi: Dict[str, float]
    stop_loss_pct: float
    take_profit_pct: float
    breakeven_threshold_pct: float
    max_hold_hours: float

    # Regime (for MarketStateService indicator computation)
    trend_adx_entry_min: float
    trend_adx_exit_min: float
    range_adx_max: float
    choppiness_range_min: float
    regime_confirmation_bars: int
    regime_exit_grace_minutes: int

    # Execution
    prefer_limit: bool
    max_slippage_pct: float
    max_spread_pct: float
    entry_mode: str  # "taker" (default) or "maker" (post-only)
    limit_timeout_seconds: int
    maker_reprice_interval_seconds: int
    maker_max_reprices: int

    # Momentum Fade Exit
    momentum_exit_enabled: bool
    momentum_exit_min_age_minutes: int
    momentum_exit_min_profit_pct: float
    momentum_exit_rsi_slope_threshold: float

    # Environment
    testnet: bool
    dry_run: bool

    # FLAG-Trader
    flag_trader_config: Dict[str, Any]

    # Squeeze trigger
    squeeze_trigger_enabled: bool = True
    squeeze_candle_interval: str = "15m"
    squeeze_candle_limit: int = 50
    squeeze_candle_ttl_seconds: float = 300.0
    squeeze_lookback_bars: int = 3
    squeeze_bb_period: int = 20
    squeeze_bb_std_mult: float = 2.0
    squeeze_kc_ema_period: int = 20
    squeeze_kc_atr_period: int = 14
    squeeze_kc_atr_mult: float = 1.5

    # R-based exit system
    r_based_exits_enabled: bool = True
    bp_activation_r: float = 2.0
    bp_offset_pct: float = 0.15
    strength_exit_r: float = 3.0
    trailing_r_enabled: bool = True
    trailing_start_r: float = 2.0
    trailing_step_r: float = 1.0
    trailing_lock_r: float = 0.5

    # Violation exit (LLM hint)
    violation_exit_enabled: bool = True
    violation_ema_period: int = 4
    violation_ema_source: str = "high"
    violation_min_profit_pct: float = 0.3

    @classmethod
    def from_yaml(cls, path: str) -> "ConservativeConfig":
        """Load configuration from YAML file."""
        with open(path, "r") as f:
            data = yaml.safe_load(f)

        def get_section(name: str) -> dict:
            return data.get(name, {})

        universe = get_section("universe")
        timeframes = get_section("timeframes")
        risk = get_section("risk")
        stops = get_section("stops")
        regime = get_section("regime")
        execution = get_section("execution")
        me = stops.get("momentum_exit", {})
        flag_trader = get_section("flag_trader")
        squeeze = get_section("squeeze_trigger")

        # Parse universe mode and assets
        universe_mode = universe.get("mode", "manual")
        if universe_mode == "all":
            assets = ["__DYNAMIC__"]
        else:
            assets = [
                asset["symbol"]
                for asset in universe.get("assets", [])
                if asset.get("enabled", True)
            ]

        # Environment: OS env var takes precedence over YAML
        env = os.getenv("ENVIRONMENT", data.get("environment", "testnet"))

        return cls(
            assets=assets,
            universe_mode=universe_mode,
            min_volume_24h=universe.get("min_volume_24h", 100000),
            exclude_symbols=universe.get("exclude_symbols", ["USDC", "USDT", "DAI"]),
            primary_timeframe=timeframes.get("primary", "15m"),
            bars_to_fetch=timeframes.get("bars_to_fetch", 200),
            scan_interval_minutes=timeframes.get("scan_interval_minutes", 5),
            min_per_trade_pct=risk.get("min_per_trade_pct", 10.0),
            max_per_trade_pct=risk.get("max_per_trade_pct", 30.0),
            max_exposure_pct=risk.get("max_exposure_pct", 300),
            max_position_pct=risk.get("max_position_pct", 70),
            min_leverage=risk.get("min_leverage", 2),
            max_leverage=risk.get("max_leverage", 5),
            initial_atr_mult=stops.get("initial_atr_mult", 2.5),
            trailing_atr_mult=stops.get("trailing_atr_mult", 0),
            minimal_roi=stops.get("minimal_roi", {}),
            stop_loss_pct=stops.get("stop_loss_pct", 0.8),
            take_profit_pct=stops.get("take_profit_pct", 1.6),
            breakeven_threshold_pct=stops.get("breakeven_threshold_pct", 1.2),
            max_hold_hours=stops.get("max_hold_hours", 6.0),
            # R-based exit system
            r_based_exits_enabled=stops.get("r_based_exits", {}).get("enabled", True),
            bp_activation_r=stops.get("r_based_exits", {}).get("bp_activation_r", 2.0),
            bp_offset_pct=stops.get("r_based_exits", {}).get("bp_offset_pct", 0.15),
            strength_exit_r=stops.get("r_based_exits", {}).get("strength_exit_r", 3.0),
            trailing_r_enabled=stops.get("r_based_exits", {}).get("trailing_enabled", True),
            trailing_start_r=stops.get("r_based_exits", {}).get("trailing_start_r", 2.0),
            trailing_step_r=stops.get("r_based_exits", {}).get("trailing_step_r", 1.0),
            trailing_lock_r=stops.get("r_based_exits", {}).get("trailing_lock_r", 0.5),
            # Violation exit
            violation_exit_enabled=stops.get("violation_exit", {}).get("enabled", True),
            violation_ema_period=stops.get("violation_exit", {}).get("ema_period", 4),
            violation_ema_source=stops.get("violation_exit", {}).get("ema_source", "high"),
            violation_min_profit_pct=stops.get("violation_exit", {}).get("min_profit_pct", 0.3),
            trend_adx_entry_min=regime.get("trend_adx_entry_min", 28.0),
            trend_adx_exit_min=regime.get("trend_adx_exit_min", 22.0),
            range_adx_max=regime.get("range_adx_max", 20.0),
            choppiness_range_min=regime.get("choppiness_range_min", 60.0),
            regime_confirmation_bars=regime.get("confirmation_bars", 3),
            regime_exit_grace_minutes=regime.get("regime_exit_grace_minutes", 5),
            prefer_limit=execution.get("prefer_limit", True),
            max_slippage_pct=execution.get("max_slippage_pct", 0.1),
            max_spread_pct=execution.get("max_spread_pct", 0.10),
            entry_mode=execution.get("entry_mode", "taker"),
            limit_timeout_seconds=execution.get("limit_timeout_seconds", 60),
            maker_reprice_interval_seconds=execution.get("maker_reprice_interval_seconds", 10),
            maker_max_reprices=execution.get("maker_max_reprices", 6),
            momentum_exit_enabled=me.get("enabled", False),
            momentum_exit_min_age_minutes=me.get("min_age_minutes", 15),
            momentum_exit_min_profit_pct=me.get("min_profit_pct", 0.1),
            momentum_exit_rsi_slope_threshold=me.get("rsi_slope_threshold", 1.0),
            testnet=env.lower() == "testnet",
            dry_run=data.get("dry_run", False),
            flag_trader_config=flag_trader if flag_trader else {},
            squeeze_trigger_enabled=squeeze.get("enabled", True),
            squeeze_candle_interval=squeeze.get("candle_interval", "15m"),
            squeeze_candle_limit=squeeze.get("candle_limit", 50),
            squeeze_candle_ttl_seconds=squeeze.get("candle_ttl_seconds", 300.0),
            squeeze_lookback_bars=squeeze.get("lookback_bars", 3),
            squeeze_bb_period=squeeze.get("bb_period", 20),
            squeeze_bb_std_mult=squeeze.get("bb_std_mult", 2.0),
            squeeze_kc_ema_period=squeeze.get("kc_ema_period", 20),
            squeeze_kc_atr_period=squeeze.get("kc_atr_period", 14),
            squeeze_kc_atr_mult=squeeze.get("kc_atr_mult", 1.5),
        )


# =============================================================================
# Bot Orchestrator
# =============================================================================

class ConservativeBot:
    """
    Main orchestrator for FLAG-Trader LLM-based trading system.

    Architecture:
        Candles -> FlagTraderModel(prompt) -> action -> RiskManager -> Execution

    Critical design principles:
    1. FLAG-Trader model decides Buy/Sell/Hold for each asset
    2. Execute actionable decisions through risk manager pipeline
    3. Physical gates: cooldown, protections, spread
    """

    SERVICE_ORDER = [
        "market_state",    # Data provider
        "risk_manager",    # Sizing
        "execution",       # Order placement
        "telegram",        # Notifications (non-critical)
        "whatsapp",              # Notifications (non-critical)
        "performance_monitor",   # Trade performance tracking
        "counterfactual_logger", # Rejected trade analysis
        "capital_ladder",        # Progressive scale-up tracking
    ]

    def __init__(
        self,
        config_path: str = "crypto_bot/config/trading.yaml",
        config: Optional[ConservativeConfig] = None,
    ) -> None:
        self.config_path = config_path
        self._config: Optional[ConservativeConfig] = config

        # Core components
        self._bus: Optional[MessageBus] = None
        self._exchange: Optional[HyperliquidClient] = None

        # FLAG-Trader
        self._flag_agent: Optional[FlagTraderAgent] = None
        self._trade_logger: Optional[FlagTradeLogger] = None

        # Services
        self._services: Dict[str, Any] = {}

        # State
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._start_time: Optional[datetime] = None

        # Background tasks
        self._strategy_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None

        # Consecutive scan error counter for ntfy alerts
        self._consecutive_scan_errors: int = 0

        # LLM position evaluation timer (exit management)
        self._last_position_eval: float = 0.0
        self._position_eval_interval: float = 60.0  # Evaluate open positions every 60s

        # Anti-churn: daily trade counter (resets at midnight UTC)
        self._daily_trade_count: int = 0
        self._daily_trade_date: Optional[str] = None  # ISO date string e.g. "2026-03-30"
        # Persisted to disk so restarts within the same UTC day don't bypass the limit
        from pathlib import Path as _Path
        import os as _os
        self._daily_trade_count_file = _Path(
            _os.environ.get("HLQUANTBOT_DATA_DIR", str(_Path.home() / ".hlquantbot"))
        ) / "main_daily_trade_count.json"
        self._load_daily_trade_count()

    # =========================================================================
    # Properties
    # =========================================================================

    @property
    def config(self) -> ConservativeConfig:
        if self._config is None:
            raise RuntimeError("Configuration not loaded")
        return self._config

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def market_state(self) -> Optional[MarketStateService]:
        return self._services.get("market_state")

    # =========================================================================
    # Initialization
    # =========================================================================

    def _load_config(self) -> ConservativeConfig:
        """Load configuration from YAML."""
        logger.info("Loading configuration from %s", self.config_path)
        self._config = ConservativeConfig.from_yaml(self.config_path)

        with open(self.config_path, "r") as f:
            self._raw_config = yaml.safe_load(f)

        logger.info(
            "Config loaded: assets=%s, risk=%.1f-%.1f%%, leverage=%d-%dx",
            self._config.assets,
            self._config.min_per_trade_pct,
            self._config.max_per_trade_pct,
            self._config.min_leverage,
            self._config.max_leverage,
        )
        return self._config

    def _load_daily_trade_count(self) -> None:
        """Restore daily trade count from disk if still in today's UTC window.

        Opt-in via ``HLQUANTBOT_PERSIST_TRADE_COUNT=1``.
        """
        import os as _os
        if _os.environ.get("HLQUANTBOT_PERSIST_TRADE_COUNT", "").strip() not in ("1", "true", "yes"):
            return
        try:
            if not self._daily_trade_count_file.exists():
                return
            import json as _json
            data = _json.loads(self._daily_trade_count_file.read_text())
            saved_date = data.get("date")
            saved_count = int(data.get("count", 0))
            if not saved_date:
                return
            today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if saved_date == today_utc:
                self._daily_trade_count = saved_count
                self._daily_trade_date = saved_date
                logger.info(
                    "Restored main daily trade count: %d for %s",
                    saved_count, saved_date,
                )
            else:
                self._daily_trade_count_file.unlink(missing_ok=True)
        except Exception as e:
            logger.warning("Failed to load main daily trade count: %s", e)

    def _save_daily_trade_count(self) -> None:
        """Persist daily trade count to disk so restarts respect the limit."""
        import os as _os
        if _os.environ.get("HLQUANTBOT_PERSIST_TRADE_COUNT", "").strip() not in ("1", "true", "yes"):
            return
        try:
            import json as _json
            self._daily_trade_count_file.parent.mkdir(parents=True, exist_ok=True)
            self._daily_trade_count_file.write_text(
                _json.dumps({"date": self._daily_trade_date, "count": self._daily_trade_count})
            )
        except Exception as e:
            logger.warning("Failed to save main daily trade count: %s", e)

    def _init_flag_trader(self) -> FlagTraderAgent:
        """Initialize FLAG-Trader model and agent."""
        ft_cfg = FlagTraderConfig.from_dict(self.config.flag_trader_config)

        logger.info(
            "Loading FLAG-Trader model: %s (device=%s)",
            ft_cfg.model_name, ft_cfg.device,
        )

        model = FlagTraderModel(
            model_name=ft_cfg.model_name,
            device=ft_cfg.device,
        )

        # Load trained checkpoint if it exists
        checkpoint = Path(ft_cfg.checkpoint_path)
        if checkpoint.exists():
            logger.info("Loading checkpoint: %s", checkpoint)
            model.load_trainable(checkpoint)
            logger.info("Checkpoint loaded successfully")
        else:
            logger.warning(
                "No checkpoint found at %s -- using base model weights",
                checkpoint,
            )

        model.eval()  # Set to inference mode
        prompt_builder = PromptBuilder(candle_window=ft_cfg.candle_window)

        # Trade logger for retraining data.
        # Prefer HLQUANTBOT_DATA_DIR (set in docker-compose to /data/hlquantbot)
        # over cwd-relative default so decisions/outcomes always land in a known
        # absolute path regardless of where the process is launched.
        _data_dir_env = os.environ.get("HLQUANTBOT_DATA_DIR")
        if _data_dir_env:
            _trade_log_dir = Path(_data_dir_env) / "trade_logs"
        else:
            _trade_log_dir = Path("data/trade_logs")
        self._trade_logger = FlagTradeLogger(log_dir=_trade_log_dir)
        logger.info("Trade logger initialized: %s", self._trade_logger.log_dir.resolve())

        self._flag_agent = FlagTraderAgent(
            config=ft_cfg,
            model=model,
            prompt_builder=prompt_builder,
            trade_logger=self._trade_logger,
        )

        logger.info(
            "FLAG-Trader initialized: scan=%d assets, window=%d candles, threshold=%.2f",
            ft_cfg.max_assets_to_scan,
            ft_cfg.candle_window,
            ft_cfg.confidence_threshold,
        )
        return self._flag_agent

    async def _init_message_bus(self) -> MessageBus:
        """Initialize message bus."""
        logger.info("Starting message bus...")
        self._bus = MessageBus()
        await self._bus.start()
        logger.info("Message bus started")
        return self._bus

    async def _init_exchange(self) -> HyperliquidClient:
        """Initialize exchange client."""
        testnet = self.config.testnet
        if os.getenv("ENVIRONMENT", "").lower() == "mainnet":
            testnet = False

        logger.info(
            "Connecting to Hyperliquid %s...",
            "TESTNET" if testnet else "MAINNET"
        )

        self._exchange = HyperliquidClient(testnet=testnet)
        await self._exchange.connect()

        account = await self._exchange.get_account_state()
        logger.info("Exchange connected: Equity $%.2f", account.get("equity", 0))
        return self._exchange

    async def _load_dynamic_assets(self) -> None:
        """Dynamically load all available assets from Hyperliquid."""
        cfg = self.config
        if cfg.universe_mode != "all":
            logger.info("Universe mode is 'manual', using configured assets: %s", cfg.assets)
            return

        if not self._exchange:
            raise RuntimeError("Exchange not initialized")

        logger.info("Loading dynamic asset universe...")

        try:
            markets = await self._exchange.get_all_markets()
            all_symbols = [market["name"] for market in markets]
            logger.info("Found %d total symbols on Hyperliquid", len(all_symbols))

            filtered = [s for s in all_symbols if s not in cfg.exclude_symbols]
            logger.info("After exclusion filter: %d symbols", len(filtered))

            # Apply min_volume_24h filter at startup
            min_vol = cfg.min_volume_24h
            if min_vol > 0:
                volumes = await self._exchange._get_asset_data(
                    "dayNtlVlm", "volumes_startup", ttl=300.0,
                )
                before = len(filtered)
                filtered = [
                    s for s in filtered
                    if volumes.get(s, 0) >= min_vol
                ]
                logger.info(
                    "After volume filter (min $%.0f): %d symbols (dropped %d)",
                    min_vol, len(filtered), before - len(filtered),
                )

            object.__setattr__(cfg, "assets", filtered)

            logger.info(
                "Dynamic universe loaded: %d assets (excluded %d)",
                len(filtered), len(all_symbols) - len(filtered),
            )
            if len(filtered) > 15:
                logger.info("First 10: %s", filtered[:10])
                logger.info("Last 5: %s", filtered[-5:])
            else:
                logger.info("Assets: %s", filtered)

        except Exception as e:
            logger.error("Failed to load dynamic assets: %s", e)
            fallback = ["BTC", "ETH"]
            object.__setattr__(cfg, "assets", fallback)
            logger.warning("Using fallback assets: %s", fallback)

    def _init_services(self) -> None:
        """Initialize all services."""
        cfg = self.config

        # Notifications
        telegram_service = None
        if self._bus is not None:
            telegram_service = TelegramService(bus=self._bus, config=self._raw_config)
            self._services["telegram"] = telegram_service
            self._services["whatsapp"] = WhatsAppService(bus=self._bus, config=self._raw_config)

        # Market State Service
        ms_config = MarketStateConfig(
            assets=cfg.assets,
            timeframe=cfg.primary_timeframe,
            bars_to_fetch=cfg.bars_to_fetch,
            interval_seconds=cfg.scan_interval_minutes * 60,
            trend_adx_entry_min=cfg.trend_adx_entry_min,
            trend_adx_exit_min=cfg.trend_adx_exit_min,
            range_adx_max=cfg.range_adx_max,
            choppiness_range_min=cfg.choppiness_range_min,
            regime_confirmation_bars=cfg.regime_confirmation_bars,
        )
        self._services["market_state"] = create_market_state_service(
            bus=self._bus, config=ms_config, testnet=cfg.testnet,
        )

        # Risk Manager
        risk_config = RiskConfig(
            min_per_trade_pct=cfg.min_per_trade_pct,
            max_per_trade_pct=cfg.max_per_trade_pct,
            max_exposure_pct=cfg.max_exposure_pct,
            max_position_pct=cfg.max_position_pct,
            min_leverage=cfg.min_leverage,
            max_leverage=cfg.max_leverage,
            trailing_atr_mult=cfg.trailing_atr_mult,
            max_slippage_pct=cfg.max_slippage_pct,
        )
        self._services["risk_manager"] = create_risk_manager(
            bus=self._bus, config=risk_config,
            client=self._exchange, telegram=telegram_service,
        )

        # Execution Engine
        class _ExecConfig:
            def __init__(self, cfg: ConservativeConfig):
                self.order_type = "limit" if cfg.prefer_limit else "market"
                self.max_slippage_pct = cfg.max_slippage_pct
                self.limit_timeout_seconds = cfg.limit_timeout_seconds
                self.retry_attempts = 3
                self.retry_delay_seconds = 5
                self.position_sync_interval = 30
                self.fill_sync_interval = 10
                self.entry_mode = cfg.entry_mode
                self.maker_reprice_interval_seconds = cfg.maker_reprice_interval_seconds
                self.maker_max_reprices = cfg.maker_max_reprices

        class _RiskConfig:
            def __init__(self, cfg: ConservativeConfig):
                self.take_profit_pct = cfg.take_profit_pct
                self.stop_loss_pct = cfg.stop_loss_pct
                self.leverage = cfg.max_leverage  # Use max as default for execution engine
                self.breakeven_threshold_pct = cfg.breakeven_threshold_pct

        class _StopsConfig:
            def __init__(self, cfg: ConservativeConfig):
                self.initial_atr_mult = cfg.initial_atr_mult
                self.trailing_atr_mult = cfg.trailing_atr_mult
                self.minimal_roi = cfg.minimal_roi
                self.max_hold_hours = cfg.max_hold_hours
                # R-based exit system
                self.r_based_exits_enabled = cfg.r_based_exits_enabled
                self.bp_activation_r = cfg.bp_activation_r
                self.bp_offset_pct = cfg.bp_offset_pct
                self.strength_exit_r = cfg.strength_exit_r
                self.trailing_r_enabled = cfg.trailing_r_enabled
                self.trailing_start_r = cfg.trailing_start_r
                self.trailing_step_r = cfg.trailing_step_r
                self.trailing_lock_r = cfg.trailing_lock_r

        class _ServicesConfig:
            def __init__(self, exec_cfg: _ExecConfig):
                self.execution_engine = exec_cfg

        class _MomentumExitConfig:
            def __init__(self, cfg: ConservativeConfig):
                self.enabled = cfg.momentum_exit_enabled
                self.min_age_minutes = cfg.momentum_exit_min_age_minutes
                self.min_profit_pct = cfg.momentum_exit_min_profit_pct
                self.rsi_slope_threshold = cfg.momentum_exit_rsi_slope_threshold

        class _RegimeConfig:
            def __init__(self, cfg: ConservativeConfig):
                self.regime_exit_grace_minutes = cfg.regime_exit_grace_minutes

        class _ConfigAdapter:
            def __init__(self, cfg: ConservativeConfig):
                self.services = _ServicesConfig(_ExecConfig(cfg))
                self.risk = _RiskConfig(cfg)
                self.stops = _StopsConfig(cfg)
                self.momentum_exit = _MomentumExitConfig(cfg)
                self.regime = _RegimeConfig(cfg)

        self._services["execution"] = ExecutionEngineService(
            bus=self._bus, config=_ConfigAdapter(cfg),
            client=self._exchange,
        )

        # Performance Monitor
        whatsapp_svc = self._services.get("whatsapp")

        from .services.performance_monitor import PerformanceMonitorService
        self._services["performance_monitor"] = PerformanceMonitorService(
            bus=self._bus, config=self._raw_config, whatsapp=whatsapp_svc,
            exchange=self._exchange,
        )

        # Counterfactual Logger
        from .services.counterfactual_logger import CounterfactualLoggerService
        self._services["counterfactual_logger"] = CounterfactualLoggerService(
            bus=self._bus, config=self._raw_config, whatsapp=whatsapp_svc,
            take_profit_pct=cfg.take_profit_pct, stop_loss_pct=cfg.stop_loss_pct,
        )

        # Capital Ladder
        from .services.capital_ladder import CapitalLadderService
        perf_mon = self._services.get("performance_monitor")
        self._services["capital_ladder"] = CapitalLadderService(
            bus=self._bus, config=self._raw_config,
            whatsapp=whatsapp_svc, performance_monitor=perf_mon,
            exchange=self._exchange,
        )

        # Wire capital_ladder back into performance_monitor
        if perf_mon:
            perf_mon._capital_ladder = self._services.get("capital_ladder")

        # Wire performance_monitor into risk_manager for cooldown trade history
        risk_mgr = self._services.get("risk_manager")
        if risk_mgr and perf_mon:
            risk_mgr._performance_monitor = perf_mon

        logger.info(
            "Initialized %d services: %s",
            len(self._services), ", ".join(self._services.keys()),
        )

    # =========================================================================
    # Evaluation Loop -- FLAG-Trader
    # =========================================================================

    async def _strategy_loop(self) -> None:
        """Main evaluation loop: scan assets with FLAG-Trader model.

        Purely event-driven via RealtimeMonitorService:
        - Price move >2% on universe assets
        - Position PnL threshold breach (±3%)
        - New fill detected

        Cooldown of 60s between triggers to avoid LLM spam.
        """
        logger.info("FLAG-Trader evaluation loop started (realtime monitor)")

        await asyncio.sleep(10)  # Let services initialize

        # Start realtime monitor with full universe
        self._monitor = RealtimeMonitorService(
            self._exchange, self.config, universe_assets=list(self.config.assets),
            squeeze_config={
                "enabled": self.config.squeeze_trigger_enabled,
                "candle_interval": self.config.squeeze_candle_interval,
                "candle_limit": self.config.squeeze_candle_limit,
                "candle_ttl_seconds": self.config.squeeze_candle_ttl_seconds,
                "lookback_bars": self.config.squeeze_lookback_bars,
                "bb_period": self.config.squeeze_bb_period,
                "bb_std_mult": self.config.squeeze_bb_std_mult,
                "kc_ema_period": self.config.squeeze_kc_ema_period,
                "kc_atr_period": self.config.squeeze_kc_atr_period,
                "kc_atr_mult": self.config.squeeze_kc_atr_mult,
            },
        )
        await self._monitor.start()

        while self._running and not self._shutdown_event.is_set():
            try:
                # --- Check squeeze trigger for new entries ---
                trigger, reason, triggered_symbols = self._monitor.should_trigger_llm()
                if trigger:
                    logger.info("LLM triggered: %s", reason)
                    await self._evaluate_with_flag_trader(triggered_symbols)
                    self._consecutive_scan_errors = 0
                else:
                    # Heartbeat ogni 5 min quando non c'è trigger
                    hb_now = time.time()
                    if not getattr(self, "_last_heartbeat", None) or (hb_now - self._last_heartbeat) > 300:
                        self._last_heartbeat = hb_now
                        try:
                            squeeze_states_count = len(getattr(self._monitor, "_squeeze_states", {}))
                        except Exception:
                            squeeze_states_count = -1
                        try:
                            rm = self._services.get("risk_manager")
                            positions_count = len(rm._open_positions) if rm else 0
                        except Exception:
                            positions_count = -1
                        logger.info(
                            "MAIN LOOP heartbeat | no_trigger | squeeze_states=%d | positions=%d",
                            squeeze_states_count,
                            positions_count,
                        )

                # --- Evaluate open positions every 60s (LLM-only exit management) ---
                now = time.time()
                if now - self._last_position_eval >= self._position_eval_interval:
                    self._last_position_eval = now
                    risk_manager = self._services.get("risk_manager")
                    if risk_manager and risk_manager._open_positions:
                        all_position_symbols = list(risk_manager._open_positions.keys())
                        portfolio = await self._get_portfolio_state()
                        await self._evaluate_positions_with_llm(
                            all_position_symbols, risk_manager, portfolio,
                        )

            except asyncio.CancelledError:
                logger.debug("Evaluation loop cancelled")
                break
            except Exception as e:
                self._consecutive_scan_errors += 1
                logger.error("Evaluation loop error: %s", e, exc_info=True)
                if self._consecutive_scan_errors >= 5 and self._bus:
                    await self._bus.publish(Topic.RISK_ALERTS, {
                        "type": "scan_errors",
                        "alert_type": "scan_errors",
                        "message": (
                            f"{self._consecutive_scan_errors} consecutive scan errors.\n"
                            f"Error: {type(e).__name__}: {e}"
                        ),
                        "consecutive_errors": self._consecutive_scan_errors,
                    })
                await asyncio.sleep(60)

            # Check every 10 seconds for triggers
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(), timeout=10,
                )
                break
            except asyncio.TimeoutError:
                pass

        # Cleanup monitor on exit
        if hasattr(self, '_monitor') and self._monitor:
            await self._monitor.stop()

    async def _get_portfolio_state(self) -> dict:
        """Fetch current portfolio state from exchange."""
        portfolio = {"cash_balance": 0.0, "asset_position": 0.0, "total_account_value": 0.0}
        try:
            if self._exchange:
                account = await self._exchange.get_account_state()
                equity = float(account.get("equity", 0))
                margin_used = float(account.get("marginUsed", 0))
                unrealized_pnl = float(account.get("unrealizedPnl", 0))
                portfolio = {
                    "cash_balance": equity - margin_used,
                    "asset_position": margin_used,
                    "total_account_value": equity,
                    "unrealized_pnl": unrealized_pnl,
                    "leverage_used": round(margin_used / equity, 2) if equity > 0 else 0.0,
                }
        except Exception as e:
            logger.warning("Could not fetch account state: %s", e)
        return portfolio

    async def _evaluate_positions_with_llm(
        self,
        symbols: list[str],
        risk_manager: Any,
        portfolio: dict,
    ) -> int:
        """Evaluate open positions with FLAG-Trader LLM for exit decisions.

        This runs on a 60-second timer, independent of squeeze triggers.
        Returns the number of positions closed.
        """
        if not self._flag_agent or not self._exchange:
            return 0

        positions_evaluated = 0
        positions_closed = 0

        for symbol in symbols:
            pos = risk_manager._open_positions.get(symbol)
            if not pos:
                continue
            positions_evaluated += 1
            try:
                # Calculate PnL %
                entry_px = float(pos.get("entry_price", 0))
                mark_px = float(pos.get("mark_price", entry_px))
                side = pos.get("side", "long")
                if entry_px > 0:
                    if side == "long":
                        pnl_pct = ((mark_px / entry_px) - 1.0) * 100.0
                    else:
                        pnl_pct = ((entry_px / mark_px) - 1.0) * 100.0 if mark_px > 0 else 0.0
                else:
                    pnl_pct = 0.0

                # Anti-churn: skip if position too young
                opened_at = pos.get("opened_at")
                if opened_at:
                    if isinstance(opened_at, str):
                        from datetime import datetime as _dt
                        opened_at = _dt.fromisoformat(opened_at)
                    age_minutes = (datetime.now(timezone.utc) - opened_at).total_seconds() / 60
                    min_hold = self.config.flag_trader_config.get("min_hold_minutes", 30)
                    if age_minutes < min_hold:
                        logger.info(
                            "FLAG-Trader EXIT SKIPPED | %s %s too young (%.1f min < %d min)",
                            side.upper(), symbol, age_minutes, min_hold,
                        )
                        continue

                # Build entry context for the LLM prompt
                entry_context = {}
                entry_reason = pos.get("entry_reason", "")
                if entry_reason:
                    entry_context["entry_reason"] = entry_reason
                entry_conf = pos.get("entry_confidence", 0.0)
                if entry_conf:
                    entry_context["entry_confidence"] = entry_conf
                entry_details = pos.get("entry_trigger_details", "")
                if entry_details:
                    entry_context["entry_trigger_details"] = entry_details

                # Also check execution engine for entry context (richer data)
                exec_engine = self._services.get("execution")
                if exec_engine:
                    exec_pos = exec_engine.active_positions.get(symbol)
                    if exec_pos:
                        if exec_pos.entry_reason:
                            entry_context["entry_reason"] = exec_pos.entry_reason
                        if exec_pos.entry_confidence:
                            entry_context["entry_confidence"] = exec_pos.entry_confidence
                        if exec_pos.entry_trigger_details:
                            entry_context["entry_trigger_details"] = exec_pos.entry_trigger_details

                # --- Violation exit detection (hint for LLM) ---
                violation_info = {}
                if self.config.violation_exit_enabled and pnl_pct > self.config.violation_min_profit_pct:
                    try:
                        violation_info = await self._check_violation_exit(
                            symbol, side, entry_px, pnl_pct,
                        )
                    except Exception as ve:
                        logger.debug("Violation check failed for %s: %s", symbol, ve)

                # Enrich portfolio with entry context and violation info for prompt builder
                eval_portfolio = {
                    **portfolio,
                    "entry_context": entry_context,
                    "violation_info": violation_info,
                }

                # Add R-multiple info from execution engine
                exec_engine_r = self._services.get("execution")
                if exec_engine_r:
                    exec_pos_r = getattr(exec_engine_r, 'active_positions', {}).get(symbol)
                    if exec_pos_r:
                        eval_portfolio["r_multiple"] = exec_pos_r.current_r_multiple
                        eval_portfolio["peak_r_multiple"] = exec_pos_r.peak_r_multiple
                        eval_portfolio["one_r_pct"] = exec_pos_r.one_r_pct
                        eval_portfolio["breakeven_activated"] = exec_pos_r.breakeven_activated

                exit_decision = await self._flag_agent.evaluate_position(
                    symbol=symbol,
                    direction=side,
                    entry_price=entry_px,
                    pnl_pct=pnl_pct,
                    candle_fetcher=self._exchange,
                    portfolio=eval_portfolio,
                )
                if exit_decision.should_close:
                    # Anti-churn fee gate for model_reversal exits
                    # model_reversal = LLM flipped opinion, often noise causing double fees
                    # Allow close only if: (a) profitable >= fee threshold, or (b) losing > 1% (clear wrong direction)
                    # Otherwise skip — likely noise, fees would make it worse
                    min_net_profit = self.config.flag_trader_config.get("min_net_profit_pct_to_close", 0.15)
                    if exit_decision.reason == "model_reversal":
                        model_reversal_loss_threshold = -1.0  # Allow close if losing > 1%
                        if pnl_pct >= min_net_profit:
                            pass  # Profitable enough to cover fees — close (take profit)
                        elif pnl_pct <= model_reversal_loss_threshold:
                            pass  # Losing badly — clear wrong direction, close
                        else:
                            logger.info(
                                "FLAG-Trader EXIT SKIPPED | %s %s | model_reversal fee gate | pnl=%.3f%% "
                                "(need >=%.2f%% or <=%.1f%% to close)",
                                side.upper(), symbol, pnl_pct, min_net_profit, model_reversal_loss_threshold,
                            )
                            continue
                    elif 0 < pnl_pct < min_net_profit:
                        # Non-reversal exits: original fee gate (small profit doesn't cover fees)
                        logger.info(
                            "FLAG-Trader EXIT SKIPPED | %s %s | pnl=%.3f%% < min_net_profit=%.2f%% (fees not covered)",
                            side.upper(), symbol, pnl_pct, min_net_profit,
                        )
                        continue

                    logger.info(
                        "FLAG-Trader EXIT | CLOSE %s %s | confidence=%.4f | reason=%s | pnl=%.3f%%",
                        side.upper(), symbol, exit_decision.confidence, exit_decision.reason, pnl_pct,
                    )
                    exec_engine = self._services.get("execution")
                    if exec_engine:
                        active_pos = getattr(exec_engine, 'active_positions', {}).get(symbol)
                        if active_pos and hasattr(active_pos, 'exit_reason'):
                            active_pos.exit_reason = exit_decision.reason
                        await exec_engine.close_position(symbol)
                        positions_closed += 1
            except Exception as e:
                logger.warning("Error evaluating position %s: %s", symbol, e)

        if positions_evaluated > 0:
            logger.info(
                "POSITION EVAL | %d evaluated, %d closed (timer-based, every %.0fs)",
                positions_evaluated, positions_closed, self._position_eval_interval,
            )
        return positions_closed

    async def _check_violation_exit(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        pnl_pct: float,
    ) -> dict:
        """Check if price violates a technical level while in profit.

        Returns violation info dict to be included in the LLM prompt.
        This is NOT a mechanical exit — it's a hint for the LLM to consider closing.

        Violation for LONG: last candle close < EMA(period) of highs
        Violation for SHORT: last candle close > EMA(period) of lows
        """
        if not self._exchange:
            return {}

        ema_period = self.config.violation_ema_period
        candles_raw = await self._exchange.get_candles(
            symbol,
            interval=self.config.primary_timeframe,
            limit=ema_period + 5,
        )

        if not candles_raw or len(candles_raw) < ema_period + 1:
            return {}

        # Calculate EMA of highs (for longs) or lows (for shorts)
        if side == "long":
            prices = [float(c.get("h", c.get("high", 0))) for c in candles_raw]
        else:
            prices = [float(c.get("l", c.get("low", 0))) for c in candles_raw]

        # Simple EMA calculation
        multiplier = 2.0 / (ema_period + 1)
        ema = prices[0]
        for p in prices[1:]:
            ema = (p - ema) * multiplier + ema

        last_close = float(candles_raw[-1].get("c", candles_raw[-1].get("close", 0)))

        violated = False
        if side == "long" and last_close < ema:
            violated = True
        elif side == "short" and last_close > ema:
            violated = True

        if violated:
            logger.info(
                "VIOLATION detected: %s %s | close=%.4f vs EMA%d_%s=%.4f | pnl=%.2f%%",
                side.upper(), symbol, last_close, ema_period,
                "high" if side == "long" else "low", ema, pnl_pct,
            )
            return {
                "violated": True,
                "ema_period": ema_period,
                "ema_source": "high" if side == "long" else "low",
                "ema_value": round(ema, 6),
                "last_close": round(last_close, 6),
                "description": (
                    f"Price closed {'below' if side == 'long' else 'above'} "
                    f"EMA{ema_period} of {'highs' if side == 'long' else 'lows'} "
                    f"({last_close:.4f} vs {ema:.4f}) — consider exiting"
                ),
            }

        return {}

    async def _evaluate_with_flag_trader(self, triggered_symbols: list[str] | None = None) -> None:
        """Evaluate assets with FLAG-Trader model — NEW ENTRIES ONLY.

        Phase 1 (exit evaluation) now runs independently on a 60s timer via
        _evaluate_positions_with_llm(). This method only handles Phase 2:
        scanning for new trade opportunities from squeeze triggers.

        Args:
            triggered_symbols: Specific symbols to evaluate (from monitor triggers).
                If empty/None, falls back to scanning top N from universe.

        Flow: Physical gates -> Get portfolio -> FlagTraderAgent.scan_and_decide()
              -> For each decision: create Setup -> validate spread -> publish to risk manager
        """
        if not self._flag_agent or not self._exchange:
            logger.warning("FLAG-Trader agent or exchange not initialized")
            return

        risk_manager = self._services.get("risk_manager")

        portfolio = await self._get_portfolio_state()

        pos_count = len(risk_manager._open_positions) if risk_manager else 0
        logger.info(
            "EVAL START | equity=$%.2f | margin=$%.2f | leverage=%.1fx | positions=%d | trigger=%s",
            portfolio.get("total_account_value", 0),
            portfolio.get("asset_position", 0),
            portfolio.get("leverage_used", 0),
            pos_count,
            "targeted" if triggered_symbols else "full_scan",
        )

        # --- Evaluate new trade candidates (no open position) ---
        symbols_with_positions: set = set()
        if risk_manager:
            symbols_with_positions = set(risk_manager._open_positions.keys())

        blocked = symbols_with_positions
        if triggered_symbols:
            scan_assets = [s for s in triggered_symbols if s not in blocked]
            logger.info("Targeted scan: %d triggered assets → %d after position filter",
                        len(triggered_symbols), len(scan_assets))
        else:
            scan_assets = [s for s in self.config.assets if s not in blocked]

        if not scan_assets:
            return

        # --- Update market states for execution engine ---
        market_state_svc = self._services.get("market_state")
        if market_state_svc:
            states = market_state_svc.get_all_states()
            exec_engine = self._services.get("execution")
            if exec_engine and states:
                exec_engine.update_market_states(states)

            cf_logger = self._services.get("counterfactual_logger")
            if cf_logger and states:
                cf_logger.update_market_states(states)

        # --- Run FLAG-Trader model ---
        decisions = await self._flag_agent.scan_and_decide(
            assets=scan_assets,
            candle_fetcher=self._exchange,
            portfolio=portfolio,
        )

        if not decisions:
            logger.info("FLAG-Trader: no actionable decisions this scan")
            return

        logger.info(
            "FLAG-Trader: %d actionable decisions",
            len(decisions),
        )

        # --- Anti-churn: daily trade limit ---
        max_trades_per_day = self.config.flag_trader_config.get("max_trades_per_day", 5)
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._daily_trade_date != today_utc:
            self._daily_trade_date = today_utc
            self._daily_trade_count = 0
            self._save_daily_trade_count()
            logger.info("DAILY TRADE COUNTER | reset for %s", today_utc)

        if self._daily_trade_count >= max_trades_per_day:
            logger.info(
                "DAILY TRADE LIMIT | %d/%d trades used today — skipping new entries",
                self._daily_trade_count, max_trades_per_day,
            )
            # Counterfactual: log every actionable decision rejected by daily limit
            cf_logger = self._services.get("counterfactual_logger")
            if cf_logger:
                for d in decisions:
                    state = states.get(d.symbol) if 'states' in locals() else None
                    price = float(state.close) if state is not None else 0.0
                    if price <= 0:
                        continue
                    direction = "long" if d.action == 2 else "short" if d.action == 0 else None
                    if direction is None:
                        continue
                    try:
                        cf_logger.log_rejection(
                            symbol=d.symbol,
                            direction=direction,
                            entry_price=price,
                            reason="main:daily_limit",
                            ml_probability=float(min(abs(d.confidence), 1.0)),
                        )
                    except Exception as e:
                        logger.debug("cf log_rejection failed: %s", e)
            return

        # --- Execute decisions (no position limit, exposure checked by RiskManager) ---
        executed = 0
        for decision in decisions:
            if self._daily_trade_count >= max_trades_per_day:
                logger.info(
                    "DAILY TRADE LIMIT | %d/%d reached mid-batch — stopping",
                    self._daily_trade_count, max_trades_per_day,
                )
                break
            success = await self._execute_flag_decision(decision)
            if success:
                executed += 1
                self._daily_trade_count += 1
                self._save_daily_trade_count()
                self._flag_agent.record_action(decision.action_name)

        logger.info(
            "EVAL PHASE2 | %d assets scanned, %d actionable, %d executed",
            len(scan_assets), len(decisions), executed,
        )

    async def _execute_flag_decision(self, decision: TradeDecision) -> bool:
        """Convert a FLAG-Trader decision into a Setup and execute it.

        Returns True if the setup was forwarded to risk manager.
        """
        # Map action to direction
        if decision.action == 2:  # Buy
            direction = Direction.LONG
        elif decision.action == 0:  # Sell
            direction = Direction.SHORT
        else:
            return False

        # Get current price from market state or exchange
        market_state_svc = self._services.get("market_state")
        states = market_state_svc.get_all_states() if market_state_svc else {}
        state = states.get(decision.symbol)

        if state is not None:
            entry_price = state.close
            atr = state.atr
            atr_pct = state.atr_pct
            adx = state.adx
            rsi = state.rsi
            regime = state.regime
        else:
            logger.warning("No MarketState for %s, skipping", decision.symbol)
            return False

        # Use model-predicted TP/SL instead of config values
        sl_pct = Decimal(str(round(decision.sl_pct, 2)))
        if direction == Direction.LONG:
            stop_price = entry_price * (Decimal("1") - sl_pct / Decimal("100"))
        else:
            stop_price = entry_price * (Decimal("1") + sl_pct / Decimal("100"))

        # Build entry trigger details from monitor state
        trigger_details = ""
        if hasattr(self, '_monitor') and self._monitor:
            sq_state = getattr(self._monitor, '_squeeze_states', {}).get(decision.symbol)
            if sq_state:
                trigger_details = (
                    f"squeeze_bars={getattr(sq_state, 'squeeze_bars', '?')} "
                    f"bb_w={getattr(sq_state, 'bb_width', 0):.4f} "
                    f"kc_w={getattr(sq_state, 'kc_width', 0):.4f}"
                )

        correlation_id = decision.correlation_id or uuid.uuid4().hex[:12]
        setup = Setup(
            id=f"flag_{uuid.uuid4().hex[:8]}",
            symbol=decision.symbol,
            timestamp=datetime.now(timezone.utc),
            setup_type=SetupType.MOMENTUM,
            direction=direction,
            regime=regime,
            entry_price=entry_price,
            stop_price=stop_price,
            stop_distance_pct=sl_pct,
            atr=atr,
            atr_pct=atr_pct,
            adx=adx,
            rsi=rsi,
            setup_quality=Decimal(str(round(min(abs(decision.confidence), 1.0), 4))),
            confidence=Decimal(str(round(min(abs(decision.confidence), 1.0), 4))),
            model_tp_pct=round(decision.tp_pct, 2),
            model_sl_pct=round(decision.sl_pct, 2),
            entry_reason="squeeze_fire",
            entry_confidence=round(min(abs(decision.confidence), 1.0), 4),
            entry_trigger_details=trigger_details,
            correlation_id=correlation_id,
        )

        logger.info(
            "FLAG-Trader | cid=%s | %s %s | confidence=%.2f | TP=%.1f%% SL=%.1f%% (model-predicted) | entry=$%s",
            correlation_id,
            direction.value.upper(),
            decision.symbol,
            decision.confidence,
            decision.tp_pct,
            decision.sl_pct,
            entry_price,
        )

        # --- Spread check ---
        if self._exchange:
            spread_pct = await self._exchange.get_spread_pct(setup.symbol)
            if spread_pct > self.config.max_spread_pct:
                logger.info(
                    "SKIP %s: spread %.3f%% > max %.2f%%",
                    setup.symbol, spread_pct, self.config.max_spread_pct,
                )
                cf_logger = self._services.get("counterfactual_logger")
                if cf_logger:
                    try:
                        cf_logger.log_rejection(
                            symbol=setup.symbol,
                            direction=direction.value,
                            entry_price=float(entry_price),
                            reason="main:spread",
                            ml_probability=float(min(abs(decision.confidence), 1.0)),
                        )
                    except Exception as e:
                        logger.debug("cf log_rejection failed: %s", e)
                return False

        # --- Tick size check ---
        price = float(entry_price)
        if price > 0:
            from math import log10, floor
            magnitude = floor(log10(price))
            max_decimals = min(4, max(0, 4 - magnitude))
            min_tick = 10 ** (-max_decimals)
            tp_distance = price * decision.sl_pct / 100
            if tp_distance < min_tick * 1.5:
                logger.info(
                    "SKIP %s: price $%.6f too low for TP/SL",
                    setup.symbol, price,
                )
                cf_logger = self._services.get("counterfactual_logger")
                if cf_logger:
                    try:
                        cf_logger.log_rejection(
                            symbol=setup.symbol,
                            direction=direction.value,
                            entry_price=float(entry_price),
                            reason="main:tick_size",
                            ml_probability=float(min(abs(decision.confidence), 1.0)),
                        )
                    except Exception as e:
                        logger.debug("cf log_rejection failed: %s", e)
                return False

        # Publish setup for risk manager
        if self._bus:
            await self._bus.publish(Topic.SETUPS, setup.model_dump())
        return True

    # =========================================================================
    # Trade Logger Callback
    # =========================================================================

    async def _on_fill_for_trade_logger(self, message: Any) -> None:
        """Update trade logger with outcome when a position closes."""
        if not self._trade_logger:
            return
        payload = message.payload
        if payload.get("event") != "position_closed":
            return

        symbol = payload.get("symbol", "")
        position_data = payload.get("position", {})
        opened_at = position_data.get("opened_at")

        hold_minutes = 0.0
        if opened_at:
            try:
                opened_dt = datetime.fromisoformat(opened_at)
                hold_minutes = (datetime.now(timezone.utc) - opened_dt).total_seconds() / 60.0
            except (ValueError, TypeError):
                pass

        try:
            self._trade_logger.log_outcome(
                symbol=symbol,
                entry_price=float(payload.get("entry_price", 0)),
                exit_price=float(payload.get("exit_price", 0)),
                pnl_usd=float(payload.get("realized_pnl", 0)),
                pnl_pct=float(payload.get("pnl_pct", 0)),
                exit_reason=payload.get("exit_reason") or "unknown",
                hold_duration_minutes=hold_minutes,
                side=payload.get("side"),
            )
        except Exception:
            logger.exception("trade_logger.log_outcome failed for %s", symbol)

    # =========================================================================
    # Health Monitoring
    # =========================================================================

    async def _health_loop(self) -> None:
        """Periodic health check loop."""
        logger.info("Health monitoring started")

        while self._running and not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(), timeout=30,
                )
                break
            except asyncio.TimeoutError:
                pass

            try:
                await self._check_health()
            except Exception as e:
                logger.error("Health check error: %s", e)

    async def _check_health(self) -> None:
        """Check health of all services."""
        for name, service in self._services.items():
            try:
                health = await service.health_check()
                if not health.healthy:
                    logger.warning("Service unhealthy: %s - %s", name, health.message)
            except Exception as e:
                logger.error("Health check failed for %s: %s", name, e)

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def _start_services(self) -> None:
        """Start all services in dependency order."""
        logger.info("Starting services...")
        for name in self.SERVICE_ORDER:
            if name in self._services:
                await self._services[name].start()
                logger.info("Started: %s", name)
        logger.info("All services started")

    async def _startup_safety_check(self) -> None:
        """Check for config mismatches with existing positions at startup."""
        execution = self._services.get("execution")
        if not execution:
            return

        positions = execution.active_positions
        if not positions:
            return

        warnings: list[str] = []

        logger.info("Startup safety check: %d open positions", len(positions))

        for symbol, pos in positions.items():
            pos_leverage = getattr(pos, "leverage", None)
            if pos_leverage and pos_leverage > self.config.max_leverage:
                msg = (
                    f"LEVERAGE WARNING: {symbol} has {pos_leverage}x "
                    f"but max_leverage={self.config.max_leverage}x"
                )
                logger.warning(msg)
                warnings.append(msg)

        try:
            open_orders = await self._exchange.get_open_orders()
            for symbol in positions:
                sym_reduce = [
                    o for o in open_orders
                    if o.get("symbol") == symbol and o.get("reduceOnly")
                ]
                if len(sym_reduce) > 2:
                    msg = (
                        f"DUPLICATE TP/SL: {symbol} has {len(sym_reduce)} "
                        f"reduce-only orders (expected 2)"
                    )
                    logger.warning(msg)
                    warnings.append(msg)
        except Exception as e:
            logger.warning("Could not check orders for safety audit: %s", e)

        if warnings:
            ntfy = self._services.get("ntfy") or self._services.get("whatsapp")
            alert_text = "STARTUP SAFETY CHECK\n" + "\n".join(warnings)
            logger.warning("=" * 60)
            logger.warning(alert_text)
            logger.warning("=" * 60)
            if ntfy and hasattr(ntfy, "send_custom_alert"):
                try:
                    await ntfy.send_custom_alert(alert_text, emoji="warning")
                except Exception:
                    pass
        else:
            logger.info("Startup safety check: all clear")

    async def _init_regime_for_open_positions(self) -> None:
        """Initialize confirmed regime to TREND for symbols with open positions."""
        execution = self._services.get("execution")
        market_state_svc = self._services.get("market_state")
        if not execution or not market_state_svc:
            return

        open_symbols = list(execution.active_positions.keys())
        if not open_symbols:
            return

        market_state_svc.init_confirmed_regime_for_symbols(open_symbols)
        logger.info(
            "Initialized confirmed regime (TREND) for %d open positions: %s",
            len(open_symbols), open_symbols,
        )

    async def _stop_services(self) -> None:
        """Stop all services in reverse order."""
        logger.info("Stopping services...")
        for name in reversed(self.SERVICE_ORDER):
            if name in self._services:
                try:
                    await self._services[name].stop()
                    logger.info("Stopped: %s", name)
                except Exception as e:
                    logger.error("Error stopping %s: %s", name, e)
        logger.info("All services stopped")

    async def start(self) -> None:
        """Initialize and start the bot."""
        if self._running:
            logger.warning("Bot already running")
            return

        logger.info("=" * 60)
        logger.info("HLQuantBot v5 - FLAG-Trader LLM Starting")
        logger.info("=" * 60)

        try:
            if self._config is None:
                self._load_config()

            setup_logging(log_level="INFO")

            await self._init_message_bus()
            await self._init_exchange()
            await self._load_dynamic_assets()

            # Initialize FLAG-Trader model
            self._init_flag_trader()

            self._init_services()

            await self._start_services()

            # Subscribe trade logger to fill events for outcome tracking
            if self._bus and self._trade_logger:
                await self._bus.subscribe(Topic.FILLS, self._on_fill_for_trade_logger)

            # SAFETY: Check for config mismatches with existing positions
            await self._startup_safety_check()

            # Initialize confirmed regime for open positions
            await self._init_regime_for_open_positions()

            self._strategy_task = asyncio.create_task(
                self._strategy_loop(), name="flag_trader_loop",
            )
            self._health_task = asyncio.create_task(
                self._health_loop(), name="health_loop",
            )

            self._running = True
            self._start_time = datetime.now(timezone.utc)
            self._shutdown_event.clear()

            if self._exchange:
                account = await self._exchange.get_account_state()
                equity = account.get("equity", 0)

            ft_cfg = FlagTraderConfig.from_dict(self.config.flag_trader_config)
            logger.info("=" * 60)
            logger.info(
                "HLQuantBot v5 Running (%s) | FLAG-Trader: %s",
                "TESTNET" if self.config.testnet else "MAINNET",
                ft_cfg.model_name,
            )
            logger.info("Assets: %d | Confidence threshold: %.2f", len(self.config.assets), ft_cfg.confidence_threshold)
            logger.info("Risk per trade: %.1f-%.1f%% | Leverage: %d-%dx",
                        self.config.min_per_trade_pct, self.config.max_per_trade_pct,
                        self.config.min_leverage, self.config.max_leverage)
            logger.info("=" * 60)

        except Exception as e:
            logger.critical("Failed to start: %s", e, exc_info=True)
            await self.stop()
            raise

    async def stop(self) -> None:
        """Stop the bot gracefully."""
        if not self._running:
            return

        logger.info("=" * 60)
        logger.info("HLQuantBot v5 Stopping")
        logger.info("=" * 60)

        self._running = False
        self._shutdown_event.set()

        for task in [self._strategy_task, self._health_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        await self._stop_services()

        if self._bus:
            await self._bus.stop()
        if self._exchange:
            await self._exchange.disconnect()

        logger.info("=" * 60)
        logger.info("HLQuantBot v5 Stopped")
        logger.info("=" * 60)

    async def run(self) -> None:
        """Run the bot until shutdown signal."""
        loop = asyncio.get_event_loop()

        def signal_handler():
            logger.info("Shutdown signal received")
            asyncio.create_task(self.stop())

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, signal_handler)
            except NotImplementedError:
                pass

        try:
            await self.start()
            while self._running:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt")
        finally:
            await self.stop()

    # =========================================================================
    # Status API
    # =========================================================================

    async def get_status(self) -> Dict[str, Any]:
        """Get comprehensive bot status."""
        service_status = {}
        for name, service in self._services.items():
            try:
                health = await service.health_check()
                service_status[name] = {
                    "healthy": health.healthy,
                    "message": health.message,
                    "metrics": getattr(service, "metrics", {}),
                }
            except Exception as e:
                service_status[name] = {"healthy": False, "message": str(e)}

        ft_cfg = FlagTraderConfig.from_dict(self.config.flag_trader_config) if self._config else None

        return {
            "running": self._running,
            "start_time": self._start_time.isoformat() if self._start_time else None,
            "uptime_seconds": (
                (datetime.now(timezone.utc) - self._start_time).total_seconds()
                if self._start_time else 0
            ),
            "config": {
                "assets": self.config.assets if self._config else [],
                "testnet": self.config.testnet if self._config else True,
                "risk_per_trade_range": f"{self.config.min_per_trade_pct}-{self.config.max_per_trade_pct}%" if self._config else "N/A",
                "leverage_range": f"{self.config.min_leverage}-{self.config.max_leverage}x" if self._config else "N/A",
                "flag_trader_model": ft_cfg.model_name if ft_cfg else "N/A",
            },
            "services": service_status,
        }


# =============================================================================
# Main Entry Point
# =============================================================================

async def main(config_path: str = "crypto_bot/config/trading.yaml") -> None:
    """Main entry point."""
    bot = ConservativeBot(config_path=config_path)
    await bot.run()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="HLQuantBot v5 - FLAG-Trader LLM Trading System")
    parser.add_argument("-c", "--config", default="crypto_bot/config/trading.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    asyncio.run(main(config_path=args.config))

# Alias for backward compatibility with tests
HLQuantBot = ConservativeBot
