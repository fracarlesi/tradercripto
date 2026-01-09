#!/usr/bin/env python3
"""
HLQuantBot Conservative System - Main Orchestrator
====================================================

"Boring but scalable" trading system with strict risk controls.

This orchestrator:
1. Loads conservative trading configuration
2. Initializes core services (database, message bus)
3. Starts specialized services in dependency order:
   - MarketStateService: Fetches data for BTC/ETH only
   - KillSwitchService: Monitors drawdowns (CRITICAL)
   - LLMVetoService: Trade filter (not decision maker)
   - RiskManagerService: Position sizing
   - ExecutionEngineService: Order execution
4. Runs strategies that only trade in appropriate regimes
5. Monitors health and handles graceful shutdown

Target: 1-3% monthly, 5-15 trades/month, max 15% drawdown

Usage:
    python -m simple_bot.main_conservative

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
from .core.models import MarketState, Setup

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
from .services.llm_veto import (
    LLMVetoService,
    LLMVetoConfig,
    create_llm_veto,
)
from .services.execution_engine import (
    ExecutionEngineService,
)

# Strategies
from .strategies.trend_follow import TrendFollowStrategy
from .strategies.mean_reversion import MeanReversionStrategy

# API Client
from .api.hyperliquid import HyperliquidClient


# =============================================================================
# Logging
# =============================================================================

def setup_logging(log_level: str = "INFO") -> logging.Logger:
    """Configure logging for the conservative bot."""
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

    return logging.getLogger("hlquantbot.conservative")


logger = logging.getLogger("hlquantbot.conservative.main")

# Timeframe to seconds mapping
TIMEFRAME_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


# =============================================================================
# Configuration Loader
# =============================================================================

@dataclass
class ConservativeConfig:
    """Configuration for conservative trading system."""

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

    # Regime
    trend_adx_min: float
    range_adx_max: float
    regime_confirmation_bars: int

    # LLM
    llm_enabled: bool
    llm_provider: str
    llm_max_calls: int
    llm_fallback: str

    # Execution
    prefer_limit: bool
    max_slippage_pct: float

    # Strategies
    trend_follow_enabled: bool
    mean_reversion_enabled: bool

    # Environment
    testnet: bool
    dry_run: bool

    @classmethod
    def from_yaml(cls, path: str) -> "ConservativeConfig":
        """Load configuration from YAML file."""
        with open(path, "r") as f:
            data = yaml.safe_load(f)

        # Helper to get nested config with defaults
        def get_section(name: str) -> dict:
            return data.get(name, {})

        universe = get_section("universe")
        timeframes = get_section("timeframes")
        risk = get_section("risk")
        ks = get_section("kill_switch")
        stops = get_section("stops")
        regime = get_section("regime")
        llm = get_section("llm")
        execution = get_section("execution")
        strategies = get_section("strategies")

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
            primary_timeframe=timeframes.get("primary", "1h"),
            bars_to_fetch=timeframes.get("bars_to_fetch", 200),
            scan_interval_minutes=timeframes.get("scan_interval_minutes", 15),
            per_trade_pct=risk.get("per_trade_pct", 0.5),
            max_positions=risk.get("max_positions", 2),
            max_exposure_pct=risk.get("max_exposure_pct", 100),
            leverage=risk.get("leverage", 1),
            daily_loss_pct=ks.get("daily_loss_pct", 2.0),
            weekly_loss_pct=ks.get("weekly_loss_pct", 5.0),
            max_drawdown_pct=ks.get("max_drawdown_pct", 15.0),
            initial_atr_mult=stops.get("initial_atr_mult", 2.5),
            trailing_atr_mult=stops.get("trailing_atr_mult", 2.5),
            trend_adx_min=regime.get("trend_adx_min", 25.0),
            range_adx_max=regime.get("range_adx_max", 20.0),
            regime_confirmation_bars=regime.get("confirmation_bars", 2),
            llm_enabled=llm.get("enabled", True),
            llm_provider=llm.get("provider", "deepseek"),
            llm_max_calls=llm.get("max_calls_per_day", 6),
            llm_fallback=llm.get("fallback_on_error", "allow"),
            prefer_limit=execution.get("prefer_limit", True),
            max_slippage_pct=execution.get("max_slippage_pct", 0.1),
            trend_follow_enabled=strategies.get("trend_follow", {}).get("enabled", True),
            mean_reversion_enabled=strategies.get("mean_reversion", {}).get("enabled", False),
            testnet=env.lower() == "testnet",
            dry_run=data.get("dry_run", False),
        )


# =============================================================================
# Conservative Bot Orchestrator
# =============================================================================

class ConservativeBot:
    """
    Main orchestrator for conservative trading system.

    Architecture:
        MarketState → Strategy → LLMVeto → RiskManager → Execution

    Critical design principles:
    1. Trade only BTC/ETH (liquid, low-spread)
    2. One strategy at a time (trend follow primary)
    3. Risk-based sizing (0.5% per trade)
    4. Kill switch ALWAYS active
    5. LLM as filter, not decision maker
    """

    # Service startup order (dependencies)
    SERVICE_ORDER = [
        "kill_switch",     # MUST be first - safety critical
        "market_state",    # Data provider
        "llm_veto",        # Filter (optional)
        "risk_manager",    # Sizing
        "execution",       # Order placement
    ]

    def __init__(
        self,
        config_path: str = "simple_bot/config/trading.yaml",
        config: Optional[ConservativeConfig] = None,
    ) -> None:
        """
        Initialize ConservativeBot.

        Args:
            config_path: Path to trading.yaml
            config: Optional pre-loaded config
        """
        self.config_path = config_path
        self._config: Optional[ConservativeConfig] = config

        # Core components
        self._db: Optional[Database] = None
        self._bus: Optional[MessageBus] = None
        self._exchange: Optional[HyperliquidClient] = None

        # Services
        self._services: Dict[str, Any] = {}

        # Strategies
        self._strategies: List[Any] = []

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
        """Get configuration."""
        if self._config is None:
            raise RuntimeError("Configuration not loaded")
        return self._config

    @property
    def is_running(self) -> bool:
        """Check if bot is running."""
        return self._running

    @property
    def kill_switch(self) -> Optional[KillSwitchService]:
        """Get kill switch service."""
        return self._services.get("kill_switch")

    @property
    def market_state(self) -> Optional[MarketStateService]:
        """Get market state service."""
        return self._services.get("market_state")

    # =========================================================================
    # Initialization
    # =========================================================================

    def _load_config(self) -> ConservativeConfig:
        """Load configuration from YAML."""
        logger.info("Loading configuration from %s", self.config_path)
        self._config = ConservativeConfig.from_yaml(self.config_path)

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
        logger.info(
            "Exchange connected: Equity $%.2f",
            account.get("equity", 0),
        )

        return self._exchange

    async def _load_dynamic_assets(self) -> None:
        """
        Dynamically load all available assets from Hyperliquid.

        When universe_mode is "all", fetches all symbols and filters by:
        - Minimum 24h volume
        - Exclusion list (stablecoins, etc.)
        """
        cfg = self.config
        if cfg.universe_mode != "all":
            logger.info("Universe mode is 'manual', using configured assets: %s", cfg.assets)
            return

        if not self._exchange:
            raise RuntimeError("Exchange not initialized")

        logger.info("Loading dynamic asset universe...")

        try:
            # Get all available symbols from Hyperliquid
            markets = await self._exchange.get_all_markets()
            all_symbols = [market["name"] for market in markets]

            logger.info("Found %d total symbols on Hyperliquid", len(all_symbols))

            # Filter out excluded symbols
            filtered = [
                s for s in all_symbols
                if s not in cfg.exclude_symbols
            ]
            logger.info("After exclusion filter: %d symbols", len(filtered))

            # Filter by 24h volume if threshold > 0
            if cfg.min_volume_24h > 0:
                volume_filtered = []
                # Fetch spot meta for volume info (this is approximate)
                # For more accurate volume, we'd need to query each symbol
                # For now, include all non-excluded symbols
                volume_filtered = filtered
                logger.info(
                    "Volume filter: including all %d symbols (volume check disabled for speed)",
                    len(volume_filtered)
                )
                filtered = volume_filtered

            # Update config with dynamic assets
            # Note: dataclass is frozen=False by default, so we can modify
            object.__setattr__(cfg, "assets", filtered)

            logger.info(
                "Dynamic universe loaded: %d assets (excluded %d)",
                len(filtered),
                len(all_symbols) - len(filtered),
            )

            # Log first 10 and last 5 for visibility
            if len(filtered) > 15:
                logger.info("First 10: %s", filtered[:10])
                logger.info("Last 5: %s", filtered[-5:])
            else:
                logger.info("Assets: %s", filtered)

        except Exception as e:
            logger.error("Failed to load dynamic assets: %s", e)
            # Fallback to BTC/ETH
            fallback = ["BTC", "ETH"]
            object.__setattr__(cfg, "assets", fallback)
            logger.warning("Using fallback assets: %s", fallback)

    def _init_services(self) -> None:
        """Initialize all services with proper configuration."""
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
            bus=self._bus,
            db=self._db,
            config=ks_config,
        )

        # Market State Service
        ms_config = MarketStateConfig(
            assets=cfg.assets,
            timeframe=cfg.primary_timeframe,
            bars_to_fetch=cfg.bars_to_fetch,
            interval_seconds=cfg.scan_interval_minutes * 60,  # Convert minutes to seconds
            trend_adx_min=cfg.trend_adx_min,
            range_adx_max=cfg.range_adx_max,
            regime_confirmation_bars=cfg.regime_confirmation_bars,
        )
        self._services["market_state"] = create_market_state_service(
            bus=self._bus,
            db=self._db,
            config=ms_config,
            testnet=cfg.testnet,
        )

        # LLM Veto Service
        llm_config = LLMVetoConfig(
            enabled=cfg.llm_enabled,
            provider=cfg.llm_provider,
            max_calls_per_day=cfg.llm_max_calls,
            fallback_on_error=cfg.llm_fallback,
        )
        self._services["llm_veto"] = create_llm_veto(
            bus=self._bus,
            db=self._db,
            config=llm_config,
        )

        # Risk Manager Service
        risk_config = RiskConfig(
            per_trade_pct=cfg.per_trade_pct,
            max_positions=cfg.max_positions,
            max_exposure_pct=cfg.max_exposure_pct,
            leverage=cfg.leverage,
            trailing_atr_mult=cfg.trailing_atr_mult,
            max_slippage_pct=cfg.max_slippage_pct,
        )
        self._services["risk_manager"] = create_risk_manager(
            bus=self._bus,
            db=self._db,
            config=risk_config,
        )

        logger.info(
            "Initialized %d services: %s",
            len(self._services),
            ", ".join(self._services.keys()),
        )

    def _init_strategies(self) -> None:
        """Initialize trading strategies."""
        cfg = self.config

        # Trend Following (primary)
        if cfg.trend_follow_enabled:
            trend_config = {
                "breakout_period": 20,
                "price_above_ema200": True,
                "atr_filter": True,
                "stop_atr_mult": cfg.initial_atr_mult,
                "min_adx": cfg.trend_adx_min,
            }
            self._strategies.append(TrendFollowStrategy(config=trend_config))
            logger.info("Initialized TrendFollowStrategy")

        # Mean Reversion (optional, disabled by default)
        if cfg.mean_reversion_enabled:
            mr_config = {
                "bb_period": 20,
                "bb_std": 2.0,
                "rsi_oversold": 30,
                "rsi_overbought": 70,
                "exit_at_mid": True,
            }
            self._strategies.append(MeanReversionStrategy(config=mr_config))
            logger.info("Initialized MeanReversionStrategy")

        logger.info("Initialized %d strategies", len(self._strategies))

    # =========================================================================
    # Strategy Loop
    # =========================================================================

    async def _strategy_loop(self) -> None:
        """
        Main strategy evaluation loop.

        Runs every 4 hours (on new candle close):
        1. Get market state for each asset
        2. Check kill switch status
        3. Evaluate strategies for setups
        4. Pass setups through LLM veto
        5. Send approved setups to risk manager
        """
        logger.info("Strategy loop started")

        # Initial delay to let services initialize
        await asyncio.sleep(10)

        # Calculate loop interval from timeframe
        tf_seconds = TIMEFRAME_SECONDS.get(self.config.primary_timeframe, 3600)

        while self._running and not self._shutdown_event.is_set():
            try:
                await self._evaluate_all_assets()

                # Wait for next evaluation based on timeframe
                logger.info("Next scan in %d minutes", tf_seconds // 60)
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=tf_seconds,
                    )
                    break  # Shutdown requested
                except asyncio.TimeoutError:
                    pass  # Normal timeout, continue

            except asyncio.CancelledError:
                logger.debug("Strategy loop cancelled")
                break
            except Exception as e:
                logger.error("Strategy loop error: %s", e, exc_info=True)
                await asyncio.sleep(60)  # Wait before retry

    async def _evaluate_all_assets(self) -> None:
        """Evaluate all assets for trading opportunities."""
        # Check kill switch first
        kill_switch = self._services.get("kill_switch")
        if kill_switch and not kill_switch.is_trading_allowed():
            logger.warning(
                "Trading paused by kill switch: %s",
                kill_switch.get_status().value,
            )
            return

        # Get market states
        market_state_svc = self._services.get("market_state")
        if not market_state_svc:
            return

        states = market_state_svc.get_all_states()

        if not states:
            logger.warning("No market states available")
            return

        for symbol, state in states.items():
            await self._evaluate_asset(state)

    async def _evaluate_asset(self, state: MarketState) -> None:
        """
        Evaluate a single asset for trading opportunities.

        Args:
            state: Current market state for the asset
        """
        logger.debug(
            "Evaluating %s: regime=%s, ADX=%.1f",
            state.symbol,
            state.regime.value,
            float(state.adx),
        )

        # Try each strategy
        for strategy in self._strategies:
            # Check if strategy can trade in current regime
            if not strategy.can_trade(state):
                continue

            # Evaluate strategy
            result = strategy.evaluate(state)

            if not result.has_setup or not result.setup:
                continue

            setup = result.setup

            logger.info(
                "Setup found: %s %s @ %.2f (%s)",
                setup.direction.value.upper(),
                setup.symbol,
                float(setup.entry_price),
                strategy.name,
            )

            # Pass through LLM veto
            llm_service = self._services.get("llm_veto")
            if llm_service:
                approved, decision = await llm_service.evaluate_setup(setup)

                if not approved:
                    logger.info(
                        "Setup DENIED by LLM: %s - %s",
                        setup.symbol,
                        decision.reason[:50],
                    )
                    continue

                logger.info(
                    "Setup APPROVED by LLM: %s (confidence: %.0f%%)",
                    setup.symbol,
                    float(decision.confidence) * 100,
                )

                # Mark as approved
                setup.llm_approved = True

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
                    self._shutdown_event.wait(),
                    timeout=30,  # Check every 30 seconds
                )
                break
            except asyncio.TimeoutError:
                pass

            try:
                await self._check_health()
            except Exception as e:
                logger.error("Health check error: %s", e)

    async def _check_health(self) -> None:
        """Check health of all services and sync account data to dashboard."""
        # Check service health
        for name, service in self._services.items():
            try:
                health = await service.health_check()

                if not health.healthy:
                    logger.warning("Service unhealthy: %s - %s", name, health.message)

                # Update service health in database for dashboard
                if self._db:
                    try:
                        await self._db.update_service_health(
                            service_name=name,
                            status="healthy" if health.healthy else "unhealthy",
                            message=health.message or "OK",
                        )
                    except Exception as e:
                        logger.debug("Could not update service health in DB: %s", e)

            except Exception as e:
                logger.error("Health check failed for %s: %s", name, e)

        # Sync account data to database for dashboard
        if self._exchange and self._db:
            try:
                account = await self._exchange.get_account_state()
                await self._db.update_account(
                    equity=Decimal(str(account.get("equity", 0))),
                    available_balance=Decimal(str(account.get("availableBalance", 0))),
                    margin_used=Decimal(str(account.get("marginUsed", 0))),
                    unrealized_pnl=Decimal(str(account.get("unrealizedPnl", 0))),
                )
                logger.debug("Account synced to DB: equity=$%.2f", account.get("equity", 0))
            except Exception as e:
                logger.debug("Could not sync account to DB: %s", e)

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def _start_services(self) -> None:
        """Start all services in dependency order."""
        logger.info("Starting services...")

        for name in self.SERVICE_ORDER:
            if name in self._services:
                service = self._services[name]
                await service.start()
                logger.info("Started: %s", name)

        logger.info("All services started")

    async def _stop_services(self) -> None:
        """Stop all services in reverse order."""
        logger.info("Stopping services...")

        for name in reversed(self.SERVICE_ORDER):
            if name in self._services:
                service = self._services[name]
                try:
                    await service.stop()
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
        logger.info("HLQuantBot Conservative System Starting")
        logger.info("=" * 60)

        try:
            # Load config
            if self._config is None:
                self._load_config()

            # Setup logging
            setup_logging(log_level="INFO")

            # Initialize components
            await self._init_database()
            await self._init_message_bus()
            await self._init_exchange()

            # Load dynamic assets if mode="all"
            await self._load_dynamic_assets()

            # Initialize services and strategies
            self._init_services()
            self._init_strategies()

            # Start services
            await self._start_services()

            # Start background tasks
            self._strategy_task = asyncio.create_task(
                self._strategy_loop(),
                name="strategy_loop",
            )
            self._health_task = asyncio.create_task(
                self._health_loop(),
                name="health_loop",
            )

            self._running = True
            self._start_time = datetime.now(timezone.utc)
            self._shutdown_event.clear()

            # Update equity in kill switch
            if self._exchange:
                account = await self._exchange.get_account_state()
                equity = account.get("equity", 0)

                if self.kill_switch:
                    await self.kill_switch.update_equity(
                        __import__("decimal").Decimal(str(equity))
                    )

            logger.info("=" * 60)
            logger.info(
                "HLQuantBot Conservative Running (%s)",
                "TESTNET" if self.config.testnet else "MAINNET",
            )
            logger.info("Assets: %s", ", ".join(self.config.assets))
            logger.info("Risk per trade: %.1f%%", self.config.per_trade_pct)
            logger.info("Max drawdown: %.1f%%", self.config.max_drawdown_pct)
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
        logger.info("HLQuantBot Conservative Stopping")
        logger.info("=" * 60)

        self._running = False
        self._shutdown_event.set()

        # Cancel background tasks
        for task in [self._strategy_task, self._health_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Stop services
        await self._stop_services()

        # Stop message bus
        if self._bus:
            await self._bus.stop()

        # Disconnect exchange
        if self._exchange:
            await self._exchange.disconnect()

        # Disconnect database
        if self._db:
            await self._db.disconnect()

        logger.info("=" * 60)
        logger.info("HLQuantBot Conservative Stopped")
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
                pass  # Windows

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
                service_status[name] = {
                    "healthy": False,
                    "message": str(e),
                }

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
            "strategies": [s.name for s in self._strategies],
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

    parser = argparse.ArgumentParser(
        description="HLQuantBot Conservative Trading System"
    )
    parser.add_argument(
        "-c", "--config",
        default="simple_bot/config/trading.yaml",
        help="Path to trading.yaml configuration file",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    asyncio.run(main(config_path=args.config))
