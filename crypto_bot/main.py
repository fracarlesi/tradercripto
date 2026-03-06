#!/usr/bin/env python3
"""
HLQuantBot v4 - XGBoost ML Trade Selector
==========================================

Trading system with ML-based trade selection and strict risk controls.

Architecture:
    MarketState → XGBoost.predict(features) → P(TP) → RiskManager → Execution

This orchestrator:
1. Loads configuration from trading.yaml
2. Initializes core services (message bus)
3. Loads pre-trained XGBoost model
4. Scans all assets, predicts P(TP) for each direction
5. Executes top-N candidates by P(TP) descending

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
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from scipy.stats import rankdata

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from .core.enums import Topic
from .core.models import Direction, MarketState, Regime, Setup, SetupType

# Services
from .services.message_bus import MessageBus
from .services.market_state import (
    MarketStateService,
    MarketStateConfig,
    create_market_state_service,
)
from .services.risk_manager import (
    RiskManagerService,
    RiskConfig,
    create_risk_manager,
)
from .services.kill_switch import (
    KillSwitchService,
    KillSwitchConfig,
    create_kill_switch,
)
from .services.execution_engine import (
    ExecutionEngineService,
)
from .services.telegram_service import TelegramService
from .services.whatsapp_service import WhatsAppService
from .services.protections import ProtectionManager
from .services.ml_model import MLTradeModel

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

    # Risk
    per_trade_pct: float
    max_per_trade_pct: float
    max_positions: int
    max_exposure_pct: float
    max_position_pct: float
    max_daily_trades: int
    leverage: float

    # Kill switch
    daily_loss_pct: float
    weekly_loss_pct: float
    max_drawdown_pct: float

    # Stops
    initial_atr_mult: float
    trailing_atr_mult: float
    minimal_roi: Dict[str, float]
    stop_loss_pct: float
    take_profit_pct: float
    breakeven_threshold_pct: float

    # Regime (for MarketStateService indicator computation)
    trend_adx_entry_min: float
    trend_adx_exit_min: float
    range_adx_max: float
    choppiness_range_min: float
    regime_confirmation_bars: int
    regime_exit_grace_minutes: int

    # ML Model
    ml_model_path: str
    ml_min_probability: float
    ml_retrain_interval_days: int
    ml_retrain_days: int

    # Execution
    prefer_limit: bool
    max_slippage_pct: float
    max_spread_pct: float
    entry_mode: str  # "taker" (default) or "maker" (post-only)
    limit_timeout_seconds: int
    maker_reprice_interval_seconds: int
    maker_max_reprices: int

    # Volume Breakout
    volume_breakout_enabled: bool
    volume_breakout_min_volume_ratio: float
    volume_breakout_min_candle_body_pct: float
    volume_breakout_min_atr_pct: float
    volume_breakout_rsi_min: float
    volume_breakout_rsi_max: float
    volume_breakout_allowed_regimes: List[str]

    # Momentum Burst
    momentum_burst_enabled: bool
    momentum_burst_min_rsi_slope: float
    momentum_burst_min_candle_body_pct: float
    momentum_burst_max_rsi_entry: float
    momentum_burst_min_volume_ratio: float
    momentum_burst_allowed_regimes: List[str]

    # Momentum Fade Exit
    momentum_exit_enabled: bool
    momentum_exit_min_age_minutes: int
    momentum_exit_min_profit_pct: float
    momentum_exit_rsi_slope_threshold: float

    # Environment
    testnet: bool
    dry_run: bool

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
        ks = get_section("kill_switch")
        stops = get_section("stops")
        regime = get_section("regime")
        execution = get_section("execution")
        ml = get_section("ml_model")
        vb = get_section("volume_breakout")
        mb = get_section("momentum_burst")
        me = get_section("stops").get("momentum_exit", {})

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
            per_trade_pct=risk.get("per_trade_pct", 5.0),
            max_per_trade_pct=risk.get("max_per_trade_pct", 10.0),
            max_positions=risk.get("max_positions", 3),
            max_exposure_pct=risk.get("max_exposure_pct", 300),
            max_position_pct=risk.get("max_position_pct", 70),
            max_daily_trades=risk.get("max_daily_trades", 8),
            leverage=risk.get("leverage", 10),
            daily_loss_pct=ks.get("daily_loss_pct", 8.0),
            weekly_loss_pct=ks.get("weekly_loss_pct", 15.0),
            max_drawdown_pct=ks.get("max_drawdown_pct", 30.0),
            initial_atr_mult=stops.get("initial_atr_mult", 2.5),
            trailing_atr_mult=stops.get("trailing_atr_mult", 2.5),
            minimal_roi=stops.get("minimal_roi", {}),
            stop_loss_pct=stops.get("stop_loss_pct", 0.8),
            take_profit_pct=stops.get("take_profit_pct", 1.6),
            breakeven_threshold_pct=stops.get("breakeven_threshold_pct", 1.2),
            trend_adx_entry_min=regime.get("trend_adx_entry_min", 28.0),
            trend_adx_exit_min=regime.get("trend_adx_exit_min", 22.0),
            range_adx_max=regime.get("range_adx_max", 20.0),
            choppiness_range_min=regime.get("choppiness_range_min", 60.0),
            regime_confirmation_bars=regime.get("confirmation_bars", 3),
            regime_exit_grace_minutes=regime.get("regime_exit_grace_minutes", 5),
            ml_model_path=ml.get("model_path", "models/trade_model.joblib"),
            ml_min_probability=ml.get("min_probability", 0.50),
            # ml_mode removed — regime gate always active (trend strategy)
            ml_retrain_interval_days=ml.get("retrain_interval_days", 3),
            ml_retrain_days=ml.get("retrain_days", 30),
            prefer_limit=execution.get("prefer_limit", True),
            max_slippage_pct=execution.get("max_slippage_pct", 0.1),
            max_spread_pct=execution.get("max_spread_pct", 0.10),
            entry_mode=execution.get("entry_mode", "taker"),
            limit_timeout_seconds=execution.get("limit_timeout_seconds", 60),
            maker_reprice_interval_seconds=execution.get("maker_reprice_interval_seconds", 10),
            maker_max_reprices=execution.get("maker_max_reprices", 6),
            volume_breakout_enabled=vb.get("enabled", True),
            volume_breakout_min_volume_ratio=vb.get("min_volume_ratio", 2.0),
            volume_breakout_min_candle_body_pct=vb.get("min_candle_body_pct", 0.3),
            volume_breakout_min_atr_pct=vb.get("min_atr_pct", 0.15),
            volume_breakout_rsi_min=vb.get("rsi_min", 25.0),
            volume_breakout_rsi_max=vb.get("rsi_max", 80.0),
            volume_breakout_allowed_regimes=vb.get("allowed_regimes", ["chaos", "trend"]),
            momentum_burst_enabled=mb.get("enabled", True),
            momentum_burst_min_rsi_slope=mb.get("min_rsi_slope", 8.0),
            momentum_burst_min_candle_body_pct=mb.get("min_candle_body_pct", 0.3),
            momentum_burst_max_rsi_entry=mb.get("max_rsi_entry", 75.0),
            momentum_burst_min_volume_ratio=mb.get("min_volume_ratio", 1.2),
            momentum_burst_allowed_regimes=mb.get("allowed_regimes", ["chaos", "trend"]),
            momentum_exit_enabled=me.get("enabled", True),
            momentum_exit_min_age_minutes=me.get("min_age_minutes", 15),
            momentum_exit_min_profit_pct=me.get("min_profit_pct", 0.1),
            momentum_exit_rsi_slope_threshold=me.get("rsi_slope_threshold", 1.0),
            testnet=env.lower() == "testnet",
            dry_run=data.get("dry_run", False),
        )


# =============================================================================
# Bot Orchestrator
# =============================================================================

class ConservativeBot:
    """
    Main orchestrator for ML-based trading system.

    Architecture:
        MarketState → XGBoost(features) → P(TP) → RiskManager → Execution

    Critical design principles:
    1. XGBoost predicts P(TP) for every asset/direction
    2. Execute top-N candidates sorted by P(TP)
    3. Kill switch ALWAYS active
    4. Physical gates: cooldown, protections, spread, max_positions
    """

    SERVICE_ORDER = [
        "kill_switch",     # MUST be first - safety critical
        "market_state",    # Data provider
        "risk_manager",    # Sizing
        "execution",       # Order placement
        "telegram",        # Notifications (non-critical)
        "whatsapp",              # Notifications (non-critical)
        "performance_monitor",   # Trade performance tracking
        "counterfactual_logger", # Rejected trade analysis
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

        # Services
        self._services: Dict[str, Any] = {}

        # ML Model
        self._ml_model: MLTradeModel = MLTradeModel()

        # State
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._start_time: Optional[datetime] = None

        # Background tasks
        self._strategy_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
        self._retrain_task: Optional[asyncio.Task] = None

        # Consecutive scan error counter for ntfy alerts (#17)
        self._consecutive_scan_errors: int = 0

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
    def kill_switch(self) -> Optional[KillSwitchService]:
        return self._services.get("kill_switch")

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
            "Config loaded: assets=%s, risk=%.1f%%, max_dd=%.1f%%",
            self._config.assets,
            self._config.per_trade_pct,
            self._config.max_drawdown_pct,
        )
        return self._config

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

        # Kill Switch - CRITICAL, must be first
        ks_config = KillSwitchConfig(
            enabled=True,
            daily_loss_pct=cfg.daily_loss_pct,
            weekly_loss_pct=cfg.weekly_loss_pct,
            max_drawdown_pct=cfg.max_drawdown_pct,
            check_interval_seconds=60,
        )
        self._services["kill_switch"] = create_kill_switch(
            bus=self._bus, config=ks_config,
        )

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
            per_trade_pct=cfg.per_trade_pct,
            max_per_trade_pct=cfg.max_per_trade_pct,
            max_positions=cfg.max_positions,
            max_exposure_pct=cfg.max_exposure_pct,
            max_position_pct=cfg.max_position_pct,
            max_daily_trades=cfg.max_daily_trades,
            leverage=cfg.leverage,
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
                self.leverage = int(cfg.leverage)
                self.breakeven_threshold_pct = cfg.breakeven_threshold_pct

        class _StopsConfig:
            def __init__(self, cfg: ConservativeConfig):
                self.initial_atr_mult = cfg.initial_atr_mult
                self.trailing_atr_mult = cfg.trailing_atr_mult
                self.minimal_roi = cfg.minimal_roi

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

        # Protection Manager
        self._services["protection_manager"] = ProtectionManager(
            config=self._raw_config, telegram=telegram_service,
        )

        # Performance Monitor
        whatsapp_svc = self._services.get("whatsapp")

        from .services.performance_monitor import PerformanceMonitorService
        self._services["performance_monitor"] = PerformanceMonitorService(
            bus=self._bus, config=self._raw_config, whatsapp=whatsapp_svc,
        )

        # Counterfactual Logger
        from .services.counterfactual_logger import CounterfactualLoggerService
        self._services["counterfactual_logger"] = CounterfactualLoggerService(
            bus=self._bus, config=self._raw_config, whatsapp=whatsapp_svc,
            take_profit_pct=cfg.take_profit_pct, stop_loss_pct=cfg.stop_loss_pct,
        )

        # Wire performance_monitor into risk_manager for cooldown trade history
        risk_mgr = self._services.get("risk_manager")
        perf_mon = self._services.get("performance_monitor")
        if risk_mgr and perf_mon:
            risk_mgr._performance_monitor = perf_mon

        # ML Model - required
        model_loaded = self._ml_model.load(cfg.ml_model_path)
        if model_loaded:
            effective_threshold = cfg.ml_min_probability
            logger.info(
                "ML model loaded: model_calibrated=%.4f, config_threshold=%.2f, effective=%.4f",
                self._ml_model.optimal_threshold or 0.0,
                cfg.ml_min_probability,
                effective_threshold,
            )
        else:
            logger.warning(
                "ML model not found at %s — bot will skip trades until model is trained. "
                "Run: python3 -m crypto_bot.scripts.retrain_model",
                cfg.ml_model_path,
            )

        logger.info(
            "Initialized %d services: %s",
            len(self._services), ", ".join(self._services.keys()),
        )

    # =========================================================================
    # Evaluation Loop
    # =========================================================================

    async def _strategy_loop(self) -> None:
        """Main evaluation loop: scan all assets with ML model.

        Scans are aligned to the 15-minute bar close + 30s buffer, so the bot
        always evaluates freshly closed candles rather than mid-candle data.
        This means scans fire at ~XX:00:30, XX:15:30, XX:30:30, XX:45:30 UTC.
        """
        logger.info("ML evaluation loop started")

        await asyncio.sleep(10)  # Let services initialize

        bar_seconds = 15 * 60  # 15m bars
        buffer = 30  # seconds after bar close to let data feed update

        while self._running and not self._shutdown_event.is_set():
            try:
                await self._evaluate_all_assets()
                self._consecutive_scan_errors = 0

                # Align next scan to the next 15m bar close + buffer
                now = datetime.now(timezone.utc)
                current_ts = int(now.timestamp())
                next_bar_close = (current_ts // bar_seconds + 1) * bar_seconds
                sleep_seconds = next_bar_close + buffer - current_ts
                if sleep_seconds <= 0:
                    sleep_seconds = bar_seconds  # fallback: full bar

                next_scan_time = datetime.fromtimestamp(
                    current_ts + sleep_seconds, tz=timezone.utc
                )
                logger.info(
                    "Next scan at %s UTC (in %ds, aligned to bar close + %ds)",
                    next_scan_time.strftime("%H:%M:%S"),
                    sleep_seconds,
                    buffer,
                )
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(), timeout=sleep_seconds,
                    )
                    break
                except asyncio.TimeoutError:
                    pass

            except asyncio.CancelledError:
                logger.debug("Evaluation loop cancelled")
                break
            except Exception as e:
                self._consecutive_scan_errors += 1
                logger.error("Evaluation loop error: %s", e, exc_info=True)
                if self._consecutive_scan_errors >= 5 and self._bus:
                    await self._bus.publish(Topic.RISK_ALERTS, {
                        "alert_type": "scan_errors",
                        "message": f"5 consecutive scan errors. Latest: {e}",
                        "consecutive_errors": self._consecutive_scan_errors,
                    })
                await asyncio.sleep(60)

    async def _evaluate_all_assets(self) -> None:
        """Evaluate all assets with XGBoost ML model.

        Flow: MarketState → extract features → predict P(TP) → sort → execute top N
        """
        # --- Physical gates ---
        kill_switch = self._services.get("kill_switch")
        if kill_switch and not kill_switch.is_trading_allowed():
            logger.warning("Trading paused by kill switch: %s", kill_switch.get_status().value)
            return

        risk_manager = self._services.get("risk_manager")
        if risk_manager:
            is_cooldown, cooldown_state = await risk_manager.check_cooldown_required()
            if is_cooldown and cooldown_state:
                remaining = cooldown_state.time_remaining()
                logger.warning(
                    "Trading paused by COOLDOWN: %s (resuming in %d min)",
                    cooldown_state.reason.value if cooldown_state.reason else "unknown",
                    remaining // 60 if remaining else 0,
                )
                return

        protection_manager = self._services.get("protection_manager")
        if protection_manager:
            can_trade, protection_result = await protection_manager.check_all_protections()
            if not can_trade and protection_result:
                logger.warning(
                    "Trading paused by PROTECTION: %s - %s",
                    protection_result.protection_name, protection_result.reason,
                )
                return

        # --- Get market states ---
        market_state_svc = self._services.get("market_state")
        if not market_state_svc:
            return

        states = market_state_svc.get_all_states()
        if not states:
            logger.warning("No market states available")
            return

        # --- Filter out stale market states (#23) ---
        now = datetime.now(timezone.utc)
        scan_interval_seconds = self.config.scan_interval_minutes * 60
        max_age = timedelta(seconds=scan_interval_seconds * 2)
        fresh_states = {
            sym: state for sym, state in states.items()
            if state.timestamp and (now - state.timestamp) <= max_age
        }
        stale_count = len(states) - len(fresh_states)
        if stale_count > 0:
            logger.warning("Filtered %d stale market states (max age %ds)", stale_count, scan_interval_seconds * 2)
        states = fresh_states
        if not states:
            logger.warning("All market states are stale, skipping evaluation")
            return

        # --- Update counterfactual logger with fresh market states ---
        cf_logger = self._services.get("counterfactual_logger")
        if cf_logger and states:
            cf_logger.update_market_states(states)

        # --- Update execution engine with fresh market states ---
        exec_engine = self._services.get("execution")
        if exec_engine and states:
            exec_engine.update_market_states(states)

        # --- ML model gate ---
        if not self._ml_model.is_loaded:
            logger.debug("ML model not loaded, skipping evaluation")
            return

        # --- Score all assets with ML ---
        candidates: list[tuple[str, Setup, float, str]] = []
        all_scores: list[tuple[str, float]] = []  # diagnostic: track ALL scores
        symbols_with_positions: set = set()

        if risk_manager:
            symbols_with_positions = set(risk_manager._open_positions.keys())

        threshold = self.config.ml_min_probability

        # Guard: volume breakout requires 13-feature model
        n_model_features = getattr(self._ml_model._model, "n_features_in_", 0)
        vb_enabled = (
            self.config.volume_breakout_enabled
            and n_model_features >= 13
        )
        if self.config.volume_breakout_enabled and not vb_enabled:
            logger.debug(
                "Volume breakout disabled: model has %d features (need 13). Retrain required.",
                n_model_features,
            )

        # Guard: momentum burst requires 14-feature model
        mb_enabled = (
            self.config.momentum_burst_enabled
            and n_model_features >= 14
        )
        if self.config.momentum_burst_enabled and not mb_enabled:
            logger.debug(
                "Momentum burst disabled: model has %d features (need 14). Retrain required.",
                n_model_features,
            )

        vb_allowed_regimes = {r.lower() for r in self.config.volume_breakout_allowed_regimes}
        mb_allowed_regimes = {r.lower() for r in self.config.momentum_burst_allowed_regimes}

        # BTC state for ML context features (altcoins use BTC as macro indicator)
        btc_state = states.get("BTC")

        regime_skipped: int = 0
        breakout_evaluated: int = 0
        burst_evaluated: int = 0
        sl_pct_frac = self.config.stop_loss_pct / 100  # e.g. 1.0 -> 0.01
        min_ema_gap_pct = 0.001  # Fix 4: minimum 0.10% EMA9/EMA21 gap

        for symbol, state in states.items():
            if symbol in symbols_with_positions:
                continue

            # Fix 2: Enforce min_volume_24h filter
            if state.volume_24h is not None and float(state.volume_24h) < self.config.min_volume_24h:
                logger.debug("Skipping %s: 24h volume $%.0f < $%.0f min", symbol, float(state.volume_24h), self.config.min_volume_24h)
                continue

            # Fix 3: ATR vs SL gate — skip if single candle range exceeds stop loss
            if state.atr_pct is not None and float(state.atr_pct) > self.config.stop_loss_pct:
                logger.debug("Skipping %s: ATR %.2f%% > SL %.2f%%", symbol, float(state.atr_pct), self.config.stop_loss_pct)
                continue

            best_prob = -1.0
            best_direction: Direction = Direction.FLAT
            best_reason = ""
            best_setup_type = SetupType.MOMENTUM
            best_signal_type = 0.0

            # Track highest score across all paths (for counterfactual near-miss logging)
            top_scored_prob = 0.0
            top_scored_direction: Direction = Direction.FLAT

            # --- PATH 1 (existing): TREND only → EMA direction → ML(signal_type=0.0) ---
            if state.regime == Regime.TREND:
                direction = state.trend_direction
                if direction != Direction.FLAT:
                    # Fix 4: Minimum EMA gap — skip noise crossovers
                    ema_gap_ok = True
                    if state.ema9 is not None and state.ema21 is not None and float(state.ema21) > 0:
                        ema_gap = abs(float(state.ema9) - float(state.ema21)) / float(state.ema21)
                        if ema_gap < min_ema_gap_pct:
                            ema_gap_ok = False
                            logger.debug("Skipping %s EMA: gap %.4f%% < %.4f%% min", symbol, ema_gap * 100, min_ema_gap_pct * 100)

                    # Gate: Funding rate filter (skip if paying excessive funding)
                    funding_ok = True
                    if direction == Direction.LONG and state.funding_rate and float(state.funding_rate) > 0.0005:
                        funding_ok = False
                    if direction == Direction.SHORT and state.funding_rate and float(state.funding_rate) < -0.0005:
                        funding_ok = False

                    # Gate: Multi-timeframe alignment (1h EMA9/EMA21 must agree with direction)
                    mtf_aligned = True
                    if state.ema9_1h is not None and state.ema21_1h is not None:
                        if direction == Direction.LONG and float(state.ema9_1h) <= float(state.ema21_1h):
                            mtf_aligned = False
                        elif direction == Direction.SHORT and float(state.ema9_1h) >= float(state.ema21_1h):
                            mtf_aligned = False

                    # Gate: RSI hard floor for SHORT entries
                    rsi_ok = True
                    if direction == Direction.SHORT and state.rsi and float(state.rsi) < 35:
                        rsi_ok = False

                    if funding_ok and mtf_aligned and rsi_ok and ema_gap_ok:
                        dir_float = 1.0 if direction == Direction.LONG else -1.0
                        features = self._ml_model.extract_features(
                            state, signal_type=0.0, direction=dir_float,
                            btc_state=btc_state,
                        )
                        prob, reason = self._ml_model.predict(features)
                        all_scores.append((f"{symbol}/ema", prob))
                        if prob > top_scored_prob:
                            top_scored_prob = prob
                            top_scored_direction = direction
                        if prob >= threshold and prob > best_prob:
                            best_prob = prob
                            best_direction = direction
                            best_reason = reason
                            best_setup_type = SetupType.MOMENTUM
                            best_signal_type = 0.0

            # --- PATH 2: CHAOS+TREND → volume check → price direction → ML(signal_type=1.0) ---
            if vb_enabled and state.regime.value.lower() in vb_allowed_regimes:
                vb_direction = self._breakout_direction(state)
                # Gate: Funding rate filter
                if vb_direction == Direction.LONG and state.funding_rate and float(state.funding_rate) > 0.0005:
                    vb_direction = Direction.FLAT
                if vb_direction == Direction.SHORT and state.funding_rate and float(state.funding_rate) < -0.0005:
                    vb_direction = Direction.FLAT
                # Fix 5: RSI direction alignment — don't long overbought, don't short oversold
                if vb_direction == Direction.LONG and state.rsi and float(state.rsi) > 70:
                    vb_direction = Direction.FLAT
                if vb_direction == Direction.SHORT and state.rsi and float(state.rsi) < 30:
                    vb_direction = Direction.FLAT
                if vb_direction != Direction.FLAT and self._is_volume_breakout(state):
                    breakout_evaluated += 1
                    vb_dir_float = 1.0 if vb_direction == Direction.LONG else -1.0
                    features = self._ml_model.extract_features(
                        state, signal_type=1.0, direction=vb_dir_float,
                        btc_state=btc_state,
                    )
                    prob, reason = self._ml_model.predict(features)
                    all_scores.append((f"{symbol}/vb", prob))
                    if prob > top_scored_prob:
                        top_scored_prob = prob
                        top_scored_direction = vb_direction
                    if prob >= threshold and prob > best_prob:
                        best_prob = prob
                        best_direction = vb_direction
                        best_reason = reason
                        best_setup_type = SetupType.VOLUME_BREAKOUT
                        best_signal_type = 1.0

            # --- PATH 3: CHAOS+TREND → RSI acceleration → ML(signal_type=2.0) ---
            if mb_enabled and state.regime.value.lower() in mb_allowed_regimes:
                if self._is_momentum_burst(state):
                    mb_direction = self._momentum_burst_direction(state)
                    # Gate: Funding rate filter
                    if mb_direction == Direction.LONG and state.funding_rate and float(state.funding_rate) > 0.0005:
                        mb_direction = Direction.FLAT
                    if mb_direction == Direction.SHORT and state.funding_rate and float(state.funding_rate) < -0.0005:
                        mb_direction = Direction.FLAT
                    # Fix 5: RSI direction alignment — don't long overbought, don't short oversold
                    if mb_direction == Direction.LONG and state.rsi and float(state.rsi) > 70:
                        mb_direction = Direction.FLAT
                    if mb_direction == Direction.SHORT and state.rsi and float(state.rsi) < 30:
                        mb_direction = Direction.FLAT
                    if mb_direction != Direction.FLAT:
                        burst_evaluated += 1
                        mb_dir_float = 1.0 if mb_direction == Direction.LONG else -1.0
                        features = self._ml_model.extract_features(
                            state, signal_type=2.0, direction=mb_dir_float,
                            btc_state=btc_state,
                        )
                        prob, reason = self._ml_model.predict(features)
                        all_scores.append((f"{symbol}/mb", prob))
                        if prob > top_scored_prob:
                            top_scored_prob = prob
                            top_scored_direction = mb_direction
                        if prob >= threshold and prob > best_prob:
                            best_prob = prob
                            best_direction = mb_direction
                            best_reason = reason
                            best_setup_type = SetupType.MOMENTUM_BURST
                            best_signal_type = 2.0

            # No path fired above threshold
            if best_prob < threshold:
                # Counterfactual: log near-miss ML rejections (prob within 0.10 of threshold)
                if (cf_logger and top_scored_prob > 0
                        and top_scored_prob >= threshold - 0.10
                        and top_scored_direction != Direction.FLAT):
                    cf_logger.log_rejection(
                        symbol=symbol,
                        direction=top_scored_direction.value,
                        entry_price=float(state.close),
                        reason="ml_threshold",
                        ml_probability=top_scored_prob,
                    )

                regime_in_any = (
                    state.regime == Regime.TREND
                    or (vb_enabled and state.regime.value.lower() in vb_allowed_regimes)
                    or (mb_enabled and state.regime.value.lower() in mb_allowed_regimes)
                )
                if not regime_in_any:
                    regime_skipped += 1
                elif state.trend_direction == Direction.FLAT and state.regime == Regime.TREND:
                    regime_skipped += 1
                continue

            # Create Setup
            sl_pct = Decimal(str(self.config.stop_loss_pct))
            entry_price = state.close
            if best_direction == Direction.LONG:
                stop_price = entry_price * (Decimal("1") - sl_pct / Decimal("100"))
            else:
                stop_price = entry_price * (Decimal("1") + sl_pct / Decimal("100"))

            setup = Setup(
                id=f"ml_{uuid.uuid4().hex[:8]}",
                symbol=symbol,
                timestamp=datetime.now(timezone.utc),
                setup_type=best_setup_type,
                direction=best_direction,
                regime=state.regime,
                entry_price=entry_price,
                stop_price=stop_price,
                stop_distance_pct=sl_pct,
                atr=state.atr,
                atr_pct=state.atr_pct,
                adx=state.adx,
                rsi=state.rsi,
                setup_quality=Decimal(str(round(best_prob, 4))),
                confidence=Decimal(str(round(best_prob, 4))),
            )

            signal_labels = {0.0: "ML_SELECT", 1.0: "VOL_BREAKOUT", 2.0: "MOM_BURST"}
            signal_label = signal_labels.get(best_signal_type, "ML_SELECT")
            logger.info(
                "%s | %s %s | P(TP)=%.1f%% | regime=%s | %s",
                signal_label,
                best_direction.value.upper(), symbol, best_prob * 100,
                state.regime.value.upper(), best_reason,
            )
            candidates.append((symbol, setup, best_prob, best_reason))

        # --- Diagnostic: log P(TP) distribution for evaluated assets ---
        if all_scores:
            all_scores.sort(key=lambda x: x[1], reverse=True)
            top5 = all_scores[:5]
            above = sum(1 for _, p in all_scores if p >= threshold)
            below_near = sum(1 for _, p in all_scores if threshold - 0.05 <= p < threshold)
            below_far = sum(1 for _, p in all_scores if p < threshold - 0.05)
            logger.info(
                "P(TP) distribution | top5: %s | above %.0f%%: %d, near-miss (%.0f%%-%.0f%%): %d, far below: %d, total scored: %d",
                ", ".join(f"{s}={p:.1%}" for s, p in top5),
                threshold * 100, above,
                (threshold - 0.05) * 100, threshold * 100, below_near,
                below_far, len(all_scores),
            )

        # Count assets evaluated
        total_evaluated = len(all_scores)
        logger.info(
            "Scan: %d assets, %d scored (%d breakout, %d burst), %d skipped, %d candidates (threshold=%.2f, vb=%s, mb=%s)",
            len(states), total_evaluated, breakout_evaluated, burst_evaluated,
            regime_skipped, len(candidates), threshold,
            "ON" if vb_enabled else "OFF",
            "ON" if mb_enabled else "OFF",
        )

        # --- Sort by P(TP) desc, execute top N ---
        if not candidates:
            return

        # Cross-sectional momentum rank as tiebreaker (80% ML + 20% momentum)
        if len(candidates) > 1:
            # Momentum proxy: signed EMA spread = (ema9 - ema21) / ema21
            # Captures both direction and magnitude of momentum
            momentums: list[float] = []
            for c in candidates:
                sym = c[0]
                st = states.get(sym)
                if st is not None and st.ema9 is not None and st.ema21 is not None:
                    ema21_val = float(st.ema21)
                    if ema21_val != 0:
                        momentums.append(abs(float(st.ema9) - ema21_val) / ema21_val)
                    else:
                        momentums.append(0.0)
                else:
                    momentums.append(0.0)
            ranks = rankdata(momentums, method="ordinal") / len(momentums)
            candidates = [
                (c[0], c[1], 0.8 * c[2] + 0.2 * ranks[i], c[3])
                for i, c in enumerate(candidates)
            ]

        candidates.sort(key=lambda x: x[2], reverse=True)

        if risk_manager:
            pos_count = len(risk_manager._open_positions) + len(risk_manager._pending_intents)
            available_slots = max(0, self.config.max_positions - pos_count)
        else:
            available_slots = self.config.max_positions

        ranking_str = ", ".join(f"{c[0]}({c[2]:.2f})" for c in candidates)
        if available_slots > 0:
            logger.info(
                "Collected %d candidates, %d slots available: [%s]",
                len(candidates), available_slots, ranking_str,
            )
        else:
            logger.info(
                "Found %d candidates but all %d slots full: [%s]",
                len(candidates), self.config.max_positions, ranking_str,
            )

        executed = 0
        for _, setup, _, _ in candidates:
            if executed >= available_slots:
                break
            if risk_manager:
                cur_count = len(risk_manager._open_positions) + len(risk_manager._pending_intents)
                if cur_count >= self.config.max_positions:
                    logger.info("All slots filled during execution, stopping")
                    break
            if await self._execute_setup(setup):
                executed += 1

    def _is_volume_breakout(self, state: MarketState) -> bool:
        """Check if current candle qualifies as a volume breakout.

        Conditions:
        - volume_ratio >= config threshold (volume spike vs SMA20)
        - candle body >= config threshold (volume moved the price)
        - atr_pct >= config threshold (market is alive)
        - RSI within config bounds (not in extremes)
        """
        cfg = self.config
        vol_ratio = float(state.volume_ratio) if state.volume_ratio is not None else 0.0
        if vol_ratio < cfg.volume_breakout_min_volume_ratio:
            return False

        close = float(state.close)
        open_price = float(state.open)
        if open_price <= 0:
            return False
        candle_body_pct = abs(close - open_price) / open_price * 100
        if candle_body_pct < cfg.volume_breakout_min_candle_body_pct:
            return False

        if float(state.atr_pct) < cfg.volume_breakout_min_atr_pct:
            return False

        rsi = float(state.rsi)
        if not (cfg.volume_breakout_rsi_min <= rsi <= cfg.volume_breakout_rsi_max):
            return False

        return True

    @staticmethod
    def _breakout_direction(state: MarketState) -> Direction:
        """Determine breakout direction from price momentum (NOT EMA).

        LONG: close > open AND close > prev_close
        SHORT: close < open AND close < prev_close
        """
        close = float(state.close)
        open_price = float(state.open)
        prev_close = float(state.prev_close) if state.prev_close is not None else close

        if close > open_price and close > prev_close:
            return Direction.LONG
        if close < open_price and close < prev_close:
            return Direction.SHORT
        return Direction.FLAT

    def _is_momentum_burst(self, state: MarketState) -> bool:
        """Check if current bar qualifies as a momentum burst.

        Conditions:
        - RSI slope (rsi_slope from MarketState) >= config threshold
        - Price > EMA9 for LONG (close < EMA9 for SHORT)
        - Candle body >= config threshold
        - RSI <= max_rsi_entry for LONG (>= 100-max for SHORT)
        - Volume ratio >= config threshold
        """
        cfg = self.config
        rsi_slope = float(state.rsi_slope)

        # Need significant RSI movement in either direction
        if abs(rsi_slope) < cfg.momentum_burst_min_rsi_slope:
            return False

        close = float(state.close)
        open_price = float(state.open)
        if open_price <= 0:
            return False

        candle_body_pct = abs(close - open_price) / open_price * 100
        if candle_body_pct < cfg.momentum_burst_min_candle_body_pct:
            return False

        vol_ratio = float(state.volume_ratio) if state.volume_ratio is not None else 0.0
        if vol_ratio < cfg.momentum_burst_min_volume_ratio:
            return False

        return True

    def _momentum_burst_direction(self, state: MarketState) -> Direction:
        """Determine momentum burst direction from RSI slope + price action.

        LONG: RSI rising + close > open + close > EMA9 + RSI <= max_entry
        SHORT: RSI falling + close < open + close < EMA9 + RSI >= (100-max_entry)
        """
        cfg = self.config
        rsi_slope = float(state.rsi_slope)
        close = float(state.close)
        open_price = float(state.open)
        ema9 = float(state.ema9) if state.ema9 is not None else close
        rsi = float(state.rsi)

        if (rsi_slope >= cfg.momentum_burst_min_rsi_slope
                and close > ema9
                and close > open_price
                and rsi <= cfg.momentum_burst_max_rsi_entry):
            return Direction.LONG

        if (rsi_slope <= -cfg.momentum_burst_min_rsi_slope
                and close < ema9
                and close < open_price
                and rsi >= (100 - cfg.momentum_burst_max_rsi_entry)):
            return Direction.SHORT

        return Direction.FLAT

    async def _execute_setup(self, setup: Setup) -> bool:
        """Execute a setup: check spread and tick size, then publish to risk manager.

        Returns True if setup was forwarded, False if skipped.
        """
        # Check bid-ask spread before entering
        if self._exchange:
            spread_pct = await self._exchange.get_spread_pct(setup.symbol)
            if spread_pct > self.config.max_spread_pct:
                logger.info(
                    "SKIP %s: bid-ask spread %.3f%% > max %.2f%% (illiquid)",
                    setup.symbol, spread_pct, self.config.max_spread_pct,
                )
                cf_logger = self._services.get("counterfactual_logger")
                if cf_logger:
                    cf_logger.log_rejection(
                        symbol=setup.symbol,
                        direction=setup.direction.value,
                        entry_price=float(setup.entry_price),
                        reason="spread",
                        ml_probability=float(getattr(setup, "confidence", 0) or 0),
                    )
                return False

        # Check tick size vs TP/SL — skip if rounding would collapse stops
        price = float(setup.entry_price)
        if price > 0:
            from math import log10, floor
            magnitude = floor(log10(price))
            max_decimals = min(4, max(0, 4 - magnitude))
            min_tick = 10 ** (-max_decimals)
            tp_distance = price * self.config.stop_loss_pct / 100  # SL is smaller than TP
            if tp_distance < min_tick * 1.5:
                logger.info(
                    "SKIP %s: price $%.6f too low for TP/SL (tick=%.6f, SL_dist=%.6f)",
                    setup.symbol, price, min_tick, tp_distance,
                )
                cf_logger = self._services.get("counterfactual_logger")
                if cf_logger:
                    cf_logger.log_rejection(
                        symbol=setup.symbol,
                        direction=setup.direction.value,
                        entry_price=price,
                        reason="tick_size",
                        ml_probability=float(getattr(setup, "confidence", 0) or 0),
                    )
                return False

        # Publish setup for risk manager
        if self._bus:
            await self._bus.publish(Topic.SETUPS, setup.model_dump())
        return True

    # =========================================================================
    # ML Model Retraining
    # =========================================================================

    async def _retrain_loop(self) -> None:
        """Periodically retrain the ML model with fresh data."""
        await asyncio.sleep(60)  # Let bot fully initialize

        interval = self.config.ml_retrain_interval_days * 86400

        while self._running and not self._shutdown_event.is_set():
            try:
                await self._do_retrain()
            except Exception as e:
                logger.error("Retrain error: %s", e, exc_info=True)
                if self._bus:
                    await self._bus.publish(Topic.RISK_ALERTS, {
                        "alert_type": "retrain_failure",
                        "message": f"ML model retrain failed: {e}",
                    })

            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass

    async def _do_retrain(self) -> None:
        """Execute model retraining if model is stale or missing."""
        model_path = Path(self.config.ml_model_path)
        if model_path.exists():
            age_days = (time.time() - model_path.stat().st_mtime) / 86400
            if age_days < self.config.ml_retrain_interval_days:
                logger.info("Model is %.1f days old, retrain not needed yet", age_days)
                return

        logger.info("Starting ML model retrain (%d days of data)...", self.config.ml_retrain_days)

        metrics = await asyncio.get_event_loop().run_in_executor(None, self._retrain_sync)

        if metrics is None:
            logger.warning("Retrain produced no results (insufficient data)")
            return

        if metrics["cv_auc_mean"] < 0.55:
            logger.warning(
                "New model CV AUC %.4f too low (<0.55), keeping old model",
                metrics["cv_auc_mean"],
            )
            return

        self._ml_model.load(self.config.ml_model_path)
        logger.info(
            "Model retrained and loaded: CV AUC=%.4f, %d samples",
            metrics["cv_auc_mean"], metrics["n_samples"],
        )

    def _retrain_sync(self) -> dict | None:
        """Synchronous retrain — runs in thread pool."""
        from backtesting.api import get_all_assets
        from backtesting.config import load_config
        from crypto_bot.services.ml_dataset import generate_dataset

        cfg = load_config()
        all_assets = get_all_assets()
        symbols = [s for s in all_assets if s not in cfg.exclude_symbols]

        df = generate_dataset(symbols, days=self.config.ml_retrain_days, cfg=cfg)
        if df.empty or len(df) < 50:
            return None

        model = MLTradeModel()
        metrics = model.train(df)
        Path(self.config.ml_model_path).parent.mkdir(parents=True, exist_ok=True)
        model.save(self.config.ml_model_path)
        return metrics

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

    async def _init_regime_for_open_positions(self) -> None:
        """Initialize confirmed regime to TREND for symbols with open positions.

        After a restart, MarketStateService has an empty _confirmed_regime dict.
        The first reading would become confirmed immediately, bypassing the
        N-bar confirmation requirement. This seeds TREND for any symbol where
        the execution engine already has an open position, so regime change
        confirmation works correctly from the first scan.
        """
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
        logger.info("HLQuantBot v4 - XGBoost ML Starting")
        logger.info("=" * 60)

        try:
            if self._config is None:
                self._load_config()

            setup_logging(log_level="INFO")

            await self._init_message_bus()
            await self._init_exchange()
            await self._load_dynamic_assets()

            self._init_services()

            await self._start_services()

            # Initialize confirmed regime for open positions to prevent
            # false regime changes after restart (Problem 4 fix)
            await self._init_regime_for_open_positions()

            self._strategy_task = asyncio.create_task(
                self._strategy_loop(), name="ml_evaluation_loop",
            )
            self._health_task = asyncio.create_task(
                self._health_loop(), name="health_loop",
            )
            self._retrain_task = asyncio.create_task(
                self._retrain_loop(), name="retrain_loop",
            )

            self._running = True
            self._start_time = datetime.now(timezone.utc)
            self._shutdown_event.clear()

            if self._exchange:
                account = await self._exchange.get_account_state()
                equity = account.get("equity", 0)
                if self.kill_switch:
                    await self.kill_switch.update_equity(Decimal(str(equity)))

            effective_threshold = self.config.ml_min_probability
            logger.info("=" * 60)
            logger.info(
                "HLQuantBot v4 Running (%s) | ML threshold: %.0f%%",
                "TESTNET" if self.config.testnet else "MAINNET",
                effective_threshold * 100,
            )
            logger.info("Assets: %d", len(self.config.assets))
            logger.info("Risk per trade: %.1f%% | Max DD: %.1f%%",
                        self.config.per_trade_pct, self.config.max_drawdown_pct)
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
        logger.info("HLQuantBot v4 Stopping")
        logger.info("=" * 60)

        self._running = False
        self._shutdown_event.set()

        for task in [self._strategy_task, self._health_task, self._retrain_task]:
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
        logger.info("HLQuantBot v4 Stopped")
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
                "risk_per_trade": self.config.per_trade_pct if self._config else 0,
                "max_drawdown": self.config.max_drawdown_pct if self._config else 0,
            },
            "services": service_status,
            "ml_model_loaded": self._ml_model.is_loaded,
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

    parser = argparse.ArgumentParser(description="HLQuantBot v4 - XGBoost ML Trading System")
    parser.add_argument("-c", "--config", default="crypto_bot/config/trading.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    asyncio.run(main(config_path=args.config))

# Alias for backward compatibility with tests
HLQuantBot = ConservativeBot
