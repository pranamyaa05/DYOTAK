"""In-memory thread-safe Job Registry with disk synchronization.

Adheres to ARCHITECTURE.md Section 5:
Any job found in 'running' or 'queued' state during startup reload is marked
failed: INTERNAL (interrupted).
"""

import threading
from pathlib import Path
from typing import Dict, List, Optional
from app.jobs.persistence import JOBS_DATA_DIR, load_job_status, save_job_status
from contracts.schemas import ErrorCode, ErrorDetail, JobState, JobStatus


class JobRegistry:
    """Thread-safe registry for job state management."""

    def __init__(self, data_dir: Path = JOBS_DATA_DIR):
        self._lock = threading.Lock()
        self._jobs: Dict[str, JobStatus] = {}
        self._data_dir = data_dir

    def register_job(self, status: JobStatus) -> None:
        with self._lock:
            self._jobs[status.job_id] = status
            save_job_status(status, self._data_dir)

    def get_job(self, job_id: str) -> Optional[JobStatus]:
        with self._lock:
            if job_id in self._jobs:
                return self._jobs[job_id]
            # Fallback to disk read
            loaded = load_job_status(job_id, self._data_dir)
            if loaded:
                self._jobs[job_id] = loaded
            return loaded

    def update_job(self, status: JobStatus) -> None:
        with self._lock:
            self._jobs[status.job_id] = status
            save_job_status(status, self._data_dir)

    def list_jobs(self) -> List[JobStatus]:
        with self._lock:
            return list(self._jobs.values())

    def reload_and_recover(self) -> None:
        """Scan disk on startup and recover interrupted jobs."""
        with self._lock:
            if not self._data_dir.is_dir():
                return
            for job_dir in self._data_dir.iterdir():
                if job_dir.is_dir():
                    status = load_job_status(job_dir.name, self._data_dir)
                    if status:
                        if status.status in (JobState.RUNNING, JobState.QUEUED):
                            status.status = JobState.FAILED
                            status.error = ErrorDetail(
                                code=ErrorCode.INTERNAL,
                                message_key="errors.internal",
                                params={"message": "Process interrupted by server restart"}
                            )
                            save_job_status(status, self._data_dir)
                        self._jobs[status.job_id] = status


job_registry = JobRegistry()
