"""Tests for ib_bot.scanner.scanner_service — integration tests with mocks."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from ib_bot.core.enums import Topic
from ib_bot.flag_trader.model_router import RouteDecision
from ib_bot.scanner.scanner_service import ScannerService
from ib_bot.scanner.signals import ScanResult


def _passthrough_rank(results, max_candidates=20):
    """Rank that returns all results (including score=0) for testing."""
    return sorted(results, key=lambda r: r.score, reverse=True)[:max_candidates]


_RANK_PATCH = patch(
    "ib_bot.scanner.scanner_service.rank_candidates",
    side_effect=_passthrough_rank,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ohlcv_df(n: int = 60, base: float = 100.0) -> pd.DataFrame:
    """Generate a synthetic OHLCV DataFrame."""
    rng = np.random.default_rng(42)
    close = np.cumsum(rng.normal(0, 1, n)) + base
    dates = pd.bdate_range(end=pd.Timestamp.now(), periods=n)
    return pd.DataFrame(
        {
            "Open": close * 0.999,
            "High": close * 1.01,
            "Low": close * 0.99,
            "Close": close,
            "Volume": rng.integers(100_000, 10_000_000, n).astype(float),
        },
        index=dates,
    )


class FakeDataFetcher:
    """Mock data fetcher that returns synthetic data for any symbol."""

    def __init__(self, symbols: list[str] | None = None) -> None:
        self._symbols = symbols or []

    async def fetch_universe(
        self, symbols: list[str], days: int = 60, max_concurrent: int = 8
    ) -> dict[str, pd.DataFrame]:
        result = {}
        for sym in symbols:
            result[sym] = _make_ohlcv_df()
        return result


class FakeModelRouter:
    """Mock model router that returns canned decisions."""

    def __init__(self, decisions: list[RouteDecision] | None = None) -> None:
        self.decisions = decisions or []
        self.call_count = 0
        self.last_candidates: list[dict] = []

    async def route_and_decide(
        self,
        candidates: list[dict[str, Any]],
        candle_cache: dict[str, list[dict[str, float]]],
        portfolio: dict[str, float] | None = None,
    ) -> list[RouteDecision]:
        self.call_count += 1
        self.last_candidates = candidates
        return self.decisions


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestScannerServiceInit:
    def test_default_config(self) -> None:
        service = ScannerService()
        assert service.name == "scanner_service"
        assert service.universe_classes == ["stocks"]
        assert service.max_candidates == 20
        assert service.max_per_sector == 2
        assert service.max_total == 5

    def test_custom_config(self) -> None:
        config = {
            "universe": ["stocks", "etf"],
            "max_candidates": 10,
            "max_per_sector": 3,
            "max_total": 8,
            "confidence_threshold": 0.7,
        }
        service = ScannerService(config=config)
        assert service.universe_classes == ["stocks", "etf"]
        assert service.max_candidates == 10
        assert service.max_per_sector == 3
        assert service.max_total == 8
        assert service.confidence_threshold == 0.7


class TestScannerServiceLifecycle:
    @pytest.mark.asyncio
    async def test_start_stop(self) -> None:
        service = ScannerService()
        await service.start()
        assert service.is_running
        await service.stop()
        assert not service.is_running


class TestScannerServiceEodScan:
    @pytest.mark.asyncio
    async def test_eod_scan_without_model_router(self, tmp_path: Path) -> None:
        """Without a model router, scan still completes but returns no decisions."""
        fetcher = FakeDataFetcher()
        config = {
            "universe": ["stocks"],
            "max_candidates": 5,
            "report_dir": str(tmp_path),
        }

        # Patch get_universe to return a small list
        with patch(
            "ib_bot.scanner.scanner_service.get_universe",
            return_value=["AAPL", "MSFT", "JPM", "UNH", "XOM"],
        ), _RANK_PATCH:
            service = ScannerService(
                data_fetcher=fetcher,
                config=config,
            )
            result = await service.run_eod_scan()

        assert result == []  # No model router -> no decisions
        assert len(service.last_scan_results) > 0

        # Check report was saved
        reports = list(tmp_path.glob("scan_report_*.json"))
        assert len(reports) == 1
        report_data = json.loads(reports[0].read_text())
        assert report_data["total_scanned"] == 5

    @pytest.mark.asyncio
    async def test_eod_scan_with_model_router(self, tmp_path: Path) -> None:
        """With model router, decisions are returned and filtered."""
        decisions = [
            RouteDecision(
                symbol="AAPL",
                asset_class="equity",
                action=2,
                action_name="BUY",
                confidence=0.9,
                tp_pct=3.0,
                sl_pct=1.5,
            ),
            RouteDecision(
                symbol="JPM",
                asset_class="equity",
                action=2,
                action_name="BUY",
                confidence=0.8,
                tp_pct=2.5,
                sl_pct=1.0,
            ),
        ]

        fetcher = FakeDataFetcher()
        router = FakeModelRouter(decisions=decisions)
        config = {
            "universe": ["stocks"],
            "max_candidates": 5,
            "max_total": 5,
            "report_dir": str(tmp_path),
        }

        with patch(
            "ib_bot.scanner.scanner_service.get_universe",
            return_value=["AAPL", "MSFT", "JPM", "UNH", "XOM"],
        ), _RANK_PATCH:
            service = ScannerService(
                data_fetcher=fetcher,
                model_router=router,
                config=config,
            )
            result = await service.run_eod_scan()

        assert len(result) == 2
        assert router.call_count == 1
        assert len(router.last_candidates) > 0

    @pytest.mark.asyncio
    async def test_eod_scan_publishes_to_bus(self, tmp_path: Path) -> None:
        """Decisions should be published to Topic.LLM_SIGNAL."""
        decisions = [
            RouteDecision(
                symbol="AAPL",
                asset_class="equity",
                action=2,
                action_name="BUY",
                confidence=0.9,
                tp_pct=3.0,
                sl_pct=1.5,
            ),
        ]

        bus = AsyncMock()
        bus.publish = AsyncMock()
        fetcher = FakeDataFetcher()
        router = FakeModelRouter(decisions=decisions)
        config = {
            "universe": ["stocks"],
            "max_candidates": 5,
            "report_dir": str(tmp_path),
        }

        with patch(
            "ib_bot.scanner.scanner_service.get_universe",
            return_value=["AAPL"],
        ), _RANK_PATCH:
            service = ScannerService(
                bus=bus,
                data_fetcher=fetcher,
                model_router=router,
                config=config,
            )
            await service.run_eod_scan()

        # publish is called via BaseService.publish -> bus.publish
        assert bus.publish.call_count >= 1

    @pytest.mark.asyncio
    async def test_eod_scan_correlation_filter_applied(self, tmp_path: Path) -> None:
        """Correlation filter should limit same-sector positions."""
        # All IT stocks
        decisions = [
            RouteDecision(
                symbol="AAPL", asset_class="equity", action=2,
                action_name="BUY", confidence=0.9, tp_pct=3.0, sl_pct=1.5,
            ),
            RouteDecision(
                symbol="MSFT", asset_class="equity", action=2,
                action_name="BUY", confidence=0.85, tp_pct=3.0, sl_pct=1.5,
            ),
            RouteDecision(
                symbol="NVDA", asset_class="equity", action=2,
                action_name="BUY", confidence=0.8, tp_pct=3.0, sl_pct=1.5,
            ),
        ]

        fetcher = FakeDataFetcher()
        router = FakeModelRouter(decisions=decisions)
        config = {
            "universe": ["stocks"],
            "max_candidates": 5,
            "max_per_sector": 2,  # Only 2 IT stocks allowed
            "max_total": 10,
            "report_dir": str(tmp_path),
        }

        with patch(
            "ib_bot.scanner.scanner_service.get_universe",
            return_value=["AAPL", "MSFT", "NVDA"],
        ), _RANK_PATCH:
            service = ScannerService(
                data_fetcher=fetcher,
                model_router=router,
                config=config,
            )
            result = await service.run_eod_scan()

        # Should be limited to 2 IT stocks
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_report_content(self, tmp_path: Path) -> None:
        """Report JSON should contain expected fields."""
        fetcher = FakeDataFetcher()
        config = {
            "universe": ["stocks"],
            "max_candidates": 3,
            "report_dir": str(tmp_path),
        }

        with patch(
            "ib_bot.scanner.scanner_service.get_universe",
            return_value=["AAPL", "MSFT"],
        ), _RANK_PATCH:
            service = ScannerService(
                data_fetcher=fetcher,
                config=config,
            )
            await service.run_eod_scan()

        reports = list(tmp_path.glob("scan_report_*.json"))
        assert len(reports) == 1

        data = json.loads(reports[0].read_text())
        assert "date" in data
        assert "elapsed_seconds" in data
        assert "total_scanned" in data
        assert "ranked" in data
        assert isinstance(data["ranked"], list)
