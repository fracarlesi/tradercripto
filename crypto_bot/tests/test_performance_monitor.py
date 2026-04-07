"""
Tests for PerformanceMonitorService — snapshot notification.
=============================================================

Verifies:
- Level-scoped metrics (trades filtered by capital_ladder.started_at)
- Fallback to 12-hour rolling window when no ladder
- Ladder status displayed correctly from persisted state
- Notification format (concise, no redundancy)

Run:
    pytest crypto_bot/tests/test_performance_monitor.py -v
"""

from __future__ import annotations

import pytest
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from crypto_bot.services.performance_monitor import (
    PerformanceMonitorService,
    TradeRecord,
)
from crypto_bot.services.capital_ladder import (
    LadderState,
    LevelConfig,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _make_trade(
    symbol: str = "BTC",
    pnl: float = 0.1,
    closed_at: Optional[str] = None,
) -> TradeRecord:
    if closed_at is None:
        closed_at = datetime.now(timezone.utc).isoformat()
    return TradeRecord(
        symbol=symbol,
        direction="long",
        entry_price=100.0,
        exit_price=100.1,
        realized_pnl=pnl,
        pnl_pct=0.1,
        exit_reason="take_profit",
        closed_at=closed_at,
    )


def _make_ladder(
    current_level: int = 1,
    level_label: str = "step_250",
    status: str = "TRACKING",
    started_at: Optional[str] = None,
    target_capital_usd: float = 250.0,
    levels: Optional[List[LevelConfig]] = None,
) -> MagicMock:
    """Create a mock CapitalLadderService with realistic state."""
    ladder = MagicMock()
    state = LadderState(
        current_level=current_level,
        level_label=level_label,
        target_capital_usd=target_capital_usd,
        started_at=started_at or datetime.now(timezone.utc).isoformat(),
        status=status,
    )
    ladder._state = state
    ladder._levels = levels or [
        LevelConfig(
            level=0,
            label="test_65",
            target_capital_usd=65,
            min_closed_trades=30,
            min_live_days=14,
            min_profit_factor=1.15,
            min_net_pnl_usd=0.5,
            max_drawdown_pct=10.0,
            min_maker_fill_ratio_pct=70.0,
        ),
        LevelConfig(
            level=1,
            label="step_250",
            target_capital_usd=250,
            min_closed_trades=30,
            min_live_days=14,
            min_profit_factor=1.15,
            min_net_pnl_usd=2.0,
            max_drawdown_pct=10.0,
            min_maker_fill_ratio_pct=70.0,
        ),
    ]
    return ladder


def _make_whatsapp() -> MagicMock:
    wa = MagicMock()
    wa._send_message = AsyncMock()
    return wa


def _make_exchange(equity: float = 60.94, positions: Optional[list] = None) -> MagicMock:
    ex = MagicMock()
    ex.get_account_state = AsyncMock(return_value={"equity": equity})
    ex.get_positions = AsyncMock(return_value=positions or [])
    return ex


def _make_service(
    trades: Optional[List[TradeRecord]] = None,
    capital_ladder: Optional[Any] = None,
    whatsapp: Optional[Any] = None,
    exchange: Optional[Any] = None,
) -> PerformanceMonitorService:
    svc = PerformanceMonitorService(
        config={},
        whatsapp=whatsapp or _make_whatsapp(),
        exchange=exchange or _make_exchange(),
        capital_ladder=capital_ladder,
    )
    svc._trades = trades or []
    return svc


# ── Test: _get_level_trades filtering ────────────────────────────────


class TestGetLevelTrades:

    def test_filters_by_ladder_started_at(self) -> None:
        """Trades before the level start are excluded."""
        level_start = datetime(2026, 3, 10, 0, 0, 0, tzinfo=timezone.utc)
        old_trade = _make_trade(closed_at="2026-03-05T12:00:00+00:00", pnl=-0.5)
        new_trade = _make_trade(closed_at="2026-03-12T12:00:00+00:00", pnl=0.3)

        ladder = _make_ladder(started_at=level_start.isoformat())
        svc = _make_service(trades=[old_trade, new_trade], capital_ladder=ladder)

        trades, label, level_num, status, *_ = svc._get_level_trades()
        assert len(trades) == 1
        assert trades[0].realized_pnl == 0.3

    def test_returns_level_metadata(self) -> None:
        """Metadata from ladder state is returned correctly."""
        ladder = _make_ladder(
            current_level=1,
            level_label="step_250",
            status="TRACKING",
            target_capital_usd=250.0,
        )
        svc = _make_service(capital_ladder=ladder)

        _, label, level_num, status, target_cap, min_trades, min_days = svc._get_level_trades()
        assert level_num == 1
        assert label == "step_250"
        assert status == "TRACKING"
        assert target_cap == 250.0
        assert min_trades == 30
        assert min_days == 14

    def test_fallback_to_12h_without_ladder(self) -> None:
        """Without a ladder, uses 12-hour rolling window."""
        old_trade = _make_trade(
            closed_at=(datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(),
            pnl=-0.5,
        )
        recent_trade = _make_trade(
            closed_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
            pnl=0.3,
        )
        svc = _make_service(trades=[old_trade, recent_trade], capital_ladder=None)

        trades, label, level_num, *_ = svc._get_level_trades()
        assert level_num == -1
        assert label == "12h"
        assert len(trades) == 1  # Only the recent trade

    def test_empty_started_at_falls_back(self) -> None:
        """If ladder exists but started_at is empty, falls back to 30d."""
        ladder = _make_ladder()
        # Manually override started_at to empty (bypassing the or-default in helper)
        ladder._state.started_at = ""
        svc = _make_service(capital_ladder=ladder)

        _, _, level_num, *_ = svc._get_level_trades()
        # started_at is empty string which is falsy -> should fall back
        assert level_num == -1


# ── Test: Snapshot notification content ──────────────────────────────


class TestSnapshotNotification:

    @pytest.mark.asyncio
    async def test_ladder_active_format(self) -> None:
        """When ladder is active, report shows level-scoped metrics."""
        level_start = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
        trades = [
            _make_trade(symbol="ETH", pnl=0.5, closed_at=datetime.now(timezone.utc).isoformat()),
            _make_trade(symbol="BTC", pnl=-0.3, closed_at=datetime.now(timezone.utc).isoformat()),
        ]
        ladder = _make_ladder(
            current_level=1,
            status="TRACKING",
            started_at=level_start,
        )
        wa = _make_whatsapp()
        svc = _make_service(trades=trades, capital_ladder=ladder, whatsapp=wa)

        await svc._send_scheduled_report()

        wa._send_message.assert_called_once()
        call_args = wa._send_message.call_args
        body = call_args[0][0] if call_args[0] else call_args[1].get("message", "")
        title = call_args[1].get("title", "") if call_args[1] else ""

        # Title should be concise "Snapshot HH:MM DD/MM"
        assert title.startswith("Snapshot ")
        # No "ACCOUNT SNAPSHOT" or "Time:" in body
        assert "ACCOUNT SNAPSHOT" not in body
        assert "Time:" not in body
        # Should contain level info
        assert "Level 1" in body
        assert "$250" in body
        assert "TRACKING" in body
        # Should show WR, PF, DD
        assert "WR:" in body
        assert "PF:" in body
        assert "DD:" in body
        # Should show remaining requirements
        assert "need" in body

    @pytest.mark.asyncio
    async def test_no_ladder_fallback_format(self) -> None:
        """Without ladder, report shows 12h rolling window."""
        trades = [
            _make_trade(pnl=0.5, closed_at=datetime.now(timezone.utc).isoformat()),
        ]
        wa = _make_whatsapp()
        svc = _make_service(trades=trades, capital_ladder=None, whatsapp=wa)

        await svc._send_scheduled_report()

        body = wa._send_message.call_args[0][0]
        assert "12h:" in body
        assert "Level" not in body

    @pytest.mark.asyncio
    async def test_fallback_snapshot_uses_12h_window(self) -> None:
        """Fallback snapshot includes only trades closed in the last 12 hours."""
        now = datetime.now(timezone.utc)
        trades = [
            # Inside 12h window
            _make_trade(symbol="ETH", pnl=0.4, closed_at=(now - timedelta(hours=1)).isoformat()),
            _make_trade(symbol="BTC", pnl=0.2, closed_at=(now - timedelta(hours=6)).isoformat()),
            _make_trade(symbol="SOL", pnl=-0.1, closed_at=(now - timedelta(hours=11, minutes=30)).isoformat()),
            # Outside 12h window
            _make_trade(symbol="OLD1", pnl=99.0, closed_at=(now - timedelta(hours=13)).isoformat()),
            _make_trade(symbol="OLD2", pnl=-50.0, closed_at=(now - timedelta(days=2)).isoformat()),
            _make_trade(symbol="OLD3", pnl=10.0, closed_at=(now - timedelta(days=20)).isoformat()),
        ]
        wa = _make_whatsapp()
        svc = _make_service(trades=trades, capital_ladder=None, whatsapp=wa)

        await svc._send_scheduled_report()

        body = wa._send_message.call_args[0][0]
        # Only the 3 in-window trades should be counted
        assert "12h: 3 trades" in body
        # Old symbols must not appear in the Top line
        assert "OLD1" not in body
        assert "OLD2" not in body
        assert "OLD3" not in body

    @pytest.mark.asyncio
    async def test_ladder_status_reads_from_state(self) -> None:
        """Ladder status comes from persisted _state, not recomputed."""
        ladder = _make_ladder(
            current_level=1,
            status="TRACKING",
            started_at=(datetime.now(timezone.utc) - timedelta(days=5)).isoformat(),
        )
        wa = _make_whatsapp()
        svc = _make_service(capital_ladder=ladder, whatsapp=wa)

        await svc._send_scheduled_report()

        body = wa._send_message.call_args[0][0]
        # Should show TRACKING, not REGRESS or any other recomputed status
        assert "TRACKING" in body
        assert "REGRESS" not in body

    @pytest.mark.asyncio
    async def test_excludes_old_config_trades(self) -> None:
        """Trades from before the current level start are excluded from metrics."""
        level_start = datetime(2026, 3, 18, 0, 0, 0, tzinfo=timezone.utc)
        old_trades = [
            _make_trade(pnl=-0.5, closed_at="2026-03-01T12:00:00+00:00")
            for _ in range(100)
        ]
        new_trades = [
            _make_trade(pnl=0.3, closed_at="2026-03-18T10:00:00+00:00"),
            _make_trade(pnl=-0.1, closed_at="2026-03-18T11:00:00+00:00"),
        ]
        ladder = _make_ladder(started_at=level_start.isoformat())
        wa = _make_whatsapp()
        svc = _make_service(
            trades=old_trades + new_trades,
            capital_ladder=ladder,
            whatsapp=wa,
        )

        await svc._send_scheduled_report()

        body = wa._send_message.call_args[0][0]
        # Should show "2 trades" (only new ones), not "102 trades"
        assert "2 trades" in body

    @pytest.mark.asyncio
    async def test_remaining_requirements(self) -> None:
        """Status line shows how many trades/days are still needed."""
        level_start = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        trades = [
            _make_trade(closed_at=datetime.now(timezone.utc).isoformat())
            for _ in range(10)
        ]
        ladder = _make_ladder(started_at=level_start)
        wa = _make_whatsapp()
        svc = _make_service(trades=trades, capital_ladder=ladder, whatsapp=wa)

        await svc._send_scheduled_report()

        body = wa._send_message.call_args[0][0]
        # min_closed_trades=30, we have 10 -> need 20 trades
        assert "20 trades" in body
        # min_live_days=14, we have 5 -> need 9 days
        assert "9 days" in body


# ── Test: Metrics property ──────────────────────────────────────────


class TestMetricsProperty:

    def test_metrics_scoped_to_level(self) -> None:
        """metrics property returns level-scoped data."""
        level_start = datetime(2026, 3, 10, 0, 0, 0, tzinfo=timezone.utc)
        old_trade = _make_trade(closed_at="2026-03-05T12:00:00+00:00", pnl=-10.0)
        new_trade = _make_trade(closed_at="2026-03-12T12:00:00+00:00", pnl=0.5)

        ladder = _make_ladder(started_at=level_start.isoformat())
        svc = _make_service(trades=[old_trade, new_trade], capital_ladder=ladder)

        m = svc.metrics
        assert m["total_trades"] == 1
        assert m["total_pnl"] == 0.5
        assert m["level"] == 1
        assert m["level_label"] == "step_250"

    def test_metrics_without_ladder(self) -> None:
        """Without ladder, metrics use 12h window."""
        recent = _make_trade(
            closed_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
            pnl=1.0,
        )
        svc = _make_service(trades=[recent], capital_ladder=None)

        m = svc.metrics
        assert m["total_trades"] == 1
        assert m["level"] == -1


# ── Test: Win rate alert uses level trades ───────────────────────────


class TestWinRateAlert:

    @pytest.mark.asyncio
    async def test_alert_uses_level_trades(self) -> None:
        """Win rate check uses only current-level trades."""
        level_start = datetime(2026, 3, 10, 0, 0, 0, tzinfo=timezone.utc)
        # 20 losing trades BEFORE level start
        old_losers = [
            _make_trade(pnl=-1.0, closed_at="2026-03-05T12:00:00+00:00")
            for _ in range(20)
        ]
        # 3 winning trades AFTER level start (below MIN_TRADES_FOR_ALERT=10)
        new_winners = [
            _make_trade(pnl=0.5, closed_at="2026-03-12T12:00:00+00:00")
            for _ in range(3)
        ]

        ladder = _make_ladder(started_at=level_start.isoformat())
        svc = _make_service(trades=old_losers + new_winners, capital_ladder=ladder)

        # Should NOT log warning — only 3 level trades (< 10 threshold)
        with patch.object(svc, "_logger") as mock_logger:
            await svc._check_win_rate_alert()
            mock_logger.warning.assert_not_called()
