#!/usr/bin/env python3
"""
HLQuantBot v2.0 - Main Orchestrator
====================================

Central orchestrator that:
1. Loads configuration
2. Initializes all components (database, message bus, LLM, exchange)
3. Starts services in dependency order
4. Monitors health and restarts failed services
5. Handles graceful shutdown on SIGINT/SIGTERM

Usage:
    # Run as module
    python -m simple_bot.main
    
    # Or import and run
    from simple_bot.main import HLQuantBot
    import asyncio
    
    bot = HLQuantBot()
    asyncio.run(bot.run())

Author: Francesco Carlesi
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from database.db import Database

from .config.loader import Config, load_config
from .llm.client import DeepSeekClient, create_deepseek_client
from .api.hyperliquid import HyperliquidClient
from .services import (
    BaseService,
    MessageBus,
    Topic,
    ServiceStatus,
    HealthStatus,
    MarketScannerService,
    create_market_scanner,
    OpportunityRankerService,
    CapitalAllocatorService,
    create_capital_allocator,
    ExecutionEngineService,
    create_execution_engine,
    StrategySelectorService,
    create_strategy_selector,
    LearningModuleService,
    create_learning_module,
)


# =============================================================================
# Logging Setup
# =============================================================================

def setup_logging(config: Config) -> logging.Logger:
    """
    Configure logging based on configuration.
    
    Args:
        config: Application configuration
        
    Returns:
        Root logger instance
    """
    log_level = getattr(logging, config.system.log_level, logging.INFO)
    
    # Create formatter
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    
    # File handler
    log_file = Path(config.system.log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
    
    # Reduce noise from third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("asyncpg").setLevel(logging.WARNING)
    
    return logging.getLogger("hlquantbot")


logger = logging.getLogger("hlquantbot.main")


# =============================================================================
# HLQuantBot Orchestrator
# =============================================================================

class HLQuantBot:
    """
    Main orchestrator for HLQuantBot v2.0.
    
    Manages the complete lifecycle of all services:
    - Configuration loading and validation
    - Component initialization (database, message bus, LLM, exchange)
    - Service startup in dependency order
    - Health monitoring with automatic restart
    - Graceful shutdown with cleanup
    
    Example:
        bot = HLQuantBot()
        await bot.run()  # Runs until SIGINT/SIGTERM
        
        # Or with custom config
        bot = HLQuantBot(config_path="custom_config.yaml")
        await bot.run()
    """
    
    # Service start order (dependencies)
    SERVICE_ORDER = [
        "market_scanner",
        "opportunity_ranker",
        "strategy_selector",
        "capital_allocator",
        "execution_engine",
        "learning_module",
    ]
    
    def __init__(
        self,
        config_path: str = "simple_bot/config/intelligent_bot.yaml",
        config: Optional[Config] = None,
    ) -> None:
        """
        Initialize HLQuantBot.
        
        Args:
            config_path: Path to YAML configuration file
            config: Optional pre-loaded Config object (overrides config_path)
        """
        self.config_path = config_path
        self._config: Optional[Config] = config
        
        # Core components
        self._db: Optional[Database] = None
        self._bus: Optional[MessageBus] = None
        self._llm: Optional[DeepSeekClient] = None
        self._exchange: Optional[HyperliquidClient] = None
        
        # Services
        self._services: Dict[str, BaseService] = {}
        
        # State
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._start_time: Optional[datetime] = None
        self._health_task: Optional[asyncio.Task] = None
        
        # Health monitoring
        self._health_check_interval = 30.0  # seconds
        self._restart_attempts: Dict[str, int] = {}
        self._max_restart_attempts = 3
    
    # =========================================================================
    # Properties
    # =========================================================================
    
    @property
    def config(self) -> Config:
        """Get current configuration."""
        if self._config is None:
            raise RuntimeError("Configuration not loaded")
        return self._config
    
    @property
    def is_running(self) -> bool:
        """Check if bot is running."""
        return self._running
    
    @property
    def uptime_seconds(self) -> float:
        """Get bot uptime in seconds."""
        if self._start_time is None:
            return 0.0
        return (datetime.utcnow() - self._start_time).total_seconds()
    
    @property
    def services(self) -> Dict[str, BaseService]:
        """Get all services."""
        return self._services.copy()
    
    # =========================================================================
    # Initialization
    # =========================================================================
    
    def _load_config(self) -> Config:
        """Load configuration from file."""
        logger.info("Loading configuration from %s", self.config_path)
        self._config = load_config(self.config_path)
        return self._config
    
    async def _init_database(self) -> Database:
        """Initialize database connection."""
        logger.info("Connecting to database...")
        
        db_config = self.config.database
        dsn = db_config.dsn
        
        self._db = Database(dsn)
        await self._db.connect(
            min_size=db_config.pool_min,
            max_size=db_config.pool_max,
        )
        
        # Verify connection
        if not await self._db.health_check():
            raise RuntimeError("Database health check failed")
        
        logger.info("Database connected successfully")
        return self._db
    
    async def _init_message_bus(self) -> MessageBus:
        """Initialize message bus."""
        logger.info("Starting message bus...")
        
        self._bus = MessageBus()
        await self._bus.start()
        
        logger.info("Message bus started with %d topics", len(Topic))
        return self._bus
    
    def _init_llm(self) -> DeepSeekClient:
        """Initialize LLM client."""
        logger.info("Initializing LLM client...")
        
        llm_config = self.config.llm
        self._llm = create_deepseek_client(llm_config)
        
        if self._llm.is_available:
            logger.info(
                "LLM client ready: %s, %d requests/day remaining",
                llm_config.model,
                self._llm.remaining_requests,
            )
        else:
            logger.warning(
                "LLM client not available (no API key?). "
                "Strategy selector will use fallback rules."
            )
        
        return self._llm
    
    async def _init_exchange(self) -> HyperliquidClient:
        """Initialize exchange client."""
        logger.info("Connecting to Hyperliquid...")
        
        hl_config = self.config.hyperliquid
        self._exchange = HyperliquidClient(testnet=hl_config.testnet)
        await self._exchange.connect()
        
        # Get initial account state
        account = await self._exchange.get_account_state()
        logger.info(
            "Exchange connected: %s, Equity: $%.2f",
            "TESTNET" if hl_config.testnet else "MAINNET",
            account["equity"],
        )
        
        return self._exchange
    
    async def _init_services(self) -> None:
        """Initialize all services."""
        logger.info("Initializing services...")
        
        services_config = self.config.services
        
        # Market Scanner
        if services_config.market_scanner.enabled:
            self._services["market_scanner"] = create_market_scanner(
                bus=self._bus,
                db=self._db,
                config=services_config.market_scanner,
                testnet=self.config.hyperliquid.testnet,
            )
        
        # Opportunity Ranker
        if services_config.opportunity_ranker.enabled:
            self._services["opportunity_ranker"] = OpportunityRankerService(
                bus=self._bus,
                db=self._db,
                config=services_config.opportunity_ranker,
            )
        
        # Strategy Selector
        if services_config.strategy_selector.enabled:
            self._services["strategy_selector"] = create_strategy_selector(
                bus=self._bus,
                db=self._db,
                config=services_config.strategy_selector,
            )
        
        # Capital Allocator
        if services_config.capital_allocator.enabled:
            self._services["capital_allocator"] = create_capital_allocator(
                bus=self._bus,
                db=self._db,
                client=self._exchange,
            )
        
        # Execution Engine
        if services_config.execution_engine.enabled:
            self._services["execution_engine"] = create_execution_engine(
                bus=self._bus,
                config=self.config,
                client=self._exchange,
                db=self._db,
            )
        
        # Learning Module
        if services_config.learning_module.enabled:
            self._services["learning_module"] = create_learning_module(
                bus=self._bus,
                db=self._db,
                llm=self._llm,
                config=self.config.model_dump() if hasattr(self.config, 'model_dump') else {},
            )
        
        logger.info(
            "Initialized %d services: %s",
            len(self._services),
            ", ".join(self._services.keys()),
        )
    
    # =========================================================================
    # Service Management
    # =========================================================================
    
    async def _start_services(self) -> None:
        """Start all services in dependency order."""
        logger.info("Starting services...")
        
        for service_name in self.SERVICE_ORDER:
            if service_name in self._services:
                service = self._services[service_name]
                try:
                    await service.start()
                    logger.info("Started: %s", service_name)
                    self._restart_attempts[service_name] = 0
                except Exception as e:
                    logger.error("Failed to start %s: %s", service_name, e)
                    raise
        
        logger.info("All services started")
    
    async def _stop_services(self) -> None:
        """Stop all services in reverse order."""
        logger.info("Stopping services...")
        
        for service_name in reversed(self.SERVICE_ORDER):
            if service_name in self._services:
                service = self._services[service_name]
                try:
                    await service.stop()
                    logger.info("Stopped: %s", service_name)
                except Exception as e:
                    logger.error("Error stopping %s: %s", service_name, e)
        
        logger.info("All services stopped")
    
    async def _restart_service(self, service_name: str) -> bool:
        """
        Attempt to restart a failed service.
        
        Args:
            service_name: Name of service to restart
            
        Returns:
            True if restart succeeded
        """
        if service_name not in self._services:
            return False
        
        attempts = self._restart_attempts.get(service_name, 0)
        if attempts >= self._max_restart_attempts:
            logger.error(
                "Max restart attempts (%d) exceeded for %s",
                self._max_restart_attempts,
                service_name,
            )
            return False
        
        self._restart_attempts[service_name] = attempts + 1
        service = self._services[service_name]
        
        try:
            logger.warning(
                "Restarting %s (attempt %d/%d)",
                service_name,
                attempts + 1,
                self._max_restart_attempts,
            )
            
            await service.restart(delay=2.0)
            
            logger.info("Successfully restarted %s", service_name)
            self._restart_attempts[service_name] = 0
            return True
            
        except Exception as e:
            logger.error("Failed to restart %s: %s", service_name, e)
            return False
    
    # =========================================================================
    # Health Monitoring
    # =========================================================================
    
    async def _health_check_loop(self) -> None:
        """Periodic health check and service restart loop."""
        logger.info(
            "Health monitor started (interval: %.0fs)",
            self._health_check_interval,
        )
        
        while self._running and not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self._health_check_interval,
                )
                break  # Shutdown requested
            except asyncio.TimeoutError:
                pass  # Normal timeout, continue with health check
            
            try:
                await self._check_services_health()
            except Exception as e:
                logger.error("Health check error: %s", e)
    
    async def _check_services_health(self) -> None:
        """Check health of all services, persist to DB, and restart if needed."""
        for service_name, service in self._services.items():
            try:
                health = await service.health_check()

                # Persist health status to database for dashboard visibility
                if self._db:
                    status = "healthy" if health.healthy else "unhealthy"
                    if service.status == ServiceStatus.ERROR:
                        status = "error"
                    elif service.status == ServiceStatus.STOPPED:
                        status = "stopped"

                    await self._db.update_service_health(
                        service_name=service_name,
                        status=status,
                        metadata={"message": health.message} if health.message else None,
                    )

                if not health.healthy:
                    logger.warning(
                        "Service unhealthy: %s - %s",
                        service_name,
                        health.message,
                    )

                    # Attempt restart if in error state
                    if service.status == ServiceStatus.ERROR:
                        await self._restart_service(service_name)

            except Exception as e:
                logger.error(
                    "Failed to check health of %s: %s",
                    service_name,
                    e,
                )
                # Still try to update DB with error status
                if self._db:
                    try:
                        await self._db.update_service_health(
                            service_name=service_name,
                            status="error",
                            metadata={"error": str(e)},
                            increment_errors=1,
                        )
                    except Exception:
                        pass  # Don't fail the health loop on DB errors
    
    async def get_status(self) -> Dict[str, Any]:
        """
        Get comprehensive bot status.
        
        Returns:
            Dict with bot and service status
        """
        service_status = {}
        for name, service in self._services.items():
            try:
                health = await service.health_check()
                service_status[name] = health.to_dict()
            except Exception as e:
                service_status[name] = {
                    "healthy": False,
                    "status": "error",
                    "message": str(e),
                }
        
        return {
            "running": self._running,
            "uptime_seconds": self.uptime_seconds,
            "start_time": (
                self._start_time.isoformat() if self._start_time else None
            ),
            "config": {
                "mode": self.config.system.mode,
                "testnet": self.config.hyperliquid.testnet,
            },
            "services": service_status,
            "message_bus": (
                self._bus.get_statistics() if self._bus else None
            ),
            "llm": self._llm.stats if self._llm else None,
        }
    
    # =========================================================================
    # Lifecycle
    # =========================================================================
    
    async def start(self) -> None:
        """
        Initialize and start the bot.
        
        Loads configuration, initializes all components,
        and starts services in dependency order.
        """
        if self._running:
            logger.warning("Bot already running")
            return
        
        logger.info("=" * 60)
        logger.info("HLQuantBot v2.0 Starting")
        logger.info("=" * 60)
        
        try:
            # Load configuration
            if self._config is None:
                self._load_config()
            
            # Setup logging
            setup_logging(self.config)
            
            # Initialize components
            await self._init_database()
            await self._init_message_bus()
            self._init_llm()
            await self._init_exchange()
            
            # Initialize and start services
            await self._init_services()
            await self._start_services()
            
            # Start health monitoring
            self._health_task = asyncio.create_task(
                self._health_check_loop(),
                name="health_monitor",
            )
            
            self._running = True
            self._start_time = datetime.utcnow()
            self._shutdown_event.clear()
            
            logger.info("=" * 60)
            logger.info(
                "HLQuantBot v2.0 Running (%s)",
                "TESTNET" if self.config.hyperliquid.testnet else "MAINNET",
            )
            logger.info("=" * 60)
            
        except Exception as e:
            logger.critical("Failed to start bot: %s", e, exc_info=True)
            await self.stop()
            raise
    
    async def stop(self) -> None:
        """
        Stop the bot gracefully.
        
        Stops services in reverse order, closes connections,
        and cleans up resources.
        """
        if not self._running:
            return
        
        logger.info("=" * 60)
        logger.info("HLQuantBot v2.0 Stopping")
        logger.info("=" * 60)
        
        self._running = False
        self._shutdown_event.set()
        
        # Cancel health monitor
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
        
        # Stop services
        await self._stop_services()
        
        # Stop message bus
        if self._bus:
            await self._bus.stop()
            logger.info("Message bus stopped")
        
        # Disconnect exchange
        if self._exchange:
            await self._exchange.disconnect()
            logger.info("Exchange disconnected")
        
        # Close LLM client
        if self._llm:
            await self._llm.close()
            logger.info("LLM client closed")
        
        # Disconnect database
        if self._db:
            await self._db.disconnect()
            logger.info("Database disconnected")
        
        logger.info("=" * 60)
        logger.info("HLQuantBot v2.0 Stopped")
        logger.info("=" * 60)
    
    async def run(self) -> None:
        """
        Run the bot until shutdown signal.
        
        Starts the bot and waits for SIGINT/SIGTERM.
        """
        # Register signal handlers
        loop = asyncio.get_event_loop()
        
        def signal_handler():
            logger.info("Shutdown signal received")
            asyncio.create_task(self.stop())
        
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, signal_handler)
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                pass
        
        try:
            await self.start()
            
            # Wait until shutdown
            while self._running:
                await asyncio.sleep(1)
                
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
        finally:
            await self.stop()


# =============================================================================
# Main Entry Point
# =============================================================================

async def main(config_path: str = "simple_bot/config/intelligent_bot.yaml") -> None:
    """
    Main entry point for running HLQuantBot.
    
    Args:
        config_path: Path to configuration file
    """
    bot = HLQuantBot(config_path=config_path)
    await bot.run()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(
        description="HLQuantBot v2.0 - Intelligent Trading Bot for Hyperliquid"
    )
    parser.add_argument(
        "-c", "--config",
        default="simple_bot/config/intelligent_bot.yaml",
        help="Path to configuration file",
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
