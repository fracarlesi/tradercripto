"""Health check HTTP server for Docker/K8s monitoring."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from aiohttp import web

from .hft_metrics import get_metrics_collector

if TYPE_CHECKING:
    from ..bot import HLQuantBot

logger = logging.getLogger(__name__)


class HealthServer:
    """
    Lightweight HTTP server for health checks and status endpoints.

    Endpoints:
    - /health: Liveness probe (is the process alive?)
    - /ready: Readiness probe (is the bot ready to trade?)
    - /status: Full status for monitoring dashboards
    - /metrics: HFT performance metrics (latency, CPU, RAM)
    - /metrics/history: Latency history for charts
    - /metrics/dry-run: Control dry-run mode
    """

    def __init__(self, bot: "HLQuantBot", port: int = 8080):
        self.bot = bot
        self.port = port
        self._app = web.Application()
        self._runner: Optional[web.AppRunner] = None
        self._start_time = datetime.now(timezone.utc)
        self._setup_routes()

    def _setup_routes(self):
        """Setup HTTP routes."""
        self._app.router.add_get("/health", self._health_handler)
        self._app.router.add_get("/ready", self._ready_handler)
        self._app.router.add_get("/status", self._status_handler)
        self._app.router.add_get("/metrics", self._metrics_handler)
        self._app.router.add_get("/metrics/history", self._metrics_history_handler)
        self._app.router.add_post("/metrics/dry-run", self._dry_run_handler)
        self._app.router.add_post("/metrics/reset", self._metrics_reset_handler)

    async def _health_handler(self, request: web.Request) -> web.Response:
        """
        Liveness probe - is the bot process alive?

        Returns 200 if the process is running.
        Docker uses this to determine if container should be restarted.
        """
        return web.json_response({
            "status": "healthy",
            "running": self.bot._running,
            "uptime_seconds": self._get_uptime_seconds(),
        })

    async def _ready_handler(self, request: web.Request) -> web.Response:
        """
        Readiness probe - is the bot ready to trade?

        Returns 200 if ready, 503 if not ready.
        """
        is_ready = (
            self.bot._running
            and self.bot.market_data is not None
            and self.bot.risk_engine is not None
            and not self.bot.risk_engine.circuit_breaker.is_triggered
        )

        reasons = []
        if not self.bot._running:
            reasons.append("bot not running")
        if self.bot.market_data is None:
            reasons.append("market data not initialized")
        if self.bot.risk_engine is None:
            reasons.append("risk engine not initialized")
        elif self.bot.risk_engine.circuit_breaker.is_triggered:
            reasons.append("circuit breaker triggered")

        status_code = 200 if is_ready else 503
        return web.json_response({
            "status": "ready" if is_ready else "not_ready",
            "reasons": reasons if reasons else None,
            "circuit_breaker_triggered": (
                self.bot.risk_engine.circuit_breaker.is_triggered
                if self.bot.risk_engine else None
            ),
        }, status=status_code)

    async def _status_handler(self, request: web.Request) -> web.Response:
        """
        Full status for monitoring dashboards.

        Returns detailed information about bot state.
        """
        # Basic status
        status = {
            "running": self.bot._running,
            "environment": "TESTNET" if self.bot.settings.is_testnet else "PRODUCTION",
            "uptime_seconds": self._get_uptime_seconds(),
            "symbols": self.bot.settings.active_symbols,
        }

        # Circuit breaker status
        if self.bot.risk_engine:
            cb = self.bot.risk_engine.circuit_breaker
            status["circuit_breaker"] = {
                "triggered": cb.is_triggered,
                "daily_pnl_pct": float(cb.daily_pnl_pct) if hasattr(cb, 'daily_pnl_pct') else None,
                "max_drawdown_pct": float(cb.max_drawdown_pct) if hasattr(cb, 'max_drawdown_pct') else None,
            }

        # Account status
        if self.bot.market_data:
            account = self.bot.market_data.get_account_state()
            if account:
                status["account"] = {
                    "equity": float(account.equity),
                    "available_balance": float(account.available_balance),
                    "total_margin_used": float(account.total_margin_used),
                    "current_leverage": float(account.current_leverage),
                    "position_count": len(account.positions),
                    "daily_pnl": float(account.daily_pnl) if account.daily_pnl else None,
                }

        # Strategies status
        status["strategies"] = []
        for strategy in self.bot.strategies:
            status["strategies"].append({
                "name": strategy.name,
                "enabled": strategy.is_enabled,
            })

        return web.json_response(status)

    async def _metrics_handler(self, request: web.Request) -> web.Response:
        """
        HFT performance metrics endpoint.

        Returns current latency, CPU, RAM statistics.
        """
        collector = get_metrics_collector()
        snapshot = collector.get_snapshot()

        return web.json_response({
            "timestamp": snapshot.timestamp.isoformat(),
            "latency": {
                "avg_loop_ms": round(snapshot.avg_loop_ms, 2),
                "p50_loop_ms": round(snapshot.p50_loop_ms, 2),
                "p95_loop_ms": round(snapshot.p95_loop_ms, 2),
                "p99_loop_ms": round(snapshot.p99_loop_ms, 2),
                "max_loop_ms": round(snapshot.max_loop_ms, 2),
                "breakdown": {
                    "strategy_ms": round(snapshot.avg_strategy_ms, 2),
                    "risk_ms": round(snapshot.avg_risk_ms, 2),
                    "execution_ms": round(snapshot.avg_execution_ms, 2),
                    "regime_ms": round(snapshot.avg_regime_ms, 2),
                }
            },
            "system": {
                "cpu_percent": round(snapshot.cpu_percent, 1),
                "memory_mb": round(snapshot.memory_mb, 1),
            },
            "counters": {
                "loop_count": snapshot.loop_count,
                "signals_generated": snapshot.signals_generated,
                "orders_executed": snapshot.orders_executed,
                "regime_detections": snapshot.regime_detections,
            },
            "dry_run": {
                "enabled": collector.is_dry_run,
                "simulated_orders": len(collector.get_dry_run_orders()),
            },
            "thresholds": {
                "test_a_target_ms": 500,
                "test_b_target_ms": 1000,
                "test_c_target_ms": 2000,
            }
        })

    async def _metrics_history_handler(self, request: web.Request) -> web.Response:
        """
        Latency history for charting.

        Query params:
        - limit: Number of samples (default 100)
        """
        limit = int(request.query.get("limit", 100))
        collector = get_metrics_collector()
        history = collector.get_latency_history(limit=limit)

        return web.json_response({
            "history": history,
            "count": len(history),
        })

    async def _dry_run_handler(self, request: web.Request) -> web.Response:
        """
        Toggle dry-run mode.

        POST body: {"enabled": true/false}
        """
        try:
            data = await request.json()
            enabled = data.get("enabled", False)

            collector = get_metrics_collector()
            if enabled:
                collector.enable_dry_run()
            else:
                collector.disable_dry_run()

            return web.json_response({
                "dry_run_enabled": collector.is_dry_run,
                "message": f"Dry-run mode {'enabled' if enabled else 'disabled'}",
            })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    async def _metrics_reset_handler(self, request: web.Request) -> web.Response:
        """
        Reset all metrics counters.

        Useful when starting a new test phase.
        """
        collector = get_metrics_collector()
        collector.reset_counters()

        return web.json_response({
            "message": "Metrics counters reset successfully",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def _get_uptime_seconds(self) -> int:
        """Get uptime in seconds."""
        return int((datetime.now(timezone.utc) - self._start_time).total_seconds())

    async def start(self):
        """Start the health server."""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await site.start()
        logger.info(f"Health server started on port {self.port}")

    async def stop(self):
        """Stop the health server."""
        if self._runner:
            await self._runner.cleanup()
            logger.info("Health server stopped")
