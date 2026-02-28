"""
HLQuantBot v3.0 - Integration Tests
====================================

Comprehensive integration tests for the trading system.

Tests:
    - Import verification for all modules
    - MessageBus pub/sub functionality
    - Configuration loading and validation
    - Service lifecycle (start/stop/health)
    - Factory function verification

Running:
    pytest crypto_bot/tests/test_integration.py -v

Author: Francesco Carlesi
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# =============================================================================
# Test Imports
# =============================================================================

class TestImports:
    """Test that all modules can be imported correctly."""
    
    def test_import_package(self):
        """Test main package import."""
        import crypto_bot
        assert crypto_bot.__version__ == "3.0.0"
        assert hasattr(crypto_bot, "run_bot")
        assert hasattr(crypto_bot, "get_version")
    
    def test_import_message_bus(self):
        """Test message bus imports."""
        from crypto_bot.services import MessageBus, Message, Topic, TopicStats
        
        assert MessageBus is not None
        assert Message is not None
        assert Topic is not None
        assert TopicStats is not None
    
    def test_import_base_service(self):
        """Test base service imports."""
        from crypto_bot.services import (
            BaseService,
            ServiceStatus,
            HealthStatus,
            RetryConfig,
        )
        
        assert BaseService is not None
        assert ServiceStatus is not None
        assert HealthStatus is not None
        assert RetryConfig is not None
    
    def test_import_execution_engine(self):
        """Test execution engine imports."""
        from crypto_bot.services import (
            ExecutionEngineService,
            Order,
            ExecutionPosition,
            OrderStatus,
            PositionStatus,
            ExecutionMetrics,
            create_execution_engine,
        )
        
        assert ExecutionEngineService is not None
        assert Order is not None
        assert ExecutionPosition is not None
        assert OrderStatus is not None
        assert PositionStatus is not None
        assert ExecutionMetrics is not None
        assert create_execution_engine is not None
    
    def test_import_config(self):
        """Test config loader imports."""
        from crypto_bot.config.loader import (
            Config,
            ConfigLoader,
            load_config,
            get_config,
            reload_config,
            SystemConfig,
            HyperliquidConfig,
            ServicesConfig,
            RiskConfig,
            LLMConfig,
            StrategiesConfig,
        )

        assert Config is not None
        assert ConfigLoader is not None
        assert load_config is not None
        assert get_config is not None
        assert reload_config is not None
    
    def test_import_api_client(self):
        """Test Hyperliquid API client imports."""
        from crypto_bot.api.hyperliquid import (
            HyperliquidClient,
            create_client,
            OrderType,
        )
        from crypto_bot.api.exceptions import (
            HyperliquidError,
            RateLimitError,
            ConnectionError,
            AuthenticationError,
            OrderRejectedError,
        )
        
        assert HyperliquidClient is not None
        assert create_client is not None
        assert OrderType is not None
        assert HyperliquidError is not None
    
    def test_import_main_orchestrator(self):
        """Test main orchestrator import."""
        from crypto_bot.main import ConservativeBot, main

        assert ConservativeBot is not None
        assert main is not None


# =============================================================================
# Test Message Bus
# =============================================================================

class TestMessageBus:
    """Test MessageBus functionality."""
    
    @pytest.fixture
    def bus(self):
        """Create message bus instance."""
        from crypto_bot.services import MessageBus
        return MessageBus()
    
    @pytest.mark.asyncio
    async def test_bus_start_stop(self, bus):
        """Test bus start and stop."""
        assert not bus.is_running
        
        await bus.start()
        assert bus.is_running
        
        await bus.stop()
        assert not bus.is_running
    
    @pytest.mark.asyncio
    async def test_publish_subscribe(self, bus):
        """Test basic pub/sub."""
        from crypto_bot.services import Topic, Message
        
        await bus.start()
        
        received: List[Message] = []
        
        async def handler(msg: Message):
            received.append(msg)
        
        await bus.subscribe(Topic.MARKET_DATA, handler)
        await bus.publish(Topic.MARKET_DATA, {"test": 1}, source="test")
        
        # Wait for message processing
        await asyncio.sleep(0.2)
        
        assert len(received) == 1
        assert received[0].payload == {"test": 1}
        assert received[0].source == "test"
        assert received[0].topic == Topic.MARKET_DATA
        
        await bus.stop()
    
    @pytest.mark.asyncio
    async def test_multiple_subscribers(self, bus):
        """Test multiple subscribers receive messages."""
        from crypto_bot.services import Topic, Message
        
        await bus.start()
        
        received_1: List[Message] = []
        received_2: List[Message] = []
        
        async def handler_1(msg: Message):
            received_1.append(msg)
        
        async def handler_2(msg: Message):
            received_2.append(msg)
        
        await bus.subscribe(Topic.SIGNALS, handler_1)
        await bus.subscribe(Topic.SIGNALS, handler_2)
        await bus.publish(Topic.SIGNALS, {"signal": "buy"}, source="test")
        
        await asyncio.sleep(0.2)
        
        assert len(received_1) == 1
        assert len(received_2) == 1
        
        await bus.stop()
    
    @pytest.mark.asyncio
    async def test_topic_isolation(self, bus):
        """Test messages only go to correct topic subscribers."""
        from crypto_bot.services import Topic, Message
        
        await bus.start()
        
        received: List[Message] = []
        
        async def handler(msg: Message):
            received.append(msg)
        
        await bus.subscribe(Topic.MARKET_DATA, handler)
        
        # Publish to different topic
        await bus.publish(Topic.SIGNALS, {"signal": "buy"}, source="test")
        
        await asyncio.sleep(0.2)
        
        # Should not receive message from different topic
        assert len(received) == 0
        
        await bus.stop()
    
    @pytest.mark.asyncio
    async def test_unsubscribe(self, bus):
        """Test unsubscribe removes handler."""
        from crypto_bot.services import Topic, Message
        
        await bus.start()
        
        received: List[Message] = []
        
        async def handler(msg: Message):
            received.append(msg)
        
        await bus.subscribe(Topic.MARKET_DATA, handler)
        await bus.unsubscribe(Topic.MARKET_DATA, handler)
        
        await bus.publish(Topic.MARKET_DATA, {"test": 1}, source="test")
        
        await asyncio.sleep(0.2)
        
        assert len(received) == 0
        
        await bus.stop()
    
    @pytest.mark.asyncio
    async def test_message_age(self, bus):
        """Test message age calculation."""
        from crypto_bot.services import Message, Topic
        
        msg = Message(
            topic=Topic.MARKET_DATA,
            payload={"test": 1},
            source="test",
        )
        
        await asyncio.sleep(0.1)
        
        age_ms = msg.age_ms()
        assert age_ms >= 100  # At least 100ms
    
    @pytest.mark.asyncio
    async def test_statistics(self, bus):
        """Test bus statistics collection."""
        from crypto_bot.services import Topic, Message
        
        await bus.start()
        
        async def handler(msg: Message):
            pass
        
        await bus.subscribe(Topic.MARKET_DATA, handler)
        
        for i in range(5):
            await bus.publish(Topic.MARKET_DATA, {"count": i}, source="test")
        
        await asyncio.sleep(0.3)
        
        stats = bus.get_statistics()
        
        assert stats["running"] is True
        assert stats["total_messages"] >= 5
        assert "topics" in stats
        assert "market_data" in stats["topics"]
        
        await bus.stop()


# =============================================================================
# Test Configuration
# =============================================================================

class TestConfiguration:
    """Test configuration loading and validation."""
    
    def test_default_config_creation(self):
        """Test creating config with defaults."""
        from crypto_bot.config.loader import Config
        
        config = Config()
        
        assert config.system.name == "HLQuantBot-v2"
        assert config.system.mode == "testnet"
        assert config.hyperliquid.testnet is True
        assert config.risk.leverage == 5
    
    def test_system_config_validation(self):
        """Test system config validation."""
        from crypto_bot.config.loader import SystemConfig
        
        config = SystemConfig(
            name="TestBot",
            mode="testnet",
            log_level="debug",  # type: ignore[arg-type]  # Test validator uppercases this
        )
        
        assert config.log_level == "DEBUG"
    
    def test_risk_config_validation(self):
        """Test risk config validation."""
        from crypto_bot.config.loader import RiskConfig
        
        config = RiskConfig(
            leverage=10,
            stop_loss_pct=2.0,
            take_profit_pct=1.0,  # Less than stop loss - should warn
        )
        
        assert config.leverage == 10
        assert config.stop_loss_pct == 2.0
        assert config.take_profit_pct == 1.0
    
    def test_strategy_ema_validation(self):
        """Test momentum strategy EMA validation."""
        from crypto_bot.config.loader import MomentumStrategyConfig
        from pydantic import ValidationError
        
        # Valid config
        valid = MomentumStrategyConfig(
            ema_fast=20,
            ema_slow=50,
        )
        assert valid.ema_fast < valid.ema_slow
        
        # Invalid: fast >= slow
        with pytest.raises(ValidationError):
            MomentumStrategyConfig(
                ema_fast=50,
                ema_slow=20,
            )
    
    def test_env_var_resolution(self):
        """Test environment variable resolution in config."""
        from crypto_bot.config.loader import resolve_env_vars
        
        # Set test env var
        os.environ["TEST_VAR"] = "test_value"
        
        # Required var
        result = resolve_env_vars("${TEST_VAR}")
        assert result == "test_value"
        
        # Default value
        result = resolve_env_vars("${MISSING_VAR:default}")
        assert result == "default"
        
        # Clean up
        del os.environ["TEST_VAR"]
    
    def test_env_var_missing_required(self):
        """Test error on missing required env var."""
        from crypto_bot.config.loader import resolve_env_vars
        
        with pytest.raises(ValueError) as exc_info:
            resolve_env_vars("${DEFINITELY_MISSING_VAR}")
        
        assert "DEFINITELY_MISSING_VAR" in str(exc_info.value)


# =============================================================================
# Test Service Lifecycle
# =============================================================================

class TestServiceLifecycle:
    """Test service start/stop/health functionality."""
    
    @pytest.fixture
    def mock_bus(self):
        """Create mock message bus."""
        from crypto_bot.services import MessageBus
        bus = MessageBus()
        return bus
    
    @pytest.mark.asyncio
    async def test_base_service_lifecycle(self, mock_bus):
        """Test base service start/stop."""
        from crypto_bot.services import BaseService, ServiceStatus
        
        class TestService(BaseService):
            async def _on_start(self):
                pass
            
            async def _on_stop(self):
                pass
        
        await mock_bus.start()
        
        service = TestService(name="test_service", bus=mock_bus)
        
        assert service.status == ServiceStatus.STOPPED
        assert not service.is_running
        
        await service.start()
        
        assert service.status == ServiceStatus.RUNNING
        assert service.is_running
        
        await service.stop()
        
        assert service.status == ServiceStatus.STOPPED
        assert not service.is_running
        
        await mock_bus.stop()
    
    @pytest.mark.asyncio
    async def test_service_health_check(self, mock_bus):
        """Test service health check."""
        from crypto_bot.services import BaseService
        
        class TestService(BaseService):
            async def _on_start(self):
                pass
            
            async def _on_stop(self):
                pass
            
            async def _health_check_impl(self):
                return True
        
        await mock_bus.start()
        
        service = TestService(name="test_service", bus=mock_bus)
        await service.start()
        
        health = await service.health_check()
        
        assert health.healthy is True
        assert health.message == "healthy"
        
        await service.stop()
        await mock_bus.stop()
    
    @pytest.mark.asyncio
    async def test_service_restart(self, mock_bus):
        """Test service restart."""
        from crypto_bot.services import BaseService, ServiceStatus
        
        start_count = 0
        stop_count = 0
        
        class TestService(BaseService):
            async def _on_start(self):
                nonlocal start_count
                start_count += 1
            
            async def _on_stop(self):
                nonlocal stop_count
                stop_count += 1
        
        await mock_bus.start()
        
        service = TestService(name="test_service", bus=mock_bus)
        await service.start()
        
        assert start_count == 1
        
        await service.restart(delay=0.1)
        
        assert stop_count == 1
        assert start_count == 2
        assert service.status == ServiceStatus.RUNNING
        
        await service.stop()
        await mock_bus.stop()
    
    @pytest.mark.asyncio
    async def test_service_publish_subscribe(self, mock_bus):
        """Test service pub/sub through bus."""
        from crypto_bot.services import BaseService, Topic, Message
        
        received: List[Message] = []
        
        class TestService(BaseService):
            async def _on_start(self):
                await self.subscribe(Topic.MARKET_DATA, self.on_data)
            
            async def _on_stop(self):
                pass
            
            async def on_data(self, msg: Message):
                received.append(msg)
        
        await mock_bus.start()
        
        service = TestService(name="test_service", bus=mock_bus)
        await service.start()
        
        await service.publish(Topic.MARKET_DATA, {"test": 1})
        
        await asyncio.sleep(0.2)
        
        assert len(received) == 1
        assert received[0].source == "test_service"
        
        await service.stop()
        await mock_bus.stop()
    
    @pytest.mark.asyncio
    async def test_service_config_access(self, mock_bus):
        """Test service configuration access."""
        from crypto_bot.services import BaseService
        
        class TestService(BaseService):
            async def _on_start(self):
                pass
            
            async def _on_stop(self):
                pass
        
        await mock_bus.start()
        
        config = {
            "interval": 60,
            "nested": {
                "value": 100,
            },
        }
        
        service = TestService(
            name="test_service",
            bus=mock_bus,
            config=config,
        )
        
        assert service.get_config("interval") == 60
        assert service.get_config("nested.value") == 100
        assert service.get_config("missing", "default") == "default"
        
        await mock_bus.stop()
    
    @pytest.mark.asyncio
    async def test_service_stats(self, mock_bus):
        """Test service statistics."""
        from crypto_bot.services import BaseService
        
        class TestService(BaseService):
            async def _on_start(self):
                pass
            
            async def _on_stop(self):
                pass
            
            async def _run_iteration(self):
                await asyncio.sleep(0.1)
        
        await mock_bus.start()
        
        service = TestService(
            name="test_service",
            bus=mock_bus,
            loop_interval_seconds=0.1,
        )
        await service.start()
        
        await asyncio.sleep(0.5)
        
        stats = service.stats
        
        assert stats["name"] == "test_service"
        assert stats["status"] == "running"
        assert stats["iteration_count"] >= 1
        assert stats["error_count"] == 0
        
        await service.stop()
        await mock_bus.stop()


# =============================================================================
# Test Factory Functions
# =============================================================================

class TestFactoryFunctions:
    """Test service factory functions."""
    
    @pytest.fixture
    def mock_bus(self):
        """Create mock message bus."""
        from crypto_bot.services import MessageBus
        return MessageBus()
    
    @pytest.fixture
    def mock_exchange(self):
        """Create mock exchange client."""
        exchange = MagicMock()
        exchange.get_account_state = AsyncMock(return_value={
            "equity": 10000.0,
            "availableBalance": 8000.0,
            "marginUsed": 2000.0,
            "unrealizedPnl": 0.0,
            "positions": [],
        })
        return exchange
    
    @pytest.fixture
    def mock_llm(self):
        """Create mock LLM client."""
        llm = MagicMock()
        llm.is_available = True
        llm.remaining_requests = 100
        return llm
    
    def test_create_execution_engine(self, mock_bus, mock_exchange):
        """Test execution engine factory."""
        from crypto_bot.services import ExecutionEngineService
        from crypto_bot.config.loader import Config

        # Create full config with default values
        config = Config()

        # Need a real client mock with is_connected attribute
        mock_exchange.is_connected = True
        mock_exchange.get_positions = AsyncMock(return_value=[])
        mock_exchange.get_fills = AsyncMock(return_value=[])

        engine = ExecutionEngineService(
            bus=mock_bus,
            config=config,
            client=mock_exchange,
        )

        assert engine is not None
        assert engine.name == "execution_engine"
    
    # Removed: test_create_learning_module, TestOrchestrator, TestUtilityFunctions (deleted services)


# =============================================================================
# Test Data Classes
# =============================================================================

class TestDataClasses:
    """Test data class serialization."""

    def test_health_status_to_dict(self):
        """Test HealthStatus serialization."""
        from crypto_bot.services import HealthStatus, ServiceStatus
        
        health = HealthStatus(
            healthy=True,
            status=ServiceStatus.RUNNING,
            message="OK",
            details={"uptime": 3600},
        )
        
        d = health.to_dict()
        
        assert d["healthy"] is True
        assert d["status"] == "running"
        assert d["message"] == "OK"


# =============================================================================
# Run Tests
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
