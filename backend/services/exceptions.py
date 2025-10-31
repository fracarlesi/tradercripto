"""Custom exception hierarchy for Bitcoin Trading System."""


class TradingException(Exception):
    """Base exception for all trading-related errors."""

    pass


class SyncException(TradingException):
    """Raised when Hyperliquid synchronization fails."""

    pass


class AIException(TradingException):
    """Raised when AI decision service encounters errors."""

    pass


class DatabaseException(TradingException):
    """Raised when database operations fail."""

    pass


class APIException(TradingException):
    """Raised when external API calls fail."""

    pass


class RateLimitException(APIException):
    """Raised when API rate limits are exceeded."""

    pass


class CircuitBreakerOpenException(APIException):
    """Raised when circuit breaker is open and blocking requests."""

    pass


class PoolExhaustedException(DatabaseException):
    """Raised when database connection pool is exhausted."""

    pass
