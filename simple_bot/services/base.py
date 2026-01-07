"""
HLQuantBot Base Service
=======================

Abstract base class for all microservices in HLQuantBot.

Features:
- Lifecycle management: start(), stop(), restart()
- Health checking with customizable logic
- Integrated logging with service context
- Message bus integration for pub/sub
- Database connection management
- Configuration loading and hot-reload support
- Error handling with exponential backoff retry
- Graceful shutdown with cleanup hooks
- Status tracking (running, stopped, error, starting, stopping)

Usage:
    class MyService(BaseService):
        async def _on_start(self) -> None:
            # Initialize resources
            await self.bus.subscribe(Topic.MARKET_DATA, self.handle_data)
        
        async def _on_stop(self) -> None:
            # Cleanup resources
            pass
        
        async def _run_iteration(self) -> None:
            # Main service loop iteration
            await asyncio.sleep(1)
        
        async def _health_check_impl(self) -> bool:
            # Custom health check
            return True
    
    service = MyService(name="my_service", bus=bus, db=db)
    await service.start()

Author: Francesco Carlesi
"""

import asyncio
import logging
import time
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, Optional, TypeVar

import yaml

# Local imports
from .message_bus import MessageBus, Topic

# Try to import Database, make it optional
try:
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from database.db import Database
    DB_AVAILABLE = True
except ImportError:
    Database = None  # type: ignore
    DB_AVAILABLE = False


logger = logging.getLogger(__name__)


# =============================================================================
# Service Status
# =============================================================================

class ServiceStatus(str, Enum):
    """Current status of a service."""
    
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"
    
    def __str__(self) -> str:
        return self.value


# =============================================================================
# Service Health
# =============================================================================

@dataclass
class HealthStatus:
    """Health check result for a service."""
    
    healthy: bool
    status: ServiceStatus
    last_check: datetime = field(default_factory=datetime.utcnow)
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "healthy": self.healthy,
            "status": str(self.status),
            "last_check": self.last_check.isoformat(),
            "message": self.message,
            "details": self.details,
        }


# =============================================================================
# Retry Configuration
# =============================================================================

@dataclass
class RetryConfig:
    """Configuration for exponential backoff retry."""
    
    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0
    exponential_base: float = 2.0
    
    def get_delay(self, attempt: int) -> float:
        """Calculate delay for given attempt number (0-indexed)."""
        delay = self.base_delay_seconds * (self.exponential_base ** attempt)
        return min(delay, self.max_delay_seconds)


# =============================================================================
# Base Service
# =============================================================================

class BaseService(ABC):
    """
    Abstract base class for HLQuantBot microservices.
    
    Provides common functionality:
    - Lifecycle management (start/stop/restart)
    - Health checking
    - Logging with service context
    - Message bus integration
    - Database access
    - Configuration management
    - Error handling with retry
    
    Subclasses must implement:
    - _on_start(): Called when service starts
    - _on_stop(): Called when service stops
    - _run_iteration(): Main loop iteration (optional)
    - _health_check_impl(): Custom health check (optional)
    """
    
    def __init__(
        self,
        name: str,
        bus: Optional[MessageBus] = None,
        db: Optional["Database"] = None,
        config: Optional[Dict[str, Any]] = None,
        config_path: Optional[str] = None,
        retry_config: Optional[RetryConfig] = None,
        loop_interval_seconds: float = 1.0,
    ) -> None:
        """
        Initialize base service.
        
        Args:
            name: Unique service name for logging and identification
            bus: MessageBus instance for pub/sub communication
            db: Database instance for persistence
            config: Configuration dictionary
            config_path: Path to YAML config file (alternative to config dict)
            retry_config: Configuration for retry behavior
            loop_interval_seconds: Delay between _run_iteration calls
        """
        self.name = name
        self.bus = bus
        self.db = db
        self.retry_config = retry_config or RetryConfig()
        self.loop_interval_seconds = loop_interval_seconds
        
        # Status tracking
        self._status = ServiceStatus.STOPPED
        self._error_message: Optional[str] = None
        self._start_time: Optional[datetime] = None
        self._stop_time: Optional[datetime] = None
        self._iteration_count: int = 0
        self._error_count: int = 0
        self._last_health_check: Optional[HealthStatus] = None
        
        # Task management
        self._main_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
        
        # Load config
        self._config: Dict[str, Any] = config or {}
        self._config_path = config_path
        if config_path and not config:
            self._load_config()
        
        # Service-specific logger
        self._logger = logging.getLogger(f"hlquantbot.{name}")
        
        self._logger.info("Service initialized: %s", name)
    
    # =========================================================================
    # Configuration
    # =========================================================================
    
    def _load_config(self) -> None:
        """Load configuration from YAML file."""
        if not self._config_path:
            return
        
        try:
            path = Path(self._config_path)
            if path.exists():
                with open(path, "r") as f:
                    self._config = yaml.safe_load(f) or {}
                self._logger.info("Loaded config from %s", self._config_path)
            else:
                self._logger.warning("Config file not found: %s", self._config_path)
        except Exception as e:
            self._logger.error("Failed to load config: %s", e)
    
    def reload_config(self) -> bool:
        """
        Reload configuration from file.
        
        Returns:
            True if reload succeeded
        """
        try:
            self._load_config()
            self._on_config_reload()
            self._logger.info("Configuration reloaded")
            return True
        except Exception as e:
            self._logger.error("Config reload failed: %s", e)
            return False
    
    def get_config(self, key: str, default: Any = None) -> Any:
        """
        Get configuration value by key.
        
        Supports dot-notation for nested keys: "database.host"
        """
        keys = key.split(".")
        value = self._config
        
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
            
            if value is None:
                return default
        
        return value
    
    def _on_config_reload(self) -> None:
        """Hook called after config reload. Override in subclasses."""
        pass
    
    @property
    def config(self) -> Dict[str, Any]:
        """Get full configuration dictionary."""
        return self._config.copy()
    
    # =========================================================================
    # Lifecycle Management
    # =========================================================================
    
    async def start(self) -> None:
        """
        Start the service.
        
        Calls _on_start() and begins the main loop if _run_iteration is implemented.
        """
        if self._status in (ServiceStatus.RUNNING, ServiceStatus.STARTING):
            self._logger.warning("Service already running or starting")
            return
        
        self._logger.info("Starting service: %s", self.name)
        self._status = ServiceStatus.STARTING
        self._error_message = None
        self._shutdown_event.clear()
        
        try:
            # Custom startup logic
            await self._on_start()
            
            # Start main loop in background
            self._main_task = asyncio.create_task(
                self._main_loop(),
                name=f"{self.name}_main_loop"
            )
            
            self._status = ServiceStatus.RUNNING
            self._start_time = datetime.now(timezone.utc)
            self._logger.info("Service started: %s", self.name)
            
        except Exception as e:
            self._status = ServiceStatus.ERROR
            self._error_message = str(e)
            self._logger.error(
                "Failed to start service: %s - %s",
                self.name,
                e,
                exc_info=True
            )
            raise
    
    async def stop(self, timeout: float = 10.0) -> None:
        """
        Stop the service gracefully.
        
        Args:
            timeout: Max seconds to wait for cleanup
        """
        if self._status == ServiceStatus.STOPPED:
            return
        
        self._logger.info("Stopping service: %s", self.name)
        self._status = ServiceStatus.STOPPING
        
        # Signal shutdown
        self._shutdown_event.set()
        
        # Wait for main loop to finish
        if self._main_task and not self._main_task.done():
            try:
                await asyncio.wait_for(self._main_task, timeout=timeout)
            except asyncio.TimeoutError:
                self._logger.warning("Main loop did not stop in time, cancelling")
                self._main_task.cancel()
                try:
                    await self._main_task
                except asyncio.CancelledError:
                    pass
        
        # Custom cleanup
        try:
            await self._on_stop()
        except Exception as e:
            self._logger.error("Error during stop: %s", e)
        
        self._status = ServiceStatus.STOPPED
        self._stop_time = datetime.now(timezone.utc)
        self._main_task = None
        self._logger.info("Service stopped: %s", self.name)
    
    async def restart(self, delay: float = 1.0) -> None:
        """
        Restart the service.
        
        Args:
            delay: Seconds to wait between stop and start
        """
        self._logger.info("Restarting service: %s", self.name)
        await self.stop()
        await asyncio.sleep(delay)
        await self.start()
    
    # =========================================================================
    # Main Loop
    # =========================================================================
    
    async def _main_loop(self) -> None:
        """Main service loop with error handling and retry."""
        consecutive_errors = 0
        
        while not self._shutdown_event.is_set():
            try:
                await self._run_iteration()
                self._iteration_count += 1
                consecutive_errors = 0
                
                # Wait for next iteration or shutdown
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=self.loop_interval_seconds
                    )
                    break  # Shutdown requested
                except asyncio.TimeoutError:
                    continue  # Normal iteration
                    
            except asyncio.CancelledError:
                self._logger.debug("Main loop cancelled")
                break
                
            except Exception as e:
                self._error_count += 1
                consecutive_errors += 1
                
                self._logger.error(
                    "Error in main loop (attempt %d/%d): %s",
                    consecutive_errors,
                    self.retry_config.max_attempts,
                    e,
                    exc_info=True
                )
                
                if consecutive_errors >= self.retry_config.max_attempts:
                    self._status = ServiceStatus.ERROR
                    self._error_message = str(e)
                    self._logger.critical(
                        "Max retries exceeded, service entering error state"
                    )
                    break
                
                # Exponential backoff
                delay = self.retry_config.get_delay(consecutive_errors - 1)
                self._logger.info("Retrying in %.1f seconds", delay)
                await asyncio.sleep(delay)
    
    # =========================================================================
    # Health Check
    # =========================================================================
    
    async def health_check(self) -> HealthStatus:
        """
        Perform health check.
        
        Returns:
            HealthStatus with check results
        """
        try:
            # Base checks
            healthy = self._status == ServiceStatus.RUNNING
            details: Dict[str, Any] = {
                "uptime_seconds": self._get_uptime_seconds(),
                "iteration_count": self._iteration_count,
                "error_count": self._error_count,
            }
            
            # Check database if available
            if self.db:
                db_healthy = await self.db.health_check()
                details["database"] = db_healthy
                healthy = healthy and db_healthy
            
            # Check message bus if available
            if self.bus:
                details["message_bus"] = self.bus.is_running
                healthy = healthy and self.bus.is_running
            
            # Custom health check
            custom_healthy = await self._health_check_impl()
            healthy = healthy and custom_healthy
            
            message = "healthy" if healthy else "unhealthy"
            if self._error_message:
                message = self._error_message
            
            self._last_health_check = HealthStatus(
                healthy=healthy,
                status=self._status,
                message=message,
                details=details,
            )
            
        except Exception as e:
            self._logger.error("Health check failed: %s", e)
            self._last_health_check = HealthStatus(
                healthy=False,
                status=self._status,
                message=f"Health check error: {e}",
            )
        
        return self._last_health_check
    
    def _get_uptime_seconds(self) -> float:
        """Calculate service uptime in seconds."""
        if not self._start_time:
            return 0.0
        
        end_time = self._stop_time or datetime.now(timezone.utc)
        return (end_time - self._start_time).total_seconds()
    
    # =========================================================================
    # Abstract Methods
    # =========================================================================
    
    @abstractmethod
    async def _on_start(self) -> None:
        """
        Called when service starts.
        
        Override to initialize resources, subscribe to topics, etc.
        """
        pass
    
    @abstractmethod
    async def _on_stop(self) -> None:
        """
        Called when service stops.
        
        Override to cleanup resources, unsubscribe from topics, etc.
        """
        pass
    
    async def _run_iteration(self) -> None:
        """
        Main loop iteration.
        
        Override to implement periodic work. Called every loop_interval_seconds.
        Default implementation does nothing (event-driven services).
        """
        pass
    
    async def _health_check_impl(self) -> bool:
        """
        Custom health check implementation.
        
        Override to add service-specific health checks.
        
        Returns:
            True if service-specific checks pass
        """
        return True
    
    # =========================================================================
    # Utility Methods
    # =========================================================================
    
    async def publish(
        self,
        topic: Topic,
        payload: Any,
    ) -> None:
        """
        Publish a message to the bus.
        
        Convenience wrapper that sets source to service name.
        """
        if not self.bus:
            self._logger.warning("Cannot publish: no message bus configured")
            return
        
        await self.bus.publish(topic, payload, source=self.name)
    
    async def subscribe(
        self,
        topic: Topic,
        handler: Callable[..., Coroutine],
    ) -> None:
        """
        Subscribe to a topic.
        
        Convenience wrapper for bus.subscribe().
        """
        if not self.bus:
            self._logger.warning("Cannot subscribe: no message bus configured")
            return
        
        await self.bus.subscribe(topic, handler)
    
    def log_info(self, message: str, **kwargs: Any) -> None:
        """Log info message with optional extra fields."""
        self._logger.info(message, extra=kwargs)
    
    def log_warning(self, message: str, **kwargs: Any) -> None:
        """Log warning message with optional extra fields."""
        self._logger.warning(message, extra=kwargs)
    
    def log_error(self, message: str, exc_info: bool = False, **kwargs: Any) -> None:
        """Log error message with optional exception info."""
        self._logger.error(message, exc_info=exc_info, extra=kwargs)
    
    # =========================================================================
    # Properties
    # =========================================================================
    
    @property
    def status(self) -> ServiceStatus:
        """Get current service status."""
        return self._status
    
    @property
    def is_running(self) -> bool:
        """Check if service is running."""
        return self._status == ServiceStatus.RUNNING
    
    @property
    def is_healthy(self) -> bool:
        """Check if service is healthy based on last health check."""
        if self._last_health_check:
            return self._last_health_check.healthy
        return self._status == ServiceStatus.RUNNING
    
    @property
    def uptime_seconds(self) -> float:
        """Get service uptime in seconds."""
        return self._get_uptime_seconds()
    
    @property
    def error_message(self) -> Optional[str]:
        """Get last error message if in error state."""
        return self._error_message
    
    @property
    def stats(self) -> Dict[str, Any]:
        """Get service statistics."""
        return {
            "name": self.name,
            "status": str(self._status),
            "uptime_seconds": self._get_uptime_seconds(),
            "iteration_count": self._iteration_count,
            "error_count": self._error_count,
            "start_time": (
                self._start_time.isoformat() if self._start_time else None
            ),
            "last_health_check": (
                self._last_health_check.to_dict()
                if self._last_health_check
                else None
            ),
        }
    
    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}(name={self.name}, status={self._status})>"
