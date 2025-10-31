"""Request ID middleware for tracking requests across logs."""

import uuid

from config.logging import set_request_id
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response


def get_request_id(request: Request) -> str:
    """Get request ID from request state.

    Args:
        request: FastAPI Request object

    Returns:
        Request ID string, or "unknown" if not set
    """
    return getattr(request.state, "request_id", "unknown")


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Middleware to assign unique ID to each request."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Process request with unique request ID.

        Args:
            request: Incoming HTTP request
            call_next: Next middleware/route handler

        Returns:
            HTTP response with X-Request-ID header
        """
        # Generate or extract request ID
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))

        # Set request ID in logging context
        set_request_id(request_id)

        # Attach to request state for access in routes
        request.state.request_id = request_id

        # Process request
        response = await call_next(request)

        # Add request ID to response headers
        response.headers["X-Request-ID"] = request_id

        return response
