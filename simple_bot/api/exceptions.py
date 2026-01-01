"""
Hyperliquid API Exception Hierarchy
===================================

Custom exceptions for the Hyperliquid API wrapper.
"""

from typing import Any, Optional


class HyperliquidError(Exception):
    """Base exception for all Hyperliquid API errors."""

    def __init__(
        self,
        message: str,
        code: Optional[str] = None,
        details: Optional[dict[str, Any]] = None
    ):
        self.message = message
        self.code = code
        self.details = details or {}
        super().__init__(self.message)

    def __str__(self) -> str:
        parts = [self.message]
        if self.code:
            parts.append(f"[{self.code}]")
        if self.details:
            parts.append(f"details={self.details}")
        return " ".join(parts)


class RateLimitError(HyperliquidError):
    """Raised when rate limit is exceeded."""

    def __init__(
        self,
        message: str = "Rate limit exceeded",
        retry_after: Optional[float] = None
    ):
        super().__init__(message, code="RATE_LIMIT")
        self.retry_after = retry_after


class ConnectionError(HyperliquidError):
    """Raised when connection to Hyperliquid fails."""

    def __init__(self, message: str = "Connection failed"):
        super().__init__(message, code="CONNECTION_ERROR")


class AuthenticationError(HyperliquidError):
    """Raised when authentication fails."""

    def __init__(self, message: str = "Authentication failed"):
        super().__init__(message, code="AUTH_ERROR")


class OrderError(HyperliquidError):
    """Base exception for order-related errors."""

    def __init__(
        self,
        message: str,
        order_id: Optional[int] = None,
        symbol: Optional[str] = None
    ):
        super().__init__(message, code="ORDER_ERROR")
        self.order_id = order_id
        self.symbol = symbol


class OrderRejectedError(OrderError):
    """Raised when an order is rejected."""

    def __init__(
        self,
        message: str = "Order rejected",
        reason: Optional[str] = None,
        **kwargs
    ):
        super().__init__(message, **kwargs)
        self.code = "ORDER_REJECTED"
        self.reason = reason


class InsufficientMarginError(OrderError):
    """Raised when there's insufficient margin for an order."""

    def __init__(
        self,
        message: str = "Insufficient margin",
        required_margin: Optional[float] = None,
        available_margin: Optional[float] = None,
        **kwargs
    ):
        super().__init__(message, **kwargs)
        self.code = "INSUFFICIENT_MARGIN"
        self.required_margin = required_margin
        self.available_margin = available_margin


class InvalidOrderError(OrderError):
    """Raised when order parameters are invalid."""

    def __init__(self, message: str = "Invalid order parameters", **kwargs):
        super().__init__(message, **kwargs)
        self.code = "INVALID_ORDER"


class SymbolNotFoundError(HyperliquidError):
    """Raised when a symbol is not found."""

    def __init__(self, symbol: str):
        super().__init__(f"Symbol not found: {symbol}", code="SYMBOL_NOT_FOUND")
        self.symbol = symbol


class APIResponseError(HyperliquidError):
    """Raised when API returns an unexpected response."""

    def __init__(
        self,
        message: str = "Unexpected API response",
        response: Optional[dict] = None
    ):
        super().__init__(message, code="API_RESPONSE_ERROR")
        self.response = response


class TimeoutError(HyperliquidError):
    """Raised when an API request times out."""

    def __init__(self, message: str = "Request timed out"):
        super().__init__(message, code="TIMEOUT")
