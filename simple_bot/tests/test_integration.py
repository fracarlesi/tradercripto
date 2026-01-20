"""
HLQuantBot v2.0 - Integration Tests
====================================

Comprehensive integration tests for the trading system.

Tests:
    - Import verification for all modules
    - MessageBus pub/sub functionality
    - Configuration loading and validation
    - Service lifecycle (start/stop/health)
    - Factory function verification

Running:
    pytest simple_bot/tests/test_integration.py -v

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
        import simple_bot
        assert simple_bot.__version__ == "2.0.0"
        assert hasattr(simple_bot, "run_bot")
        assert hasattr(simple_bot, "get_version")
    
    def test_import_message_bus(self):
        """Test message bus imports."""
        from simple_bot.services import MessageBus, Message, Topic, TopicStats
        
        assert MessageBus is not None
        assert Message is not None
        assert Topic is not None
        assert TopicStats is not None
    
    def test_import_base_service(self):
        """Test base service imports."""
        from simple_bot.services import (
            BaseService,
            ServiceStatus,
            HealthStatus,
            RetryConfig,
        )
        
        assert BaseService is not None
        assert ServiceStatus is not None
        assert HealthStatus is not None
        assert RetryConfig is not None
    
    def test_import_market_scanner(self):
        """Test market scanner imports."""
        from simple_bot.services import (
            MarketScannerService,
            CoinData,
            ScanMetrics,
            create_market_scanner,
        )
        
        assert MarketScannerService is not None
        assert CoinData is not None
        assert ScanMetrics is not None
        assert create_market_scanner is not None
    
    def test_import_opportunity_ranker(self):
        """Test opportunity ranker imports."""
        from simple_bot.services import (
            OpportunityRankerService,
            OpportunityScore,
            SymbolMetrics,
        )
        
        assert OpportunityRankerService is not None
        assert OpportunityScore is not None
        assert SymbolMetrics is not None
    
    def test_import_strategy_selector(self):
        """Test strategy selector imports."""
        from simple_bot.services import (
            StrategySelectorService,
            Signal,
            StrategyPerformance,
            create_strategy_selector,
        )
        
        assert StrategySelectorService is not None
        assert Signal is not None
        assert StrategyPerformance is not None
        assert create_strategy_selector is not None
    
    def test_import_capital_allocator(self):
        """Test capital allocator imports."""
        from simple_bot.services import (
            CapitalAllocatorService,
            Position,
            AccountState,
            SizedSignal,
            kelly_size,
            atr_size,
            risk_parity_weight,
            create_capital_allocator,
        )
        
        assert CapitalAllocatorService is not None
        assert Position is not None
        assert AccountState is not None
        assert SizedSignal is not None
        assert kelly_size is not None
        assert atr_size is not None
        assert risk_parity_weight is not None
        assert create_capital_allocator is not None
    
    def test_import_execution_engine(self):
        """Test execution engine imports."""
        from simple_bot.services import (
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
    
    def test_import_learning_module(self):
        """Test learning module imports."""
        from simple_bot.services import (
            LearningModuleService,
            StrategyMetrics,
            OptimizationResult,
            OptimizationCycle,
            create_learning_module,
        )
        
        assert LearningModuleService is not None
        assert StrategyMetrics is not None
        assert OptimizationResult is not None
        assert OptimizationCycle is not None
        assert create_learning_module is not None
    
    def test_import_config(self):
        """Test config loader imports."""
        from simple_bot.config.loader import (
            Config,
            ConfigLoader,
            load_config,
            get_config,
            reload_config,
            SystemConfig,
            HyperliquidConfig,
            DatabaseConfig,
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
    
    def test_import_llm_client(self):
        """Test LLM client imports."""
        from simple_bot.llm.client import (
            DeepSeekClient,
            StrategyDecision,
            MarketAnalysis,
            StrategyType,
            DirectionType,
            create_deepseek_client,
            RateLimiter,
            LLMError,
            RateLimitExceeded,
            APIError,
            ParseError,
        )
        
        assert DeepSeekClient is not None
        assert StrategyDecision is not None
        assert MarketAnalysis is not None
        assert StrategyType is not None
        assert DirectionType is not None
        assert create_deepseek_client is not None
    
    def test_import_api_client(self):
        """Test Hyperliquid API client imports."""
        from simple_bot.api.hyperliquid import (
            HyperliquidClient,
            create_client,
            OrderType,
        )
        from simple_bot.api.exceptions import (
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
        from simple_bot.main import HLQuantBot, main
        
        assert HLQuantBot is not None
        assert main is not None


# =============================================================================
# Test Message Bus
# =============================================================================

class TestMessageBus:
    """Test MessageBus functionality."""
    
    @pytest.fixture
    def bus(self):
        """Create message bus instance."""
        from simple_bot.services import MessageBus
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
        from simple_bot.services import Topic, Message
        
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
        from simple_bot.services import Topic, Message
        
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
        from simple_bot.services import Topic, Message
        
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
        from simple_bot.services import Topic, Message
        
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
        from simple_bot.services import Message, Topic
        
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
        from simple_bot.services import Topic, Message
        
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
        from simple_bot.config.loader import Config
        
        config = Config()
        
        assert config.system.name == "HLQuantBot-v2"
        assert config.system.mode == "testnet"
        assert config.hyperliquid.testnet is True
        assert config.risk.leverage == 5
    
    def test_system_config_validation(self):
        """Test system config validation."""
        from simple_bot.config.loader import SystemConfig
        
        config = SystemConfig(
            name="TestBot",
            mode="testnet",
            log_level="debug",  # type: ignore[arg-type]  # Test validator uppercases this
        )
        
        assert config.log_level == "DEBUG"
    
    def test_database_config_dsn(self):
        """Test database DSN generation."""
        from simple_bot.config.loader import DatabaseConfig
        
        config = DatabaseConfig(
            host="localhost",
            port=5432,
            name="test_db",
            user="test_user",
            password="test_pass",
        )
        
        expected = "postgresql://test_user:test_pass@localhost:5432/test_db"
        assert config.dsn == expected
    
    def test_database_pool_validation(self):
        """Test database pool size validation."""
        from simple_bot.config.loader import DatabaseConfig
        from pydantic import ValidationError
        
        with pytest.raises(ValidationError):
            DatabaseConfig(
                pool_min=10,
                pool_max=5,  # Invalid: min > max
            )
    
    def test_risk_config_validation(self):
        """Test risk config validation."""
        from simple_bot.config.loader import RiskConfig
        
        config = RiskConfig(
            leverage=10,
            stop_loss_pct=2.0,
            take_profit_pct=1.0,  # Less than stop loss - should warn
        )
        
        assert config.leverage == 10
        assert config.stop_loss_pct == 2.0
        assert config.take_profit_pct == 1.0
    
    def test_opportunity_weights_sum(self):
        """Test opportunity weights must sum to 1.0."""
        from simple_bot.config.loader import OpportunityWeights
        from pydantic import ValidationError
        
        # Valid weights
        valid = OpportunityWeights(
            trend_strength=0.25,
            volatility=0.20,
            volume=0.15,
            funding=0.15,
            liquidity=0.15,
            momentum=0.10,
        )
        assert valid is not None
        
        # Invalid weights (don't sum to 1.0)
        with pytest.raises(ValidationError):
            OpportunityWeights(
                trend_strength=0.5,
                volatility=0.5,
                volume=0.5,  # Sum > 1.0
            )
    
    def test_strategy_ema_validation(self):
        """Test momentum strategy EMA validation."""
        from simple_bot.config.loader import MomentumStrategyConfig
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
        from simple_bot.config.loader import resolve_env_vars
        
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
        from simple_bot.config.loader import resolve_env_vars
        
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
        from simple_bot.services import MessageBus
        bus = MessageBus()
        return bus
    
    @pytest.mark.asyncio
    async def test_base_service_lifecycle(self, mock_bus):
        """Test base service start/stop."""
        from simple_bot.services import BaseService, ServiceStatus
        
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
        from simple_bot.services import BaseService
        
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
        from simple_bot.services import BaseService, ServiceStatus
        
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
        from simple_bot.services import BaseService, Topic, Message
        
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
        from simple_bot.services import BaseService
        
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
        from simple_bot.services import BaseService
        
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
# Test LLM Client
# =============================================================================

class TestLLMClient:
    """Test DeepSeek LLM client."""
    
    def test_client_creation(self):
        """Test client creation without API key."""
        import os
        from simple_bot.llm.client import DeepSeekClient

        # Clear API key env var to test client without credentials
        old_key = os.environ.pop("DEEPSEEK_API_KEY", None)
        try:
            client = DeepSeekClient(api_key=None)

            assert client.is_available is False
            assert client.remaining_requests == 300
        finally:
            if old_key is not None:
                os.environ["DEEPSEEK_API_KEY"] = old_key
    
    def test_rate_limiter(self):
        """Test rate limiter functionality."""
        from simple_bot.llm.client import RateLimiter
        
        limiter = RateLimiter(max_per_day=5)
        
        assert limiter.can_make_request()
        assert limiter.remaining_today == 5
        
        # Record some requests
        for _ in range(5):
            limiter.record_request()
        
        assert not limiter.can_make_request()
        assert limiter.remaining_today == 0
    
    def test_strategy_decision_model(self):
        """Test StrategyDecision model validation."""
        from simple_bot.llm.client import (
            StrategyDecision,
            StrategyType,
            DirectionType,
        )
        
        decision = StrategyDecision(
            strategy=StrategyType.MOMENTUM,
            confidence=0.85,
            direction=DirectionType.LONG,
            reasoning="Strong uptrend with volume",
            entry_conditions=["EMA crossover", "RSI above 50"],
            risk_factors=["High volatility"],
        )
        
        assert decision.strategy == StrategyType.MOMENTUM
        assert decision.confidence == 0.85
        assert decision.direction == DirectionType.LONG
    
    def test_confidence_normalization(self):
        """Test confidence value normalization."""
        from simple_bot.llm.client import StrategyDecision, StrategyType, DirectionType
        
        # Test percentage string
        decision = StrategyDecision(
            strategy=StrategyType.MOMENTUM,
            confidence="85%",  # type: ignore[arg-type]  # Test validator converts string to float
            direction=DirectionType.LONG,
            reasoning="Test",
        )
        assert decision.confidence == 0.85
        
        # Test value > 1 (percentage as number)
        decision2 = StrategyDecision(
            strategy=StrategyType.MOMENTUM,
            confidence=75,  # Should convert to 0.75
            direction=DirectionType.LONG,
            reasoning="Test",
        )
        assert decision2.confidence == 0.75
    
    def test_market_analysis_model(self):
        """Test MarketAnalysis model."""
        from simple_bot.llm.client import MarketAnalysis, StrategyType
        
        analysis = MarketAnalysis(
            regime="bullish",
            trend_strength=0.8,
            risk_level="medium",
            summary="Market is trending up",
            recommended_strategies=[StrategyType.MOMENTUM],
            avoid_strategies=[StrategyType.MEAN_REVERSION],
        )
        
        assert analysis.regime == "bullish"
        assert analysis.trend_strength == 0.8
        assert StrategyType.MOMENTUM in analysis.recommended_strategies


# =============================================================================
# Test Factory Functions
# =============================================================================

class TestFactoryFunctions:
    """Test service factory functions."""
    
    @pytest.fixture
    def mock_bus(self):
        """Create mock message bus."""
        from simple_bot.services import MessageBus
        return MessageBus()
    
    @pytest.fixture
    def mock_db(self):
        """Create mock database."""
        db = MagicMock()
        db.health_check = AsyncMock(return_value=True)
        return db
    
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
    
    def test_create_market_scanner(self, mock_bus, mock_db, mock_exchange):
        """Test market scanner factory."""
        from simple_bot.services import create_market_scanner
        from simple_bot.config.loader import MarketScannerConfig
        
        config = MarketScannerConfig()
        
        scanner = create_market_scanner(
            bus=mock_bus,
            db=mock_db,
            config=config,
            testnet=True,
        )
        
        assert scanner is not None
        assert scanner.name == "market_scanner"
    
    def test_create_strategy_selector(self, mock_bus, mock_db, mock_llm):
        """Test strategy selector factory."""
        from simple_bot.services import create_strategy_selector
        from simple_bot.config.loader import StrategySelectorConfig
        
        config = StrategySelectorConfig()
        
        selector = create_strategy_selector(
            bus=mock_bus,
            db=mock_db,
            config=config,
        )
        
        assert selector is not None
        assert selector.name == "strategy_selector"
    
    def test_create_capital_allocator(self, mock_bus, mock_db, mock_exchange):
        """Test capital allocator factory."""
        from simple_bot.services import CapitalAllocatorService
        from simple_bot.config.loader import CapitalAllocatorConfig, RiskConfig
        
        # Create with explicit config to avoid get_config() call
        config = CapitalAllocatorConfig()
        risk_config = RiskConfig()
        
        allocator = CapitalAllocatorService(
            bus=mock_bus,
            db=mock_db,
            client=mock_exchange,
            config=config,
            risk_config=risk_config,
        )
        
        assert allocator is not None
        assert allocator.name == "capital_allocator"
    
    def test_create_execution_engine(self, mock_bus, mock_db, mock_exchange):
        """Test execution engine factory."""
        from simple_bot.services import ExecutionEngineService
        from simple_bot.config.loader import Config
        
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
            db=mock_db,
        )
        
        assert engine is not None
        assert engine.name == "execution_engine"
    
    def test_create_learning_module(self, mock_bus, mock_db, mock_llm):
        """Test learning module factory."""
        from simple_bot.services import create_learning_module
        
        module = create_learning_module(
            bus=mock_bus,
            db=mock_db,
            llm=mock_llm,
            config={},
        )
        
        assert module is not None
        assert module.name == "learning_module"


# =============================================================================
# Test Orchestrator
# =============================================================================

class TestOrchestrator:
    """Test main HLQuantBot orchestrator."""
    
    def test_orchestrator_creation(self):
        """Test orchestrator instantiation."""
        from simple_bot.main import HLQuantBot

        bot = HLQuantBot(config_path="simple_bot/config/intelligent_bot.yaml")

        assert bot is not None
        assert not bot.is_running
    
    def test_service_order(self):
        """Test service start order is defined."""
        from simple_bot.main import HLQuantBot

        # ConservativeBot service order
        expected_order = [
            "kill_switch",     # MUST be first - safety critical
            "market_state",    # Data provider
            "llm_veto",        # Filter (optional)
            "risk_manager",    # Sizing
            "execution",       # Order placement
            "telegram",        # Notifications (last, non-critical)
        ]

        assert HLQuantBot.SERVICE_ORDER == expected_order
    
    @pytest.mark.asyncio
    async def test_orchestrator_config_loading(self):
        """Test orchestrator loads config."""
        from simple_bot.main import HLQuantBot
        from simple_bot.config.loader import Config
        
        # Create with pre-loaded config
        config = Config()
        bot = HLQuantBot(config=config)
        
        assert bot.config == config


# =============================================================================
# Test Utility Functions
# =============================================================================

class TestUtilityFunctions:
    """Test utility functions."""
    
    def test_kelly_size(self):
        """Test Kelly criterion sizing."""
        from simple_bot.services import kelly_size
        
        # Win rate 60%, win/loss ratio 1.5
        size = kelly_size(0.6, 1.5, 0.5)
        
        assert size > 0
        assert size <= 0.5  # Half Kelly
    
    def test_atr_size(self):
        """Test ATR-based sizing."""
        from simple_bot.services import atr_size
        
        # $10000 capital, $100 ATR, 1% risk
        size = atr_size(10000.0, 100.0, 0.01, atr_multiplier=2.0)
        
        assert size > 0
    
    def test_risk_parity_weight(self):
        """Test risk parity weighting."""
        from simple_bot.services import risk_parity_weight
        
        volatilities = {"A": 0.2, "B": 0.3, "C": 0.4}
        weights = [risk_parity_weight(v, volatilities) for s, v in volatilities.items()]
        
        # Weights should sum close to 1.0
        assert abs(sum(weights) - 1.0) < 0.01
        
        # Higher volatility should have lower weight
        assert weights[0] > weights[2]


# =============================================================================
# Test Data Classes
# =============================================================================

class TestDataClasses:
    """Test data class serialization."""
    
    def test_coin_data_to_dict(self):
        """Test CoinData serialization."""
        from simple_bot.services import CoinData
        
        coin = CoinData(
            symbol="ETH",
            price=3000.0,
            volume_24h=1000000.0,
            change_24h_pct=2.5,
            open_interest=500000.0,
            funding_rate=0.01,
            predicted_funding=0.008,
            spread_pct=0.05,
            atr_pct=3.0,
        )
        
        d = coin.to_dict()
        
        assert d["symbol"] == "ETH"
        assert d["price"] == 3000.0
    
    def test_order_to_dict(self):
        """Test Order serialization."""
        from simple_bot.services import Order, OrderStatus
        
        order = Order(
            symbol="BTC",
            side="buy",
            size=0.1,
            price=50000.0,
            order_type="limit",
            status=OrderStatus.PENDING,
        )
        
        d = order.to_dict()
        
        assert d["symbol"] == "BTC"
        assert d["side"] == "buy"
        assert d["size"] == 0.1
    
    def test_health_status_to_dict(self):
        """Test HealthStatus serialization."""
        from simple_bot.services import HealthStatus, ServiceStatus
        
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
