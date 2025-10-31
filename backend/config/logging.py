"""Structured logging configuration for Bitcoin Trading System (Enhanced T142)."""

import json
import logging
import sys
import traceback
from datetime import datetime
from typing import Any

from config.settings import settings


class StructuredFormatter(logging.Formatter):
    """JSON formatter for structured logging with enhanced fields (T142)."""

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON with enhanced structured fields.

        Args:
            record: Log record to format

        Returns:
            JSON-formatted log string with: timestamp, level, service, message,
            request_id, user_id, account_id, operation, duration_ms, error_code,
            stack_trace (for exceptions)
        """
        log_data: dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "service": record.name,
            "message": record.getMessage(),
        }

        # Add request_id if present (T142)
        if hasattr(record, "request_id"):
            log_data["request_id"] = record.request_id

        # Add user_id if present (T142)
        if hasattr(record, "user_id"):
            log_data["user_id"] = record.user_id

        # Add account_id if present (T142)
        if hasattr(record, "account_id"):
            log_data["account_id"] = record.account_id

        # Add operation if present (T142)
        if hasattr(record, "operation"):
            log_data["operation"] = record.operation

        # Add duration_ms if present (T142)
        if hasattr(record, "duration_ms"):
            log_data["duration_ms"] = record.duration_ms

        # Add error_code if present (T142)
        if hasattr(record, "error_code"):
            log_data["error_code"] = record.error_code

        # Add context if present
        if hasattr(record, "context"):
            log_data["context"] = record.context

        # Add exception info with full stack trace if present (T142)
        if record.exc_info:
            exc_type, exc_value, exc_tb = record.exc_info
            log_data["exception"] = {
                "type": exc_type.__name__ if exc_type else "Unknown",
                "message": str(exc_value),
                "traceback": self.formatException(record.exc_info),
                "stack_trace": traceback.format_exception(exc_type, exc_value, exc_tb),
            }

        return json.dumps(log_data)


def setup_logging() -> None:
    """Configure application logging with structured format."""
    # Set log level based on environment
    log_level = logging.DEBUG if settings.debug else logging.INFO

    # Create handler
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(log_level)

    # Use structured formatter for production, simple for development
    if settings.environment == "production":
        formatter = StructuredFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    handler.setFormatter(formatter)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()
    root_logger.addHandler(handler)

    # Suppress noisy loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.sql_debug else logging.WARNING
    )


# Request ID context variable (set by middleware)
_request_id: str | None = None


def set_request_id(request_id: str) -> None:
    """Set request ID for current context."""
    global _request_id
    _request_id = request_id


def get_request_id() -> str | None:
    """Get request ID for current context."""
    return _request_id


def get_logger(name: str) -> logging.Logger:
    """Get logger with automatic request_id injection.

    Args:
        name: Logger name (usually __name__)

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)

    # Wrap logger methods to inject request_id
    original_log = logger._log

    def _log_with_request_id(
        level: int,
        msg: str,
        args: tuple,
        exc_info: Any = None,
        extra: dict | None = None,
        **kwargs: Any,
    ) -> None:
        if extra is None:
            extra = {}
        if _request_id:
            extra["request_id"] = _request_id
        original_log(level, msg, args, exc_info=exc_info, extra=extra, **kwargs)

    logger._log = _log_with_request_id  # type: ignore

    return logger


# Enhanced logging utilities (T142)


def log_operation(
    logger: logging.Logger,
    level: int,
    operation: str,
    message: str,
    user_id: int | None = None,
    account_id: int | None = None,
    duration_ms: float | None = None,
    error_code: str | None = None,
    **context: Any,
) -> None:
    """Log operation with structured fields (T142).

    Args:
        logger: Logger instance
        level: Log level (logging.INFO, logging.ERROR, etc.)
        operation: Operation name (e.g., "sync_account", "place_order")
        message: Log message
        user_id: Optional user ID
        account_id: Optional account ID
        duration_ms: Optional operation duration in milliseconds
        error_code: Optional error code for failures
        **context: Additional context data
    """
    extra: dict[str, Any] = {"operation": operation}

    if user_id is not None:
        extra["user_id"] = user_id
    if account_id is not None:
        extra["account_id"] = account_id
    if duration_ms is not None:
        extra["duration_ms"] = duration_ms
    if error_code is not None:
        extra["error_code"] = error_code
    if context:
        extra["context"] = context

    logger.log(level, message, extra=extra)


def log_operation_start(
    logger: logging.Logger,
    operation: str,
    user_id: int | None = None,
    account_id: int | None = None,
    **context: Any,
) -> float:
    """Log operation start and return start timestamp (T142).

    Args:
        logger: Logger instance
        operation: Operation name
        user_id: Optional user ID
        account_id: Optional account ID
        **context: Additional context data

    Returns:
        Start timestamp in milliseconds for duration calculation
    """
    import time

    start_ms = time.time() * 1000

    log_operation(
        logger,
        logging.INFO,
        operation,
        f"Operation started: {operation}",
        user_id=user_id,
        account_id=account_id,
        **context,
    )

    return start_ms


def log_operation_end(
    logger: logging.Logger,
    operation: str,
    start_ms: float,
    success: bool = True,
    user_id: int | None = None,
    account_id: int | None = None,
    error_code: str | None = None,
    **context: Any,
) -> None:
    """Log operation end with duration (T142).

    Args:
        logger: Logger instance
        operation: Operation name
        start_ms: Start timestamp from log_operation_start()
        success: Whether operation succeeded
        user_id: Optional user ID
        account_id: Optional account ID
        error_code: Optional error code for failures
        **context: Additional context data
    """
    import time

    end_ms = time.time() * 1000
    duration_ms = end_ms - start_ms

    level = logging.INFO if success else logging.ERROR
    status = "completed" if success else "failed"

    log_operation(
        logger,
        level,
        operation,
        f"Operation {status}: {operation}",
        user_id=user_id,
        account_id=account_id,
        duration_ms=duration_ms,
        error_code=error_code,
        **context,
    )
