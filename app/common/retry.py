"""Retry and timeout helper adhering to .cursorrules Rule 8.

Every external call has a timeout, retry with backoff, and a typed error.

Authentication/authorization failures (HTTP 401/403 or invalid credentials) are
NOT retried: they cannot succeed on a second attempt, and retrying burns rate
limit. Anything deriving from ``app.common.errors.NonRetryableError`` — or listed
in ``no_retry_exceptions`` — is raised immediately.
"""

import asyncio
import functools
import logging
import time
from typing import Callable, Tuple, Type

from app.common.errors import NonRetryableError

logger = logging.getLogger("dyotak.retry")


def _is_non_retryable(exc: BaseException, extra: Tuple[Type[Exception], ...]) -> bool:
    return isinstance(exc, NonRetryableError) or isinstance(exc, extra)


def retry_with_backoff(
    retries: int = 3,
    backoff_factor: float = 1.5,
    timeout: float = 30.0,
    expected_exceptions: Tuple[Type[Exception], ...] = (Exception,),
    on_failure_raise: Callable[[Exception], Exception] = None,
    no_retry_exceptions: Tuple[Type[Exception], ...] = (),
):
    """Decorator applying timeout, exponential backoff, and typed error mapping.

    Errors that are non-retryable (auth failures) are re-raised immediately,
    without exhausting the retry budget.
    """
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
                        if _is_non_retryable(exc, no_retry_exceptions):
                            logger.warning(f"{func.__name__} failed fast (non-retryable): {exc}")
                            raise
                        logger.warning(
                            f"Call {func.__name__} attempt {attempt}/{retries} failed: {exc}. "
                            f"Retrying in {delay}s..."
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
                        if _is_non_retryable(exc, no_retry_exceptions):
                            logger.warning(f"{func.__name__} failed fast (non-retryable): {exc}")
                            raise
                        logger.warning(
                            f"Call {func.__name__} attempt {attempt}/{retries} failed: {exc}. "
                            f"Retrying in {delay}s..."
                        )
                        if attempt < retries:
                            time.sleep(delay)
                            delay *= backoff_factor
                if on_failure_raise:
                    raise on_failure_raise(last_exc)
                raise last_exc
            return sync_wrapper
    return decorator
