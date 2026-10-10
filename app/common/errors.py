"""Typed application errors and error taxonomy for DYOTAK.

All external calls and pipeline checks raise typed exceptions with
machine-readable error codes and i18n message keys.
"""

from typing import Any, Dict
from contracts.schemas import ErrorCode, ErrorDetail


class NonRetryableError(Exception):
    """Marker mixin: errors that must not be retried (e.g. auth failures)."""


class DyotakError(Exception):
    """Base exception for all domain errors."""
    def __init__(self, code: ErrorCode, message_key: str, params: Dict[str, Any] = None):
        self.code = code
        self.message_key = message_key
        self.params = params or {}
        super().__init__(f"[{code.value}] {message_key} (params: {self.params})")

    def to_detail(self) -> ErrorDetail:
        return ErrorDetail(
            code=self.code,
            message_key=self.message_key,
            params=self.params
        )


class AoiTooLargeError(DyotakError):
    def __init__(self, area_km2: float, max_area_km2: float):
        super().__init__(
            code=ErrorCode.AOI_TOO_LARGE,
            message_key="errors.aoi_too_large",
            params={"area_km2": area_km2, "max_area_km2": max_area_km2}
        )


class NoValidOrbitPairError(DyotakError):
    def __init__(self, relative_orbit: int = None, direction: str = None, reason: str = None):
        super().__init__(
            code=ErrorCode.NO_VALID_ORBIT_PAIR,
            message_key="errors.no_valid_orbit_pair",
            params={"relative_orbit": relative_orbit, "direction": direction, "reason": reason}
        )


class NoPostEventSceneError(DyotakError):
    def __init__(self, event_date: str, latest_available: str = None):
        super().__init__(
            code=ErrorCode.NO_POST_EVENT_SCENE,
            message_key="errors.no_post_event_scene",
            params={"event_date": event_date, "latest_available": latest_available}
        )


class CdseUnavailableError(DyotakError):
    def __init__(self, status_code: int = None, details: str = None):
        super().__init__(
            code=ErrorCode.CDSE_UNAVAILABLE,
            message_key="errors.cdse_unavailable",
            params={"status_code": status_code, "details": details}
        )


class OhsomeUnavailableError(DyotakError):
    def __init__(self, details: str = None):
        super().__init__(
            code=ErrorCode.OHSOME_UNAVAILABLE,
            message_key="errors.ohsome_unavailable",
            params={"details": details}
        )


class CdseAuthError(NonRetryableError, CdseUnavailableError):
    """CDSE authentication/authorization failure (401/403 or bad credentials).

    Same error code as CDSE_UNAVAILABLE but never retried.
    """

    def __init__(self, status_code: int = None, details: str = None):
        super().__init__(status_code=status_code, details=details)
        self.message_key = "errors.cdse_auth_failed"


class OhsomeAuthError(NonRetryableError, OhsomeUnavailableError):
    """ohsome authentication/authorization failure (missing/invalid API key)."""

    def __init__(self, details: str = None):
        super().__init__(details=details)
        self.message_key = "errors.ohsome_auth_failed"


class OhsomeRequestError(NonRetryableError, OhsomeUnavailableError):
    """ohsome rejected the request with a non-auth 4xx (e.g. HTTP 422).

    Never retried: a malformed or unsupported request cannot succeed on a
    second attempt. Keeps the OHSOME_UNAVAILABLE code; the full (untruncated)
    response body is carried in ``details``.
    """


class ModelLowConfidenceError(DyotakError):
    def __init__(self, mean_confidence: float = None):
        super().__init__(
            code=ErrorCode.MODEL_LOW_CONFIDENCE,
            message_key="errors.model_low_confidence",
            params={"mean_confidence": mean_confidence}
        )


class S2TooCloudyError(DyotakError):
    def __init__(self, cloud_pct: float, threshold: float):
        super().__init__(
            code=ErrorCode.S2_TOO_CLOUDY,
            message_key="errors.s2_too_cloudy",
            params={"cloud_pct": cloud_pct, "threshold": threshold}
        )


class BusyError(DyotakError):
    def __init__(self, queue_len: int):
        super().__init__(
            code=ErrorCode.BUSY,
            message_key="errors.busy",
            params={"queue_len": queue_len}
        )


class InternalError(DyotakError):
    def __init__(self, message: str):
        super().__init__(
            code=ErrorCode.INTERNAL,
            message_key="errors.internal",
            params={"message": message}
        )
