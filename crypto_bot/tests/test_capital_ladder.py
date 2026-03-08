"""Tests for CapitalLadderService."""

from __future__ import annotations

import json
import pytest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from crypto_bot.services.capital_ladder import (
    CapitalLadderService,
    LadderState,
    LevelConfig,
    LevelMetrics,
    STATE_FILE,
)


# ── Helpers ────────────────────────────────────────────────────────────

def _make_config(
    enabled: bool = True,
    current_level: int = 0,
    auto_promote: bool = False,
    auto_demote: bool = False,
) -> Dict[str, Any]:
    return {
        "capital_ladder": {
            "enabled": enabled,
            "current_level": current_level,
            "auto_promote": auto_promote,
            "auto_demote": auto_demote,
            "levels": [
                {
                    "level": 0,
                    "label": "test_65",
                    "target_capital_usd": 65,
                    "min_closed_trades": 30,
                    "min_live_days": 14,
                    "min_profit_factor": 1.15,
                    "min_net_pnl_usd": 0.5,
                    "max_drawdown_pct": 10.0,
                    "min_maker_fill_ratio_pct": 70.0,
                },
                {
                    "level": 1,
                    "label": "step_250",
                    "target_capital_usd": 250,
                    "min_closed_trades": 30,
                    "min_live_days": 14,
                    "min_profit_factor": 1.15,
                    "min_net_pnl_usd": 2.0,
                    "max_drawdown_pct": 10.0,
                    "min_maker_fill_ratio_pct": 70.0,
                },
            ],
        }
    }


@dataclass
class FakeTrade:
    symbol: str = "BTC"
    realized_pnl: float = 0.1
    closed_at: str = ""
    fill_type: str = "maker"


def _fake_perf_monitor(trades: List[FakeTrade]) -> MagicMock:
    pm = MagicMock()
    pm._trades = trades
    return pm


def _fake_whatsapp() -> MagicMock:
    wa = MagicMock()
    wa._send_message = AsyncMock()
    return wa


# ── Tests ──────────────────────────────────────────────────────────────


class TestLadderInit:

    def test_disabled(self) -> None:
        svc = CapitalLadderService(config=_make_config(enabled=False))
        assert svc._enabled is False
        assert svc._levels == []

    def test_enabled_parses_levels(self) -> None:
        svc = CapitalLadderService(config=_make_config())
        assert svc._enabled is True
        assert len(svc._levels) == 2
        assert svc._levels[0].label == "test_65"
        assert svc._levels[1].target_capital_usd == 250

    def test_config_level_stored(self) -> None:
        svc = CapitalLadderService(config=_make_config(current_level=1))
        assert svc._config_level == 1


class TestBlockers:

    def test_all_blockers_when_empty(self) -> None:
        svc = CapitalLadderService(config=_make_config())
        cfg = svc._levels[0]
        m = LevelMetrics()
        blockers = svc._get_blockers(cfg, m)
        # At minimum: closed_trades, live_days, net_pnl
        assert any("closed_trades" in b for b in blockers)
        assert any("live_days" in b for b in blockers)
        assert any("net_pnl" in b for b in blockers)

    def test_no_blockers_when_all_met(self) -> None:
        svc = CapitalLadderService(config=_make_config())
        cfg = svc._levels[0]
        m = LevelMetrics(
            closed_trades=50,
            live_days=30,
            net_pnl=5.0,
            profit_factor=1.5,
            max_drawdown_pct=3.0,
            maker_fill_ratio_pct=85.0,
        )
        blockers = svc._get_blockers(cfg, m)
        assert blockers == []

    def test_pf_not_checked_below_5_trades(self) -> None:
        svc = CapitalLadderService(config=_make_config())
        cfg = svc._levels[0]
        m = LevelMetrics(
            closed_trades=3,
            live_days=30,
            net_pnl=5.0,
            profit_factor=0.5,  # Would fail if checked
            max_drawdown_pct=3.0,
            maker_fill_ratio_pct=85.0,
        )
        blockers = svc._get_blockers(cfg, m)
        # profit_factor blocker should NOT appear (< 5 trades)
        assert not any("profit_factor" in b for b in blockers)


class TestRegress:

    def test_no_regress_early(self) -> None:
        svc = CapitalLadderService(config=_make_config())
        cfg = svc._levels[0]
        m = LevelMetrics(closed_trades=5, net_pnl=-1.0, profit_factor=0.5)
        reasons = svc._check_regress(cfg, m)
        assert reasons == []  # Too few trades

    def test_regress_on_negative_pnl_low_pf(self) -> None:
        svc = CapitalLadderService(config=_make_config())
        cfg = svc._levels[0]
        m = LevelMetrics(closed_trades=20, net_pnl=-5.0, profit_factor=0.8)
        reasons = svc._check_regress(cfg, m)
        assert any("negative" in r for r in reasons)

    def test_regress_on_extreme_drawdown(self) -> None:
        svc = CapitalLadderService(config=_make_config())
        cfg = svc._levels[0]
        # Max DD limit is 10%, threshold is 13% (130%)
        m = LevelMetrics(closed_trades=5, max_drawdown_pct=14.0)
        reasons = svc._check_regress(cfg, m)
        assert any("drawdown" in r for r in reasons)

    def test_regress_on_low_maker_fill(self) -> None:
        svc = CapitalLadderService(config=_make_config())
        cfg = svc._levels[0]
        # Target 70%, regress below 55% (70-15)
        m = LevelMetrics(closed_trades=15, maker_fill_ratio_pct=50.0)
        reasons = svc._check_regress(cfg, m)
        assert any("maker fill" in r for r in reasons)


class TestEvaluation:

    @pytest.mark.asyncio
    async def test_tracking_initial_state(self) -> None:
        svc = CapitalLadderService(
            config=_make_config(),
            performance_monitor=_fake_perf_monitor([]),
            whatsapp=_fake_whatsapp(),
        )
        svc._state.started_at = datetime.now(timezone.utc).isoformat()
        svc._state.baseline_equity = 65.0
        svc._state.status = "TRACKING"

        await svc._evaluate()
        # Few trades + few days → should stay TRACKING
        assert svc._state.status == "TRACKING"

    @pytest.mark.asyncio
    async def test_ready_to_scale(self) -> None:
        now = datetime.now(timezone.utc)
        trades = [
            FakeTrade(
                symbol="BTC",
                realized_pnl=0.05,
                closed_at=now.isoformat(),
            )
            for _ in range(35)
        ]
        svc = CapitalLadderService(
            config=_make_config(),
            performance_monitor=_fake_perf_monitor(trades),
            whatsapp=_fake_whatsapp(),
        )
        svc._state.started_at = "2026-01-01T00:00:00+00:00"  # 60+ days ago
        svc._state.baseline_equity = 65.0
        svc._state.status = "TRACKING"

        await svc._evaluate()
        assert svc._state.status == "READY_TO_SCALE"

    @pytest.mark.asyncio
    async def test_hold_when_blockers_exist(self) -> None:
        now = datetime.now(timezone.utc)
        # 10 trades — enough to not be TRACKING, not enough for min_closed_trades=30
        trades = [
            FakeTrade(
                symbol="ETH",
                realized_pnl=0.1,
                closed_at=now.isoformat(),
            )
            for _ in range(10)
        ]
        svc = CapitalLadderService(
            config=_make_config(),
            performance_monitor=_fake_perf_monitor(trades),
            whatsapp=_fake_whatsapp(),
        )
        svc._state.started_at = "2026-01-01T00:00:00+00:00"
        svc._state.baseline_equity = 65.0
        svc._state.status = "TRACKING"

        await svc._evaluate()
        assert svc._state.status == "HOLD"


class TestNoAutoScale:

    def test_auto_promote_is_false(self) -> None:
        """Capital ladder must NEVER auto-change capital."""
        cfg = _make_config()
        assert cfg["capital_ladder"]["auto_promote"] is False

    def test_auto_demote_is_false(self) -> None:
        cfg = _make_config()
        assert cfg["capital_ladder"]["auto_demote"] is False

    @pytest.mark.asyncio
    async def test_ready_to_scale_does_not_change_level(self) -> None:
        """Even when READY_TO_SCALE, the service must NOT auto-promote."""
        now = datetime.now(timezone.utc)
        trades = [
            FakeTrade(realized_pnl=0.05, closed_at=now.isoformat())
            for _ in range(35)
        ]
        svc = CapitalLadderService(
            config=_make_config(),
            performance_monitor=_fake_perf_monitor(trades),
            whatsapp=_fake_whatsapp(),
        )
        svc._state.started_at = "2026-01-01T00:00:00+00:00"
        svc._state.baseline_equity = 65.0
        svc._state.current_level = 0

        await svc._evaluate()
        # Must stay at level 0 — no auto-promotion
        assert svc._state.current_level == 0


class TestNotifications:

    @pytest.mark.asyncio
    async def test_dedup_prevents_duplicate_sends(self) -> None:
        wa = _fake_whatsapp()
        svc = CapitalLadderService(
            config=_make_config(),
            performance_monitor=_fake_perf_monitor([]),
            whatsapp=wa,
        )
        svc._state.started_at = datetime.now(timezone.utc).isoformat()
        svc._state.baseline_equity = 65.0

        await svc._evaluate()
        first_count = wa._send_message.call_count

        await svc._evaluate()
        # Same metrics → hash unchanged → no new send
        assert wa._send_message.call_count == first_count


class TestPersistence:

    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        state_file = tmp_path / "capital_ladder.json"

        with patch("crypto_bot.services.capital_ladder.STATE_FILE", state_file), \
             patch("crypto_bot.services.capital_ladder.DATA_DIR", tmp_path):

            svc = CapitalLadderService(config=_make_config())
            svc._state.current_level = 1
            svc._state.level_label = "step_250"
            svc._state.started_at = "2026-03-01T00:00:00+00:00"
            svc._state.status = "HOLD"
            svc._save_state()

            assert state_file.exists()

            svc2 = CapitalLadderService(config=_make_config())
            svc2._load_state()
            assert svc2._state.current_level == 1
            assert svc2._state.level_label == "step_250"
            assert svc2._state.status == "HOLD"


class TestMetricReset:

    def test_promote_resets_state(self) -> None:
        svc = CapitalLadderService(
            config=_make_config(),
            performance_monitor=_fake_perf_monitor([]),
        )
        svc._state.current_level = 0
        svc._state.status = "READY_TO_SCALE"
        svc._state.started_at = "2026-01-01T00:00:00+00:00"

        svc._promote_to_level(1)

        assert svc._state.current_level == 1
        assert svc._state.level_label == "step_250"
        assert svc._state.status == "TRACKING"
        assert svc._state.last_status_sent_hash == ""
        assert len(svc._state.history) == 1
        assert svc._state.history[0]["level"] == 0


class TestLadderStatusProperty:

    def test_returns_expected_keys(self) -> None:
        svc = CapitalLadderService(
            config=_make_config(),
            performance_monitor=_fake_perf_monitor([]),
        )
        svc._state.started_at = datetime.now(timezone.utc).isoformat()
        svc._state.baseline_equity = 65.0

        status = svc.ladder_status
        assert "ladder_current_level" in status
        assert "ladder_status" in status
        assert "ladder_blockers" in status
        assert "suggested_next_level" in status
        assert "metrics" in status
