"""Cooperative cancellation tokens for pipeline jobs."""

import threading
from typing import Dict, Set


class CancellationRegistry:
    """Thread-safe registry of cancellation requests."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cancelled_jobs: Set[str] = set()

    def request_cancellation(self, job_id: str) -> None:
        with self._lock:
            self._cancelled_jobs.add(job_id)

    def is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._cancelled_jobs

    def clear(self, job_id: str) -> None:
        with self._lock:
            self._cancelled_jobs.discard(job_id)


cancellation_registry = CancellationRegistry()
