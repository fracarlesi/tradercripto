import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

# Load environment variables from .env file
load_dotenv()

# Setup logging BEFORE any other imports (critical for seeing startup logs)
from config.logging import setup_logging
setup_logging()

# from config.settings import DEFAULT_TRADING_CONFIGS  # TODO: Restore after settings refactor
from database.connection import SessionLocal, sync_engine
from database.models import Account, Base, User  # TradingConfig temporarily disabled


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan context manager for startup and shutdown events.
    This replaces the deprecated @app.on_event("startup") and @app.on_event("shutdown") decorators.
    """
    # STARTUP: Code before yield runs on application startup
    # Create tables
    Base.metadata.create_all(bind=sync_engine)
    # Seed trading configs if empty (DISABLED - TODO: Restore after settings refactor)
    db: Session = SessionLocal()
    try:
        # Temporarily disabled trading config seeding
        pass
        # if db.query(TradingConfig).count() == 0:
        #     for cfg in DEFAULT_TRADING_CONFIGS.values():
        #         db.add(
        #             TradingConfig(
        #                 version="v1",
        #                 market=cfg.market,
        #                 min_commission=cfg.min_commission,
        #                 commission_rate=cfg.commission_rate,
        #                 exchange_rate=cfg.exchange_rate,
        #                 min_order_quantity=cfg.min_order_quantity,
        #                 lot_size=cfg.lot_size,
        #             )
        #         )
        #     db.commit()
        # Ensure only default user and its account exist
        # Delete all non-default users and their accounts
        from database.models import Order, Position, Trade

        non_default_users = db.query(User).filter(User.username != "default").all()
        for user in non_default_users:
            # Get user's account IDs
            account_ids = [
                acc.id for acc in db.query(Account).filter(Account.user_id == user.id).all()
            ]

            if account_ids:
                # Delete trades, orders, positions associated with these accounts
                db.query(Trade).filter(Trade.account_id.in_(account_ids)).delete(
                    synchronize_session=False
                )
                db.query(Order).filter(Order.account_id.in_(account_ids)).delete(
                    synchronize_session=False
                )
                db.query(Position).filter(Position.account_id.in_(account_ids)).delete(
                    synchronize_session=False
                )

                # Now delete the accounts
                db.query(Account).filter(Account.user_id == user.id).delete(
                    synchronize_session=False
                )

            # Delete the user
            db.delete(user)

        db.commit()

        # Ensure default user exists
        default_user = db.query(User).filter(User.username == "default").first()
        if not default_user:
            default_user = User(
                username="default", email="default@example.com", password_hash=None, is_active=True
            )
            db.add(default_user)
            db.commit()
            db.refresh(default_user)

        # Ensure default user has at least one account
        default_accounts = db.query(Account).filter(Account.user_id == default_user.id).all()
        if len(default_accounts) == 0:
            # Create default account
            default_account = Account(
                user_id=default_user.id,
                version="v1",
                name="DeepSeek",
                account_type="AI",
                model="deepseek-chat",
                base_url="https://api.deepseek.com",
                api_key="default-key-please-update-in-settings",
                is_active=True,
                # Note: Balance is fetched from Hyperliquid API, not stored in database
            )
            db.add(default_account)
            db.commit()
    finally:
        db.close()

    # Initialize all services (scheduler, market data tasks, auto trading, etc.)
    from services.startup import initialize_services

    initialize_services()

    # Initialize metrics service (T125-T128)
    from services.infrastructure.metrics import metrics_service

    metrics_service.start()

    # Initialize scheduler for periodic sync (T048)
    from config.settings import settings as async_settings
    from services.infrastructure.scheduler import scheduler_service
    from services.trading.sync_jobs import periodic_sync_job

    try:
        scheduler_service.start()
        scheduler_service.add_sync_job(
            job_func=periodic_sync_job,
            interval_seconds=async_settings.sync_interval_seconds,
            job_id="hyperliquid_sync",
        )

        # Add daily AI usage reset job at midnight (T101)
        from services.infrastructure.usage_tracker import reset_ai_usage_daily

        scheduler_service.add_cron_job(
            job_func=reset_ai_usage_daily, hour=0, minute=0, job_id="ai_usage_daily_reset"
        )

        # Add stop-loss check job (every 30 seconds) - FIX 3
        from services.auto_trader import check_stop_loss_async, check_take_profit_async

        scheduler_service.add_sync_job(
            job_func=check_stop_loss_async,
            interval_seconds=30,
            job_id="stop_loss_check"
        )

        # Add take-profit check job (every 30 seconds) - lock in profits at +5%
        scheduler_service.add_sync_job(
            job_func=check_take_profit_async,
            interval_seconds=30,
            job_id="take_profit_check"
        )

    except Exception as e:
        print(f"Warning: Failed to start scheduler: {e}")

    # Application is ready - yield control to FastAPI
    yield

    # SHUTDOWN: Code after yield runs on application shutdown
    try:
        scheduler_service.stop()
    except Exception as e:
        print(f"Warning: Failed to stop scheduler: {e}")

    from services.startup import shutdown_services

    shutdown_services()


# Create FastAPI app with lifespan context manager
app = FastAPI(title="Crypto Paper Trading API", lifespan=lifespan)


# Global exception handlers (T025, T057)
import logging

from middleware.request_id import get_request_id
from services.exceptions import (
    AIException,
    APIException,
    CircuitBreakerOpenException,
    DatabaseException,
    PoolExhaustedException,
    RateLimitException,
    SyncException,
    TradingException,
)
from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


@app.exception_handler(PoolExhaustedException)
async def pool_exhausted_handler(request: Request, exc: PoolExhaustedException):
    """Handle database connection pool exhaustion with 503 + Retry-After header."""
    request_id = get_request_id(request)
    logger.error(
        "Database pool exhausted",
        extra={"request_id": request_id, "error": str(exc), "path": request.url.path},
    )
    return JSONResponse(
        status_code=503,
        content={
            "error": "Service Unavailable",
            "detail": "Database connection pool exhausted. Please try again.",
            "request_id": request_id,
        },
        headers={"Retry-After": "10"},  # Retry after 10 seconds
    )


@app.exception_handler(CircuitBreakerOpenException)
async def circuit_breaker_handler(request: Request, exc: CircuitBreakerOpenException):
    """Handle circuit breaker open state with 503 error."""
    request_id = get_request_id(request)
    logger.warning(
        "Circuit breaker open",
        extra={"request_id": request_id, "error": str(exc), "path": request.url.path},
    )
    return JSONResponse(
        status_code=503,
        content={
            "error": "Service Unavailable",
            "detail": "Service temporarily unavailable due to recent failures. Please try again later.",
            "request_id": request_id,
        },
        headers={"Retry-After": "60"},  # Circuit breaker opens for 60s
    )


@app.exception_handler(RateLimitException)
async def rate_limit_handler(request: Request, exc: RateLimitException):
    """Handle rate limit exceeded errors with 429 status."""
    request_id = get_request_id(request)
    logger.warning(
        "Rate limit exceeded",
        extra={"request_id": request_id, "error": str(exc), "path": request.url.path},
    )
    return JSONResponse(
        status_code=429,
        content={
            "error": "Too Many Requests",
            "detail": str(exc) or "Rate limit exceeded. Please slow down your requests.",
            "request_id": request_id,
        },
        headers={"Retry-After": "60"},
    )


@app.exception_handler(SyncException)
async def sync_exception_handler(request: Request, exc: SyncException):
    """Handle Hyperliquid sync failures with 503 error."""
    request_id = get_request_id(request)
    logger.error(
        "Sync operation failed",
        extra={"request_id": request_id, "error": str(exc), "path": request.url.path},
    )
    return JSONResponse(
        status_code=503,
        content={
            "error": "Sync Failed",
            "detail": str(exc) or "Failed to synchronize with Hyperliquid. Data may be stale.",
            "request_id": request_id,
        },
    )


@app.exception_handler(AIException)
async def ai_exception_handler(request: Request, exc: AIException):
    """Handle AI decision service errors with 500 error."""
    request_id = get_request_id(request)
    logger.error(
        "AI service error",
        extra={"request_id": request_id, "error": str(exc), "path": request.url.path},
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "AI Service Error",
            "detail": str(exc) or "AI decision service encountered an error.",
            "request_id": request_id,
        },
    )


@app.exception_handler(DatabaseException)
async def database_exception_handler(request: Request, exc: DatabaseException):
    """Handle database operation errors with 500 error."""
    request_id = get_request_id(request)
    logger.error(
        "Database operation failed",
        extra={
            "request_id": request_id,
            "error": str(exc),
            "path": request.url.path,
            "exception_type": type(exc).__name__,
        },
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "Database Error",
            "detail": "A database error occurred. Please try again.",
            "request_id": request_id,
        },
    )


@app.exception_handler(APIException)
async def api_exception_handler(request: Request, exc: APIException):
    """Handle external API errors with 502 Bad Gateway."""
    request_id = get_request_id(request)
    logger.error(
        "External API error",
        extra={"request_id": request_id, "error": str(exc), "path": request.url.path},
    )
    return JSONResponse(
        status_code=502,
        content={
            "error": "External API Error",
            "detail": str(exc) or "External API request failed.",
            "request_id": request_id,
        },
    )


@app.exception_handler(TradingException)
async def trading_exception_handler(request: Request, exc: TradingException):
    """Handle generic trading exceptions with 500 error."""
    request_id = get_request_id(request)
    logger.error(
        "Trading system error",
        extra={
            "request_id": request_id,
            "error": str(exc),
            "path": request.url.path,
            "exception_type": type(exc).__name__,
        },
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "Trading System Error",
            "detail": str(exc) or "An error occurred in the trading system.",
            "request_id": request_id,
        },
    )


# Health check endpoint
@app.get("/api/health")
async def health_check():
    return {"status": "healthy", "message": "Trading API is running"}


# Configure CORS from environment variable
from config.settings import settings as async_settings_cors

cors_origins_list = (
    ["*"]
    if async_settings_cors.cors_origins == "*"
    else [origin.strip() for origin in async_settings_cors.cors_origins.split(",")]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files for frontend
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    assets_dir = os.path.join(static_dir, "assets")
    if os.path.exists(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")


# API routes (old synchronous)
from api.account_routes import router as account_router
from api.config_routes import router as config_router
from api.crypto_routes import router as crypto_router
from api.market_data_routes import router as market_data_router
from api.order_routes import router as order_router
from api.ranking_routes import router as ranking_router

# API routes (new async)
try:
    from api.accounts_async import router as accounts_async_router
    from api.ai_routes import router as ai_router  # AI usage tracking (T100)
    from api.health_routes import router as health_router
    from api.market_data_async import router as market_data_async_router
    from api.orders_async import router as orders_async_router
    from api.sync_routes import router as sync_router

    app.include_router(health_router)
    app.include_router(sync_router)
    app.include_router(accounts_async_router)
    app.include_router(market_data_async_router)
    app.include_router(orders_async_router)
    app.include_router(ai_router)  # AI usage tracking endpoint (T100)
except ImportError:
    pass  # New routes not available yet

app.include_router(market_data_router)
app.include_router(order_router)
app.include_router(account_router)
app.include_router(config_router)
app.include_router(ranking_router)
app.include_router(crypto_router)

# WebSocket endpoints - using async version with real-time Hyperliquid data
from api.ws_async import websocket_endpoint_async

# Primary WebSocket endpoint (async, fetches from Hyperliquid)
app.websocket("/ws")(websocket_endpoint_async)


# Serve frontend index.html for root and SPA routes
@app.get("/")
async def serve_root():
    """Serve the frontend index.html for root route"""
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    index_path = os.path.join(static_dir, "index.html")

    if os.path.exists(index_path):
        return FileResponse(index_path)
    else:
        return {"message": "Frontend not built yet"}


# Catch-all route for SPA routing (must be last)
@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    """Serve the frontend index.html for SPA routes that don't match API/static"""
    # Skip API and static routes
    if (
        full_path.startswith("api")
        or full_path.startswith("static")
        or full_path.startswith("docs")
        or full_path.startswith("openapi.json")
    ):
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Not found")

    static_dir = os.path.join(os.path.dirname(__file__), "static")
    index_path = os.path.join(static_dir, "index.html")

    if os.path.exists(index_path):
        return FileResponse(index_path)
    else:
        return {"message": "Frontend not built yet"}
