#!/usr/bin/env python3
"""
HLQuantBot v4 - XGBoost ML Trade Selector
==========================================

Trading system with ML-based trade selection and strict risk controls.

Architecture:
    MarketState → XGBoost.predict(features) → P(TP) → RiskManager → Execution

This orchestrator:
1. Loads configuration from trading.yaml
2. Initializes core services (database, message bus)
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
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from database.db import Database

from .core.enums import Topic
from .core.models import Direction, MarketState, Setup, SetupType

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

    file_handler = logging.FileHandler(log_dir / "conservative_bot.log")
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
    logging.getLogger("asyncpg").setLevel(logging.WARNING)

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
    max_positions: int
    max_exposure_pct: float
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

    # Regime (for MarketStateService indicator computation)
    trend_adx_min: float
    range_adx_max: float
    ema_slope_threshold: float
    choppiness_range_min: float
    regime_confirmation_bars: int

    # ML Model
    ml_model_path: str
    ml_min_probability: float

    # Execution
    prefer_limit: bool
    max_slippage_pct: float
    max_spread_pct: float

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
            per_trade_pct=risk.get("per_trade_pct", 10.0),
            max_positions=risk.get("max_positions", 3),
            max_exposure_pct=risk.get("max_exposure_pct", 300),
            leverage=risk.get("leverage", 10),
            daily_loss_pct=ks.get("daily_loss_pct", 8.0),
            weekly_loss_pct=ks.get("weekly_loss_pct", 15.0),
            max_drawdown_pct=ks.get("max_drawdown_pct", 30.0),
            initial_atr_mult=stops.get("initial_atr_mult", 2.5),
            trailing_atr_mult=stops.get("trailing_atr_mult", 2.5),
            minimal_roi=stops.get("minimal_roi", {}),
            stop_loss_pct=stops.get("stop_loss_pct", 0.8),
            take_profit_pct=stops.get("take_profit_pct", 1.6),
            trend_adx_min=regime.get("trend_adx_min", 25.0),
            range_adx_max=regime.get("range_adx_max", 20.0),
            ema_slope_threshold=regime.get("ema_slope_threshold", 0.0003),
            choppiness_range_min=regime.get("choppiness_range_min", 60.0),
            regime_confirmation_bars=regime.get("confirmation_bars", 2),
            ml_model_path=ml.get("model_path", "models/trade_model.joblib"),
            ml_min_probability=ml.get("min_probability", 0.55),
            prefer_limit=execution.get("prefer_limit", True),
            max_slippage_pct=execution.get("max_slippage_pct", 0.1),
            max_spread_pct=execution.get("max_spread_pct", 0.10),
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
        "whatsapp",        # Notifications (non-critical)
    ]

    def __init__(
        self,
        config_path: str = "simple_bot/config/trading.yaml",
        config: Optional[ConservativeConfig] = None,
    ) -> None:
        self.config_path = config_path
        self._config: Optional[ConservativeConfig] = config

        # Core components
        self._db: Optional[Database] = None
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

    async def _init_database(self) -> Database:
        """Initialize database connection."""
        logger.info("Connecting to database...")
        dsn = os.getenv(
            "DATABASE_URL",
            "postgresql://hlquant:hlquant@localhost:5432/hlquantbot"
        )
        self._db = Database(dsn)
        await self._db.connect(min_size=2, max_size=10)
        if not await self._db.health_check():
            raise RuntimeError("Database health check failed")
        logger.info("Database connected")
        return self._db

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
            bus=self._bus, db=self._db, config=ks_config,
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
            trend_adx_min=cfg.trend_adx_min,
            range_adx_max=cfg.range_adx_max,
            ema_slope_threshold=cfg.ema_slope_threshold,
            choppiness_range_min=cfg.choppiness_range_min,
            regime_confirmation_bars=cfg.regime_confirmation_bars,
        )
        self._services["market_state"] = create_market_state_service(
            bus=self._bus, db=self._db, config=ms_config, testnet=cfg.testnet,
        )

        # Risk Manager
        risk_config = RiskConfig(
            per_trade_pct=cfg.per_trade_pct,
            max_positions=cfg.max_positions,
            max_exposure_pct=cfg.max_exposure_pct,
            leverage=cfg.leverage,
            trailing_atr_mult=cfg.trailing_atr_mult,
            max_slippage_pct=cfg.max_slippage_pct,
        )
        self._services["risk_manager"] = create_risk_manager(
            bus=self._bus, db=self._db, config=risk_config,
            client=self._exchange, telegram=telegram_service,
        )

        # Execution Engine
        class _ExecConfig:
            def __init__(self, cfg: ConservativeConfig):
                self.order_type = "limit" if cfg.prefer_limit else "market"
                self.max_slippage_pct = cfg.max_slippage_pct
                self.limit_timeout_seconds = 60
                self.retry_attempts = 3
                self.retry_delay_seconds = 5
                self.position_sync_interval = 30
                self.fill_sync_interval = 10

        class _RiskConfig:
            def __init__(self, cfg: ConservativeConfig):
                self.take_profit_pct = cfg.take_profit_pct
                self.stop_loss_pct = cfg.stop_loss_pct
                self.leverage = int(cfg.leverage)

        class _StopsConfig:
            def __init__(self, cfg: ConservativeConfig):
                self.initial_atr_mult = cfg.initial_atr_mult
                self.trailing_atr_mult = cfg.trailing_atr_mult
                self.minimal_roi = cfg.minimal_roi

        class _ServicesConfig:
            def __init__(self, exec_cfg: _ExecConfig):
                self.execution_engine = exec_cfg

        class _ConfigAdapter:
            def __init__(self, cfg: ConservativeConfig):
                self.services = _ServicesConfig(_ExecConfig(cfg))
                self.risk = _RiskConfig(cfg)
                self.stops = _StopsConfig(cfg)

        self._services["execution"] = ExecutionEngineService(
            bus=self._bus, config=_ConfigAdapter(cfg),
            client=self._exchange, db=self._db,
        )

        # Protection Manager
        self._services["protection_manager"] = ProtectionManager(
            config=self._raw_config, db=self._db, telegram=telegram_service,
        )

        # ML Model - required
        model_loaded = self._ml_model.load(cfg.ml_model_path)
        if model_loaded:
            logger.info("ML model loaded from %s", cfg.ml_model_path)
        else:
            logger.warning(
                "ML model not found at %s — bot will skip trades until model is trained. "
                "Run: python3 -m simple_bot.scripts.retrain_model",
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
        """Main evaluation loop: scan all assets with ML model."""
        logger.info("ML evaluation loop started")

        await asyncio.sleep(10)  # Let services initialize

        scan_seconds = self.config.scan_interval_minutes * 60

        while self._running and not self._shutdown_event.is_set():
            try:
                await self._evaluate_all_assets()

                logger.info("Next scan in %d minutes", self.config.scan_interval_minutes)
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(), timeout=scan_seconds,
                    )
                    break
                except asyncio.TimeoutError:
                    pass

            except asyncio.CancelledError:
                logger.debug("Evaluation loop cancelled")
                break
            except Exception as e:
                logger.error("Evaluation loop error: %s", e, exc_info=True)
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

        # --- ML model gate ---
        if not self._ml_model.is_loaded:
            logger.debug("ML model not loaded, skipping evaluation")
            return

        # --- Score all assets with ML ---
        candidates: list[tuple[str, Setup, float, str]] = []
        symbols_with_positions: set = set()

        if risk_manager:
            symbols_with_positions = set(risk_manager._open_positions.keys())

        for symbol, state in states.items():
            if symbol in symbols_with_positions:
                continue

            # Predict P(TP) for both directions, pick best
            best_prob = 0.0
            best_direction: Direction | None = None
            best_reason = ""

            for dir_enc, direction in [(1, Direction.LONG), (0, Direction.SHORT)]:
                features = self._ml_model.extract_features(state, dir_enc)
                prob, reason = self._ml_model.predict(features)
                if prob > best_prob:
                    best_prob = prob
                    best_direction = direction
                    best_reason = reason

            if best_prob < self.config.ml_min_probability or best_direction is None:
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
                setup_type=SetupType.MOMENTUM,
                direction=best_direction,
                regime=state.regime,
                entry_price=entry_price,
                stop_price=stop_price,
                stop_distance_pct=sl_pct,
                atr=state.atr,
                adx=state.adx,
                rsi=state.rsi,
                setup_quality=Decimal(str(round(best_prob, 4))),
                confidence=Decimal(str(round(best_prob, 4))),
            )

            logger.info(
                "ML_SELECT | %s %s | P(TP)=%.1f%% | %s",
                best_direction.value.upper(), symbol, best_prob * 100, best_reason,
            )
            candidates.append((symbol, setup, best_prob, best_reason))

        # --- Sort by P(TP) desc, execute top N ---
        if not candidates:
            return

        candidates.sort(key=lambda x: x[2], reverse=True)

        if risk_manager:
            pos_count = len(risk_manager._open_positions) + len(risk_manager._pending_intents)
            available_slots = max(0, self.config.max_positions - pos_count)
        else:
            available_slots = self.config.max_positions

        top_candidates = candidates[:available_slots]

        ranking_str = ", ".join(f"{c[0]}({c[2]:.2f})" for c in candidates)
        if top_candidates:
            logger.info(
                "Collected %d candidates, executing top %d: [%s]",
                len(candidates), len(top_candidates), ranking_str,
            )
        else:
            logger.info(
                "Found %d candidates but all %d slots full: [%s]",
                len(candidates), self.config.max_positions, ranking_str,
            )

        for _, setup, _, _ in top_candidates:
            if risk_manager:
                cur_count = len(risk_manager._open_positions) + len(risk_manager._pending_intents)
                if cur_count >= self.config.max_positions:
                    logger.info("All slots filled during execution, stopping")
                    break
            await self._execute_setup(setup)

    async def _execute_setup(self, setup: Setup) -> None:
        """Execute a setup: check spread, then publish to risk manager."""
        # Check bid-ask spread before entering
        if self._exchange:
            spread_pct = await self._exchange.get_spread_pct(setup.symbol)
            if spread_pct > self.config.max_spread_pct:
                logger.info(
                    "SKIP %s: bid-ask spread %.3f%% > max %.2f%% (illiquid)",
                    setup.symbol, spread_pct, self.config.max_spread_pct,
                )
                return

        # Publish setup for risk manager
        if self._bus:
            await self._bus.publish(Topic.SETUPS, setup.model_dump())

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

            await self._init_database()
            await self._init_message_bus()
            await self._init_exchange()
            await self._load_dynamic_assets()

            self._init_services()

            await self._start_services()

            self._strategy_task = asyncio.create_task(
                self._strategy_loop(), name="ml_evaluation_loop",
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
                if self.kill_switch:
                    await self.kill_switch.update_equity(Decimal(str(equity)))

            logger.info("=" * 60)
            logger.info(
                "HLQuantBot v4 Running (%s) | ML threshold: %.0f%%",
                "TESTNET" if self.config.testnet else "MAINNET",
                self.config.ml_min_probability * 100,
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
        if self._db:
            await self._db.disconnect()

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

async def main(config_path: str = "simple_bot/config/trading.yaml") -> None:
    """Main entry point."""
    bot = ConservativeBot(config_path=config_path)
    await bot.run()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="HLQuantBot v4 - XGBoost ML Trading System")
    parser.add_argument("-c", "--config", default="simple_bot/config/trading.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    asyncio.run(main(config_path=args.config))

# Alias for backward compatibility with tests
HLQuantBot = ConservativeBot
