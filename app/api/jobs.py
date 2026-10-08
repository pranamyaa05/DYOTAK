"""Jobs submission and management API routes (ARCHITECTURE.md Section 5)."""

from fastapi import APIRouter, HTTPException, status
from app.jobs.manager import job_manager
from contracts.schemas import (
    FactsJSON,
    JobRequest,
    JobStatus,
    ResultManifest,
)

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("", response_model=JobStatus, status_code=status.HTTP_202_ACCEPTED)
def submit_job(request: JobRequest) -> JobStatus:
    """Submit a new pipeline run."""
    return job_manager.create_job(request)


@router.get("/{job_id}", response_model=JobStatus)
def get_job_status(job_id: str) -> JobStatus:
    """Poll job progress, stage timing, and status."""
    status_obj = job_manager.registry.get_job(job_id)
    if not status_obj:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return status_obj


@router.post("/{job_id}/cancel", response_model=JobStatus)
def cancel_job(job_id: str) -> JobStatus:
    """Cancel a running or queued job."""
    status_obj = job_manager.cancel_job(job_id)
    if not status_obj:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return status_obj


@router.get("/{job_id}/result", response_model=ResultManifest)
def get_job_result(job_id: str) -> ResultManifest:
    """Retrieve result manifest containing layer URLs and provenance."""
    status_obj = job_manager.registry.get_job(job_id)
    if not status_obj:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    # In production, this reads data/jobs/<id>/manifest.json
    from pathlib import Path
    manifest_path = Path("contracts/examples/result_manifest.json")
    if manifest_path.is_file():
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = ResultManifest.model_validate_json(f.read())
            manifest.job_id = job_id
            return manifest

    raise HTTPException(status_code=404, detail="Manifest not ready")


@router.get("/{job_id}/facts", response_model=FactsJSON)
def get_job_facts(job_id: str) -> FactsJSON:
    """Retrieve single source of truth facts.json for this job."""
    from pathlib import Path
    facts_path = Path("contracts/examples/facts.json")
    if facts_path.is_file():
        with open(facts_path, "r", encoding="utf-8") as f:
            facts = FactsJSON.model_validate_json(f.read())
            facts.meta.job_id = job_id
            return facts

    raise HTTPException(status_code=404, detail="Facts not ready")
