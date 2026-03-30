"""
Scanner Service — Orchestrates EOD Scan + LLM Routing + Filtering
==================================================================

End-of-day scanning service that:
1. Fetches daily data for the configured universe
2. Computes technical signals for each symbol
3. Ranks candidates by composite score
4. Routes top candidates through the LLM model router
5. Filters for sector/correlation diversification
6. Publishes actionable signals to the message bus
7. Saves daily report in JSON

Extends BaseService for lifecycle management.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ib_bot.core.enums import Topic
from ib_bot.scanner.correlation_filter import filter_correlated
from ib_bot.scanner.data_fetcher import ScannerDataFetcher
from ib_bot.scanner.ranker import rank_candidates
from ib_bot.scanner.signals import ScanResult, scan_symbol
from ib_bot.scanner.universe import STOCK_SECTORS, get_universe
from ib_bot.services.base import BaseService
from ib_bot.services.message_bus import MessageBus

logger = logging.getLogger(__name__)

# Default report directory
_REPORT_DIR = Path(__file__).resolve().parent.parent / "data" / "scanner_reports"


class ScannerService(BaseService):
    """EOD scanning service that produces LLM-filtered trade candidates.

    Workflow:
        1. fetch_universe() -> daily OHLCV for all symbols
        2. scan all symbols -> ScanResult with technical signals
        3. rank_candidates() -> top N by composite score
        4. model_router.route_and_decide() -> LLM evaluations
        5. filter_correlated() -> diversification constraints
        6. publish to Topic.LLM_SIGNAL

    Configuration (via config dict):
        - universe: list of asset classes to scan (default: ["stocks"])
        - max_candidates: number of top candidates to pass to LLM (default: 20)
        - max_per_sector: max positions per GICS sector (default: 2)
        - max_total: max total positions (default: 5)
        - confidence_threshold: minimum confidence for LLM signals (default: 0.6)
        - report_dir: directory for daily JSON reports
    """

    def __init__(
        self,
        bus: MessageBus | None = None,
        model_router: Any | None = None,
        data_fetcher: ScannerDataFetcher | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        # EOD scan runs once, not in a loop — use long interval
        super().__init__(
            name="scanner_service",
            bus=bus,
            loop_interval_seconds=3600.0,
        )
        self.model_router = model_router
        self.data_fetcher = data_fetcher or ScannerDataFetcher()

        cfg = config or {}
        self.universe_classes: list[str] = cfg.get("universe", ["stocks"])
        self.max_candidates: int = cfg.get("max_candidates", 20)
        self.max_per_sector: int = cfg.get("max_per_sector", 2)
        self.max_total: int = cfg.get("max_total", 5)
        self.confidence_threshold: float = cfg.get("confidence_threshold", 0.6)
        self.report_dir = Path(cfg.get("report_dir", str(_REPORT_DIR)))
        self.report_dir.mkdir(parents=True, exist_ok=True)

        self._last_scan_results: list[ScanResult] = []
        self._last_decisions: list[Any] = []

    async def _on_start(self) -> None:
        """Called when service starts."""
        self._logger.info(
            "ScannerService starting | universe=%s | max_candidates=%d",
            self.universe_classes, self.max_candidates,
        )

    async def _on_stop(self) -> None:
        """Called when service stops."""
        self._logger.info("ScannerService stopped")

    async def run_eod_scan(
        self,
        open_positions: list[Any] | None = None,
        portfolio: dict[str, float] | None = None,
    ) -> list[Any]:
        """Execute the full EOD scan pipeline.

        Args:
            open_positions: Currently open positions for correlation filter.
            portfolio: Current portfolio state for LLM prompt context.

        Returns:
            List of filtered trade decisions (RouteDecision objects).
        """
        self._logger.info("=== EOD SCAN STARTING ===")
        start_time = datetime.now(timezone.utc)

        # 1. Build universe
        symbols: list[str] = []
        for asset_class in self.universe_classes:
            symbols.extend(get_universe(asset_class))
        self._logger.info("Universe: %d symbols from %s", len(symbols), self.universe_classes)

        # 2. Fetch data
        data_cache = await self.data_fetcher.fetch_universe(symbols)
        self._logger.info("Fetched data for %d / %d symbols", len(data_cache), len(symbols))

        # 3. Compute signals
        scan_results: list[ScanResult] = []
        for symbol, df in data_cache.items():
            result = scan_symbol(symbol, df)
            scan_results.append(result)

        # 4. Rank candidates
        ranked = rank_candidates(scan_results, max_candidates=self.max_candidates)
        self._last_scan_results = ranked
        self._logger.info(
            "Ranked %d candidates (top score: %.1f)",
            len(ranked),
            ranked[0].score if ranked else 0.0,
        )

        # 5. Build candle cache for LLM (convert DataFrame -> list of dicts)
        candle_cache: dict[str, list[dict[str, float]]] = {}
        for result in ranked:
            df = data_cache.get(result.symbol)
            if df is not None and not df.empty:
                candle_cache[result.symbol] = [
                    {
                        "open": float(row["Open"]),
                        "high": float(row["High"]),
                        "low": float(row["Low"]),
                        "close": float(row["Close"]),
                        "volume": float(row["Volume"]),
                    }
                    for _, row in df.iterrows()
                ]

        # 6. Route through LLM model router (if available)
        decisions: list[Any] = []
        if self.model_router is not None:
            candidates_for_llm = [
                {
                    "symbol": r.symbol,
                    "sector": STOCK_SECTORS.get(r.symbol),
                    "score": r.score,
                    "trend": r.trend,
                }
                for r in ranked
            ]
            decisions = await self.model_router.route_and_decide(
                candidates=candidates_for_llm,
                candle_cache=candle_cache,
                portfolio=portfolio,
            )
            self._logger.info("LLM router returned %d actionable decisions", len(decisions))
        else:
            self._logger.warning("No model_router configured, skipping LLM evaluation")

        # 7. Apply correlation filter
        filtered = filter_correlated(
            candidates=decisions,
            open_positions=open_positions,
            max_per_sector=self.max_per_sector,
            max_total=self.max_total,
        )
        self._last_decisions = filtered

        # 8. Publish to message bus
        if self.bus and filtered:
            for decision in filtered:
                await self.publish(Topic.LLM_SIGNAL, decision)
            self._logger.info("Published %d LLM signals", len(filtered))

        # 9. Save daily report
        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
        self._save_report(
            scan_results=scan_results,
            ranked=ranked,
            decisions=decisions,
            filtered=filtered,
            elapsed_seconds=elapsed,
        )

        self._logger.info(
            "=== EOD SCAN COMPLETE === | %d scanned | %d ranked | %d LLM | %d filtered | %.1fs",
            len(scan_results), len(ranked), len(decisions), len(filtered), elapsed,
        )
        return filtered

    def _save_report(
        self,
        scan_results: list[ScanResult],
        ranked: list[ScanResult],
        decisions: list[Any],
        filtered: list[Any],
        elapsed_seconds: float,
    ) -> None:
        """Save a daily scan report as JSON."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        report_path = self.report_dir / f"scan_report_{today}.json"

        def _scan_to_dict(r: ScanResult) -> dict[str, Any]:
            return {
                "symbol": r.symbol,
                "score": r.score,
                "squeeze_fired": r.squeeze_fired,
                "ema_cross": r.ema_cross_direction,
                "rsi": r.rsi_value,
                "atr_pct": r.atr_pct,
                "adx": r.adx_value,
                "trend": r.trend,
                "volume_ratio": r.volume_ratio,
            }

        def _decision_to_dict(d: Any) -> dict[str, Any]:
            if hasattr(d, "__dict__"):
                return {
                    k: v for k, v in d.__dict__.items()
                    if not k.startswith("_")
                }
            if isinstance(d, dict):
                return d
            return {"repr": str(d)}

        report = {
            "date": today,
            "elapsed_seconds": round(elapsed_seconds, 1),
            "total_scanned": len(scan_results),
            "ranked_count": len(ranked),
            "llm_decisions_count": len(decisions),
            "filtered_count": len(filtered),
            "ranked": [_scan_to_dict(r) for r in ranked[:20]],
            "decisions": [_decision_to_dict(d) for d in decisions],
            "filtered": [_decision_to_dict(d) for d in filtered],
        }

        try:
            report_path.write_text(json.dumps(report, indent=2, default=str))
            self._logger.info("Report saved to %s", report_path)
        except Exception as e:
            self._logger.error("Failed to save report: %s", e)

    @property
    def last_scan_results(self) -> list[ScanResult]:
        """Return the last scan results (ranked candidates)."""
        return self._last_scan_results

    @property
    def last_decisions(self) -> list[Any]:
        """Return the last filtered decisions."""
        return self._last_decisions
