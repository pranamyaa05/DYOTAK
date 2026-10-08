"""Job execution manager for coordinating async pipeline jobs."""

import asyncio
from datetime import datetime, timezone
import uuid
from typing import Optional
from app.jobs.cancellation import cancellation_registry
from app.jobs.registry import job_registry
from contracts.schemas import (
    JobRequest,
    JobState,
    JobStatus,
    PipelineStage,
    StageState,
    StageStatus,
)


class JobManager:
    """Manages job submission, execution and lifecycle."""

    def __init__(self):
        self.registry = job_registry
        self.cancellation = cancellation_registry

    def create_job(self, request: JobRequest) -> JobStatus:
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()

        stages = [
            StageStatus(name=s, state=StageState.PENDING)
            for s in PipelineStage
        ]

        status = JobStatus(
            job_id=job_id,
            status=JobState.QUEUED,
            current_stage=None,
            progress=0.0,
            stages=stages,
            created_at=now,
            updated_at=now,
            warnings=[],
            error=None
        )
        self.registry.register_job(status)
        return status

    def cancel_job(self, job_id: str) -> Optional[JobStatus]:
        status = self.registry.get_job(job_id)
        if not status:
            return None
        self.cancellation.request_cancellation(job_id)
        status.status = JobState.CANCELLED
        status.updated_at = datetime.now(timezone.utc).isoformat()
        self.registry.update_job(status)
        return status


job_manager = JobManager()
