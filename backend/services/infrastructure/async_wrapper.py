"""Async wrapper utilities for synchronous operations.

The Hyperliquid Python SDK is synchronous-only. This module provides
utilities to wrap synchronous SDK calls to prevent blocking the FastAPI
async event loop.
"""

import asyncio
import functools
from collections.abc import Callable, Coroutine
from typing import Any, ParamSpec, TypeVar

from config.logging import get_logger

logger = get_logger(__name__)

P = ParamSpec("P")
R = TypeVar("R")


async def run_in_thread(func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """Run synchronous function in thread pool to avoid blocking event loop.

    Args:
        func: Synchronous function to execute
        *args: Positional arguments for func
        **kwargs: Keyword arguments for func

    Returns:
        Result from func execution

    Raises:
        Any exception raised by func

    Example:
        # Wrap synchronous Hyperliquid SDK call
        info = hyperliquid.Info()
        user_state = await run_in_thread(info.user_state, wallet_address)
    """
    loop = asyncio.get_running_loop()

    try:
        # Run synchronous function in default thread pool executor
        result = await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))
        return result
    except Exception as e:
        logger.error(
            f"Error running {func.__name__} in thread",
            extra={"context": {"error": str(e), "function": func.__name__}},
        )
        raise


def async_wrap(func: Callable[P, R]) -> Callable[P, Coroutine[Any, Any, R]]:
    """Decorator to automatically wrap synchronous function as async.

    Args:
        func: Synchronous function to wrap

    Returns:
        Async version of function

    Example:
        @async_wrap
        def sync_hyperliquid_call(info, address):
            return info.user_state(address)

        # Can now await it
        result = await sync_hyperliquid_call(info, address)
    """

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        return await run_in_thread(func, *args, **kwargs)

    return wrapper
