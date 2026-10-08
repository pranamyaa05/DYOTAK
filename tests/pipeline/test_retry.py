"""Tests for app.common.retry fail-fast behaviour on auth errors."""

import pytest

from app.common.errors import CdseAuthError, NonRetryableError, OhsomeAuthError
from app.common.retry import retry_with_backoff


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # Keep retry tests fast: skip the backoff sleeps.
    monkeypatch.setattr("app.common.retry.time.sleep", lambda _s: None)


def test_auth_errors_are_non_retryable_marker():
    assert issubclass(CdseAuthError, NonRetryableError)
    assert issubclass(OhsomeAuthError, NonRetryableError)
    assert CdseAuthError(status_code=401).code.value == "CDSE_UNAVAILABLE"
    assert OhsomeAuthError(details="x").code.value == "OHSOME_UNAVAILABLE"


def test_non_retryable_error_fails_fast_and_is_not_wrapped():
    calls = {"n": 0}

    @retry_with_backoff(
        retries=5,
        backoff_factor=1.0,
        on_failure_raise=lambda exc: RuntimeError("wrapped"),
    )
    def fn():
        calls["n"] += 1
        raise CdseAuthError(status_code=403, details="forbidden")

    with pytest.raises(CdseAuthError):
        fn()
    assert calls["n"] == 1  # exactly one attempt, no retries


def test_transient_error_retries_then_raises_wrapped():
    calls = {"n": 0}

    @retry_with_backoff(
        retries=3,
        backoff_factor=1.0,
        on_failure_raise=lambda exc: RuntimeError("wrapped"),
    )
    def fn():
        calls["n"] += 1
        raise ValueError("transient")

    with pytest.raises(RuntimeError):
        fn()
    assert calls["n"] == 3


def test_passthrough_succeeds_first_try():
    @retry_with_backoff(retries=3, backoff_factor=1.0)
    def fn():
        return 42

    assert fn() == 42


def test_explicit_no_retry_exceptions():
    calls = {"n": 0}

    class Custom(Exception):
        pass

    @retry_with_backoff(
        retries=4, backoff_factor=1.0, no_retry_exceptions=(Custom,)
    )
    def fn():
        calls["n"] += 1
        raise Custom("nope")

    with pytest.raises(Custom):
        fn()
    assert calls["n"] == 1
