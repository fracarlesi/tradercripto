"""
Tests for HLQuantBot Push Notification Service (ntfy.sh)
=========================================================

Unit tests for WhatsAppService using ntfy.sh API.

Run:
    pytest crypto_bot/tests/test_whatsapp_service.py -v
"""

import asyncio
import pytest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from crypto_bot.services.whatsapp_service import WhatsAppService, DEFAULT_NTFY_SERVER
from crypto_bot.services.message_bus import MessageBus, Message, Topic


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_bus():
    """Create a mock MessageBus."""
    bus = AsyncMock(spec=MessageBus)
    bus.is_running = True
    bus.subscribe = AsyncMock()
    bus.publish = AsyncMock()
    return bus


@pytest.fixture
def enabled_config():
    """Config with push notifications enabled."""
    return {
        "monitoring": {
            "whatsapp": {
                "enabled": True,
                "topic": "hlquantbot-test123",
                "server": "https://ntfy.sh",
                "alert_on": [
                    "trade_open",
                    "trade_close",
                    "kill_switch_trigger",
                    "error",
                    "fill",
                ],
            }
        }
    }


@pytest.fixture
def disabled_config():
    """Config with push notifications disabled."""
    return {
        "monitoring": {
            "whatsapp": {
                "enabled": False,
            }
        }
    }


@pytest.fixture(autouse=True)
def _isolate_dedup_global(tmp_path, monkeypatch):
    """Redirect dedup file to temp dir for ALL tests (prevents cross-test pollution)."""
    import crypto_bot.services.whatsapp_service as ws_mod
    monkeypatch.setattr(ws_mod, "_DEDUP_DIR", tmp_path)
    monkeypatch.setattr(ws_mod, "_DEDUP_FILE", tmp_path / "ntfy_dedup.json")


@pytest.fixture
def service(mock_bus, enabled_config):
    """Create a WhatsAppService instance."""
    return WhatsAppService(bus=mock_bus, config=enabled_config)


@pytest.fixture
def disabled_service(mock_bus, disabled_config):
    """Create a disabled WhatsAppService instance."""
    return WhatsAppService(bus=mock_bus, config=disabled_config)


# =============================================================================
# Initialization Tests
# =============================================================================

class TestWhatsAppInit:
    """Test push notification service initialization."""

    def test_init_enabled(self, service):
        assert service._enabled is True
        assert service._topic == "hlquantbot-test123"
        assert service._server == "https://ntfy.sh"
        assert "trade_open" in service._alert_on
        assert "kill_switch_trigger" in service._alert_on

    def test_init_disabled(self, disabled_service):
        assert disabled_service._enabled is False

    def test_init_missing_topic_disables(self, mock_bus):
        config = {
            "monitoring": {
                "whatsapp": {
                    "enabled": True,
                    "topic": "",
                }
            }
        }
        with patch.dict("os.environ", {"NTFY_TOPIC": ""}, clear=False):
            svc = WhatsAppService(bus=mock_bus, config=config)
            assert svc._enabled is False

    def test_init_env_vars(self, mock_bus):
        """Test that env vars are used as fallback."""
        config = {"monitoring": {"whatsapp": {"enabled": True}}}
        with patch.dict("os.environ", {
            "NTFY_TOPIC": "my-topic-from-env",
        }):
            svc = WhatsAppService(bus=mock_bus, config=config)
            assert svc._topic == "my-topic-from-env"
            assert svc._enabled is True

    def test_init_default_server(self, mock_bus):
        """Test default ntfy.sh server when not specified."""
        config = {
            "monitoring": {
                "whatsapp": {
                    "enabled": True,
                    "topic": "test-topic",
                }
            }
        }
        svc = WhatsAppService(bus=mock_bus, config=config)
        assert svc._server == DEFAULT_NTFY_SERVER

    def test_init_custom_server(self, mock_bus):
        """Test custom self-hosted ntfy server."""
        config = {
            "monitoring": {
                "whatsapp": {
                    "enabled": True,
                    "topic": "test-topic",
                    "server": "https://ntfy.example.com",
                }
            }
        }
        svc = WhatsAppService(bus=mock_bus, config=config)
        assert svc._server == "https://ntfy.example.com"

    def test_default_alert_on(self, mock_bus):
        """Test default alert types when not specified."""
        config = {
            "monitoring": {
                "whatsapp": {
                    "enabled": True,
                    "topic": "test-topic",
                }
            }
        }
        svc = WhatsAppService(bus=mock_bus, config=config)
        assert "trade_open" in svc._alert_on
        assert "trade_close" in svc._alert_on
        assert "kill_switch_trigger" in svc._alert_on
        assert "error" in svc._alert_on


# =============================================================================
# Send Message Tests
# =============================================================================

class TestSendMessage:
    """Test message sending via ntfy.sh API."""

    @pytest.mark.asyncio
    async def test_send_message_success(self, service):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        service._session = mock_session

        result = await service._send_message("Test message")
        assert result is True
        assert service._messages_sent == 1

        # Verify POST to correct URL
        call_args = mock_session.post.call_args
        url = call_args[0][0]
        assert "ntfy.sh/hlquantbot-test123" in url

    @pytest.mark.asyncio
    async def test_send_message_with_title(self, service):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        service._session = mock_session

        await service._send_message("Body text", title="My Title")

        call_kwargs = mock_session.post.call_args
        headers = call_kwargs[1]["headers"]
        assert headers["Title"] == "My Title"

    @pytest.mark.asyncio
    async def test_send_message_priority_sets_urgent(self, service):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        service._session = mock_session

        await service._send_message("Urgent!", priority=True)

        call_kwargs = mock_session.post.call_args
        headers = call_kwargs[1]["headers"]
        assert headers["Priority"] == "urgent"

    @pytest.mark.asyncio
    async def test_send_message_api_error(self, service):
        mock_resp = AsyncMock()
        mock_resp.status = 400
        mock_resp.text = AsyncMock(return_value="Bad request")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        service._session = mock_session

        result = await service._send_message("Test")
        assert result is False
        assert service._messages_failed == 1

    @pytest.mark.asyncio
    async def test_send_message_timeout(self, service):
        mock_session = MagicMock()
        mock_session.post = MagicMock(side_effect=asyncio.TimeoutError())
        service._session = mock_session

        result = await service._send_message("Test")
        assert result is False
        assert service._messages_failed == 1

    @pytest.mark.asyncio
    async def test_send_message_disabled(self, disabled_service):
        result = await disabled_service._send_message("Test")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_message_no_session(self, service):
        service._session = None
        result = await service._send_message("Test")
        assert result is False


# =============================================================================
# Rate Limiting Tests
# =============================================================================

class TestRateLimiting:
    """Test rate limiting behavior."""

    @pytest.mark.asyncio
    async def test_rate_limit_not_exceeded(self, service):
        result = await service._check_rate_limit()
        assert result is True

    @pytest.mark.asyncio
    async def test_rate_limit_exceeded(self, service):
        now = datetime.now(timezone.utc)
        service._message_timestamps = [now] * 20
        result = await service._check_rate_limit()
        assert result is False

    @pytest.mark.asyncio
    async def test_priority_bypasses_rate_limit(self, service):
        """Priority messages should bypass rate limiting."""
        now = datetime.now(timezone.utc)
        service._message_timestamps = [now] * 25  # Over limit

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        service._session = mock_session

        result = await service._send_message("Critical!", priority=True)
        assert result is True


# =============================================================================
# Event Handler Tests
# =============================================================================

class TestOrderEvents:
    """Test order event handling."""

    @pytest.mark.asyncio
    async def test_trade_open_long(self, service):
        service._send_message = AsyncMock(return_value=True)

        msg = Message(
            topic=Topic.ORDERS,
            payload={
                "event": "order_submitted",
                "signal": {
                    "symbol": "BTC",
                    "direction": "long",
                    "strategy": "trend_momentum",
                    "tp_price": "96000",
                    "sl_price": "94000",
                },
                "order": {
                    "size": "0.01",
                    "price": "95000",
                },
            },
            source="execution",
        )

        await service._on_order_event(msg)
        service._send_message.assert_called_once()
        text = service._send_message.call_args[0][0]
        assert "Trade Opened" in text
        assert "BTC" in text
        assert "LONG" in text

    @pytest.mark.asyncio
    async def test_trade_open_short(self, service):
        service._send_message = AsyncMock(return_value=True)

        msg = Message(
            topic=Topic.ORDERS,
            payload={
                "event": "order_submitted",
                "signal": {
                    "symbol": "ETH",
                    "direction": "short",
                    "strategy": "trend_momentum",
                    "tp_price": "3000",
                    "sl_price": "3200",
                },
                "order": {
                    "size": "0.1",
                    "price": "3100",
                },
            },
            source="execution",
        )

        await service._on_order_event(msg)
        service._send_message.assert_called_once()
        text = service._send_message.call_args[0][0]
        assert "SHORT" in text

    @pytest.mark.asyncio
    async def test_order_error(self, service):
        service._send_message = AsyncMock(return_value=True)

        msg = Message(
            topic=Topic.ORDERS,
            payload={
                "event": "order_error",
                "error": "Insufficient margin",
                "signal": {"symbol": "BTC"},
            },
            source="execution",
        )

        await service._on_order_event(msg)
        service._send_message.assert_called_once()
        text = service._send_message.call_args[0][0]
        assert "Order Error" in text
        assert "Insufficient margin" in text

    @pytest.mark.asyncio
    async def test_disabled_skips_events(self, disabled_service):
        disabled_service._send_message = AsyncMock()

        msg = Message(
            topic=Topic.ORDERS,
            payload={"event": "order_submitted", "signal": {}, "order": {}},
            source="execution",
        )
        await disabled_service._on_order_event(msg)
        disabled_service._send_message.assert_not_called()


class TestFillEvents:
    """Test fill event handling."""

    @pytest.mark.asyncio
    async def test_position_closed_profit(self, service):
        service._send_message = AsyncMock(return_value=True)

        msg = Message(
            topic=Topic.FILLS,
            payload={
                "event": "position_closed",
                "symbol": "BTC",
                "realized_pnl": 42.50,
                "pnl_pct": 0.85,
                "side": "long",
                "entry_price": 95000,
                "exit_price": 95800,
            },
            source="execution",
        )

        await service._on_fill_event(msg)
        service._send_message.assert_called_once()
        text = service._send_message.call_args[0][0]
        assert "Position Closed" in text
        assert "+$42.50" in text

    @pytest.mark.asyncio
    async def test_position_closed_loss(self, service):
        service._send_message = AsyncMock(return_value=True)

        msg = Message(
            topic=Topic.FILLS,
            payload={
                "event": "position_closed",
                "symbol": "BTC",
                "realized_pnl": -18.30,
                "pnl_pct": -0.40,
                "side": "long",
                "entry_price": 95000,
                "exit_price": 94620,
            },
            source="execution",
        )

        await service._on_fill_event(msg)
        service._send_message.assert_called_once()
        text = service._send_message.call_args[0][0]
        assert "Position Closed" in text
        assert "$-18.30" in text


class TestRiskAlerts:
    """Test risk alert handling."""

    @pytest.mark.asyncio
    async def test_kill_switch_alert(self, service):
        service._send_message = AsyncMock(return_value=True)

        msg = Message(
            topic=Topic.RISK_ALERTS,
            payload={
                "trigger_type": "daily_loss",
                "trigger_value": 8.5,
                "threshold": 8.0,
                "action": "pause_until_tomorrow",
                "equity": 79.10,
                "message": "Daily loss limit exceeded",
            },
            source="kill_switch",
        )

        await service._on_risk_alert(msg)
        service._send_message.assert_called_once()
        call_kwargs = service._send_message.call_args
        assert call_kwargs[1].get("priority") is True
        text = call_kwargs[0][0]
        assert "KILL SWITCH" in text
        assert "DAILY_LOSS" in text


# =============================================================================
# Batching Tests
# =============================================================================

class TestBatching:
    """Test message batching."""

    @pytest.mark.asyncio
    async def test_queue_and_flush(self, service):
        service._send_message = AsyncMock(return_value=True)

        await service._queue_message("msg1")
        await service._queue_message("msg2")

        assert len(service._pending_messages) == 2

        await service._flush_pending_messages()

        service._send_message.assert_called_once()
        text = service._send_message.call_args[0][0]
        assert "Batched Updates (2)" in text
        assert "msg1" in text
        assert "msg2" in text

    @pytest.mark.asyncio
    async def test_flush_empty_does_nothing(self, service):
        service._send_message = AsyncMock()
        await service._flush_pending_messages()
        service._send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_single_pending_no_batch_header(self, service):
        service._send_message = AsyncMock(return_value=True)

        await service._queue_message("only one")
        await service._flush_pending_messages()

        text = service._send_message.call_args[0][0]
        assert "Batched" not in text
        assert "only one" in text


# =============================================================================
# Health Check Tests
# =============================================================================

class TestHealthCheck:
    """Test health check functionality."""

    @pytest.mark.asyncio
    async def test_health_disabled(self, disabled_service):
        result = await disabled_service._health_check_impl()
        assert result is True

    @pytest.mark.asyncio
    async def test_health_no_session(self, service):
        service._session = None
        result = await service._health_check_impl()
        assert result is False

    @pytest.mark.asyncio
    async def test_health_session_open(self, service):
        mock_session = MagicMock()
        mock_session.closed = False
        service._session = mock_session
        result = await service._health_check_impl()
        assert result is True

    @pytest.mark.asyncio
    async def test_health_session_closed(self, service):
        mock_session = MagicMock()
        mock_session.closed = True
        service._session = mock_session
        result = await service._health_check_impl()
        assert result is False


# =============================================================================
# Stats Tests
# =============================================================================

class TestStats:
    """Test service statistics."""

    def test_stats_includes_fields(self, service):
        stats = service.stats
        assert "enabled" in stats
        assert "messages_sent" in stats
        assert "messages_failed" in stats
        assert "pending_messages" in stats
        assert stats["enabled"] is True
        assert stats["messages_sent"] == 0

    def test_stats_after_activity(self, service):
        service._messages_sent = 5
        service._messages_failed = 1
        stats = service.stats
        assert stats["messages_sent"] == 5
        assert stats["messages_failed"] == 1


# =============================================================================
# Persistent Dedup Tests
# =============================================================================

class TestPersistentDedup:
    """Test file-backed dedup that survives restarts."""

    def _make_service(self, mock_bus, enabled_config):
        return WhatsAppService(bus=mock_bus, config=enabled_config)

    def test_duplicate_trade_close_blocked(self, mock_bus, enabled_config):
        """Same trade identity should be deduped."""
        svc = self._make_service(mock_bus, enabled_config)
        key = "close_BTC_long_95000_42.50"
        assert svc._is_duplicate(key) is False  # First time → not duplicate
        assert svc._is_duplicate(key) is True   # Second time → duplicate

    def test_different_trades_not_blocked(self, mock_bus, enabled_config):
        """Different trades should not be deduped."""
        svc = self._make_service(mock_bus, enabled_config)
        assert svc._is_duplicate("close_BTC_long_95000_42.50") is False
        assert svc._is_duplicate("close_ETH_short_3100_-18.30") is False

    def test_dedup_survives_reload(self, mock_bus, enabled_config):
        """Dedup state persists to disk and is reloaded."""
        svc = self._make_service(mock_bus, enabled_config)
        key = "close_BTC_long_95000_42.50"
        svc._is_duplicate(key)

        # Create new service instance (simulates restart)
        new_service = self._make_service(mock_bus, enabled_config)
        assert new_service._is_duplicate(key) is True

    def test_dedup_ttl_expiry(self, mock_bus, enabled_config):
        """Expired keys should not block."""
        svc = self._make_service(mock_bus, enabled_config)
        key = "close_BTC_long_95000_42.50"
        # Set key with expiry in the past
        svc._dedup_keys[key] = datetime.now(timezone.utc).timestamp() - 1
        assert svc._is_duplicate(key) is False  # Expired → allows re-send

    @pytest.mark.asyncio
    async def test_duplicate_fill_event_blocked(self, mock_bus, enabled_config):
        """Sending the same position_closed event twice should only notify once."""
        svc = self._make_service(mock_bus, enabled_config)
        svc._send_message = AsyncMock(return_value=True)

        msg = Message(
            topic=Topic.FILLS,
            payload={
                "event": "position_closed",
                "symbol": "GRASS",
                "realized_pnl": -0.45,
                "pnl_pct": -0.80,
                "side": "long",
                "entry_price": 2.10,
                "exit_price": 2.08,
            },
            source="execution",
        )

        await svc._on_fill_event(msg)
        await svc._on_fill_event(msg)  # Duplicate

        assert svc._send_message.call_count == 1

    @pytest.mark.asyncio
    async def test_no_duplicate_after_restart(self, mock_bus, enabled_config):
        """After restart, same trade should still be deduped."""
        svc = self._make_service(mock_bus, enabled_config)
        svc._send_message = AsyncMock(return_value=True)

        msg = Message(
            topic=Topic.FILLS,
            payload={
                "event": "position_closed",
                "symbol": "BTC",
                "realized_pnl": 42.50,
                "pnl_pct": 0.85,
                "side": "long",
                "entry_price": 95000,
                "exit_price": 95800,
            },
            source="execution",
        )

        await svc._on_fill_event(msg)
        assert svc._send_message.call_count == 1

        # Simulate restart
        new_service = self._make_service(mock_bus, enabled_config)
        new_service._send_message = AsyncMock(return_value=True)

        await new_service._on_fill_event(msg)
        new_service._send_message.assert_not_called()  # Deduped across restart
