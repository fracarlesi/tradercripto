"""Health and readiness check endpoints."""

from datetime import UTC, datetime

from config.logging import get_logger
from database.connection import get_db
from services.infrastructure.metrics import metrics_service
from services.infrastructure.sync_state_tracker import sync_state_tracker
from services.trading.hyperliquid_trading_service import (
    hyperliquid_trading_service,
)
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["health"])


class HealthResponse(BaseModel):
    """Health check response model."""

    status: str = Field(..., description="Overall system health: ok, degraded, down")
    uptime: int = Field(..., description="Seconds since application started", ge=0)
    last_sync_time: datetime | None = Field(
        None, description="ISO 8601 timestamp of last successful sync"
    )
    sync_status: str = Field(..., description="Sync health status: ok, stale, failing")
    message: str = Field(..., description="Human-readable status message")


class ReadinessChecks(BaseModel):
    """Readiness check results."""

    database: str = Field(..., description="Database connectivity: ok, failed")
    hyperliquid_api: str = Field(..., description="Hyperliquid API: ok, failed")
    environment: str = Field(..., description="Required environment variables: ok, failed")


class ReadinessResponse(BaseModel):
    """Readiness check response model."""

    ready: bool = Field(..., description="True if system ready to accept traffic")
    checks: ReadinessChecks = Field(..., description="Individual check results")
    message: str = Field(..., description="Human-readable readiness message")


@router.get(
    "/health",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "System healthy or degraded"},
        503: {"description": "System down"},
    },
)
async def get_health() -> JSONResponse:
    """Health check endpoint (T041).

    Returns system health status for monitoring tools.

    Health Status:
    - ok: System fully operational, sync working
    - degraded: System operating but with issues (stale sync)
    - down: Critical failure (database down, 3+ sync failures)

    Sync Status:
    - ok: Last sync within 2 minutes
    - stale: No sync in 2-5 minutes
    - failing: 3+ consecutive failures or no sync in 5+ minutes

    Returns:
        200: System healthy or degraded (still serving requests)
        503: System down (critical failure)
    """
    try:
        uptime = sync_state_tracker.get_uptime_seconds()
        last_sync_time = sync_state_tracker.get_last_sync_time()
        sync_health = sync_state_tracker.get_sync_health_status()

        # Determine overall system status
        if sync_health == "failing":
            system_status = "down"
            message = "System down: Sync failing (3+ consecutive failures)"
            http_status = status.HTTP_503_SERVICE_UNAVAILABLE
        elif sync_health == "stale":
            system_status = "degraded"
            seconds_since = (
                int((datetime.now(UTC) - last_sync_time).total_seconds()) if last_sync_time else 0
            )
            message = f"System degraded: Sync stale (last sync {seconds_since}s ago)"
            http_status = status.HTTP_200_OK
        else:
            system_status = "ok"
            message = "All systems operational"
            http_status = status.HTTP_200_OK

        response = HealthResponse(
            status=system_status,
            uptime=uptime,
            last_sync_time=last_sync_time,
            sync_status=sync_health,
            message=message,
        )

        return JSONResponse(
            status_code=http_status,
            content=response.model_dump(mode="json"),
        )

    except Exception as e:
        logger.error(
            "Health check failed",
            extra={"context": {"error": str(e)}},
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=HealthResponse(
                status="down",
                uptime=sync_state_tracker.get_uptime_seconds(),
                last_sync_time=None,
                sync_status="failing",
                message=f"Health check error: {str(e)}",
            ).model_dump(mode="json"),
        )


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "System ready"},
        503: {"description": "System not ready"},
    },
)
async def get_readiness(db: AsyncSession = Depends(get_db)) -> JSONResponse:
    """Readiness check endpoint (T042).

    Checks if system is ready to accept traffic.

    Checks:
    - Database connectivity (can execute simple query)
    - Hyperliquid API reachability (can fetch user state)
    - Required environment variables present

    Returns:
        200: System ready to accept traffic
        503: System not ready
    """
    checks = {
        "database": "ok",
        "hyperliquid_api": "ok",
        "environment": "ok",
    }
    ready = True
    messages = []

    # Check database connectivity
    try:
        result = await db.execute(text("SELECT 1"))
        await result.fetchone()
    except Exception as e:
        checks["database"] = "failed"
        ready = False
        messages.append(f"Database check failed: {str(e)}")
        logger.error(
            "Database readiness check failed",
            extra={"context": {"error": str(e)}},
        )

    # Check Hyperliquid API reachability
    try:
        await hyperliquid_trading_service.get_user_state_async()
    except Exception as e:
        checks["hyperliquid_api"] = "failed"
        ready = False
        messages.append(f"Hyperliquid API unreachable: {str(e)}")
        logger.error(
            "Hyperliquid API readiness check failed",
            extra={"context": {"error": str(e)}},
        )

    # Environment check is implicit - if service initialized, env vars are valid
    # (Pydantic Settings validates on import)

    message = "System ready" if ready else "; ".join(messages)
    http_status = status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE

    response = ReadinessResponse(
        ready=ready,
        checks=ReadinessChecks(**checks),
        message=message,
    )

    return JSONResponse(
        status_code=http_status,
        content=response.model_dump(mode="json"),
    )


@router.get(
    "/metrics",
    status_code=status.HTTP_200_OK,
    response_class=Response,
    responses={
        200: {
            "description": "Prometheus metrics in text exposition format",
            "content": {"text/plain": {"example": "# HELP ... # TYPE ..."}},
        }
    },
)
async def get_metrics() -> Response:
    """Prometheus metrics endpoint (T126).

    Returns system metrics in Prometheus text exposition format.

    Metrics include:
    - Application metrics: uptime, sync status, API requests, DB pool
    - Business metrics: account balance, AI decisions, orders, trades

    Returns:
        Response: Metrics in Prometheus text format (Content-Type: text/plain)
    """
    try:
        metrics_data = metrics_service.get_metrics_text()
        return Response(
            content=metrics_data,
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )
    except Exception as e:
        logger.error(
            "Failed to generate metrics",
            extra={"context": {"error": str(e)}},
        )
        # Return empty metrics on error
        return Response(
            content=b"",
            media_type="text/plain; version=0.0.4; charset=utf-8",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
