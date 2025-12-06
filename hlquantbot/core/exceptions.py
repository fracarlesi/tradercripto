"""Custom exceptions for HLQuantBot."""


class HLQuantBotError(Exception):
    """Base exception for all HLQuantBot errors."""
    pass


class ConfigurationError(HLQuantBotError):
    """Invalid or missing configuration."""
    pass


class InsufficientBalanceError(HLQuantBotError):
    """Not enough balance to execute trade."""
    pass


class RiskLimitExceededError(HLQuantBotError):
    """Trade would exceed risk limits."""

    def __init__(self, message: str, limit_type: str, current_value: float, limit_value: float):
        super().__init__(message)
        self.limit_type = limit_type
        self.current_value = current_value
        self.limit_value = limit_value


class ExecutionError(HLQuantBotError):
    """Error during order execution."""

    def __init__(self, message: str, order_id: str = None, response: dict = None):
        super().__init__(message)
        self.order_id = order_id
        self.response = response


class DataFeedError(HLQuantBotError):
    """Error with market data feed."""

    def __init__(self, message: str, source: str = None):
        super().__init__(message)
        self.source = source


class CircuitBreakerTriggeredError(HLQuantBotError):
    """Circuit breaker has been triggered - trading halted."""

    def __init__(self, message: str, reason: str, triggered_at: str = None):
        super().__init__(message)
        self.reason = reason
        self.triggered_at = triggered_at


class RateLimitError(HLQuantBotError):
    """API rate limit exceeded."""

    def __init__(self, message: str, retry_after: float = None):
        super().__init__(message)
        self.retry_after = retry_after


class WebSocketError(HLQuantBotError):
    """WebSocket connection error."""

    def __init__(self, message: str, reconnect_attempt: int = 0):
        super().__init__(message)
        self.reconnect_attempt = reconnect_attempt


class StrategyError(HLQuantBotError):
    """Error in strategy execution."""

    def __init__(self, message: str, strategy_id: str = None):
        super().__init__(message)
        self.strategy_id = strategy_id


class ValidationError(HLQuantBotError):
    """Data validation error."""
    pass
