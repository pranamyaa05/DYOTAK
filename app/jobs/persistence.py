"""Disk persistence of job states and artifacts under data/jobs/<id>/."""

import json
from pathlib import Path
from typing import Optional
from contracts.schemas import JobStatus


JOBS_DATA_DIR = Path("data/jobs")


def save_job_status(status: JobStatus, base_dir: Path = JOBS_DATA_DIR) -> Path:
    """Save JobStatus to data/jobs/<id>/job.json."""
    job_dir = base_dir / status.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    target = job_dir / "job.json"
    with open(target, "w", encoding="utf-8") as f:
        f.write(status.model_dump_json(indent=2))
    return target


def load_job_status(job_id: str, base_dir: Path = JOBS_DATA_DIR) -> Optional[JobStatus]:
    """Load JobStatus from data/jobs/<id>/job.json if present."""
    target = base_dir / job_id / "job.json"
    if not target.is_file():
        return None
    with open(target, "r", encoding="utf-8") as f:
        data = f.read()
    return JobStatus.model_validate_json(data)
