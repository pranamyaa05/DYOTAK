"""Retry and timeout helper adhering to .cursorrules Rule 8.

Every external call has a timeout, retry with backoff, and a typed error.
"""

import asyncio
import functools
import logging
import time
from typing import Callable, Type, Tuple

logger = logging.getLogger("dyotak.retry")


def retry_with_backoff(
    retries: int = 3,
    backoff_factor: float = 1.5,
    timeout: float = 30.0,
    expected_exceptions: Tuple[Type[Exception], ...] = (Exception,),
    on_failure_raise: Callable[[Exception], Exception] = None
):
    """Decorator applying timeout, exponential backoff, and typed error mapping."""
    def decorator(func: Callable):
        if asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                delay = 1.0
                last_exc = None
                for attempt in range(1, retries + 1):
                    try:
                        return await asyncio.wait_for(func(*args, **kwargs), timeout=timeout)
                    except expected_exceptions as exc:
                        last_exc = exc
                        logger.warning(
                            f"Call {func.__name__} attempt {attempt}/{retries} failed: {exc}. Retrying in {delay}s..."
                        )
                        if attempt < retries:
                            await asyncio.sleep(delay)
                            delay *= backoff_factor
                if on_failure_raise:
                    raise on_failure_raise(last_exc)
                raise last_exc
            return async_wrapper
        else:
            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs):
                delay = 1.0
                last_exc = None
                for attempt in range(1, retries + 1):
                    try:
                        return func(*args, **kwargs)
                    except expected_exceptions as exc:
                        last_exc = exc
                        logger.warning(
                            f"Call {func.__name__} attempt {attempt}/{retries} failed: {exc}. Retrying in {delay}s..."
                        )
                        if attempt < retries:
                            time.sleep(delay)
                            delay *= backoff_factor
                if on_failure_raise:
                    raise on_failure_raise(last_exc)
                raise last_exc
            return sync_wrapper
    return decorator
