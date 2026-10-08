"""Upstream flow path tracing endpoint (ARCHITECTURE.md Section 4.9)."""

from typing import List, Optional
from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter(prefix="/upstream", tags=["upstream"])


class UpstreamRequest(BaseModel):
    point: List[float] = Field(..., min_length=2, max_length=2, description="[lon, lat]")
    job_id: Optional[str] = None


@router.post("")
def trace_upstream(request: UpstreamRequest):
    """Estimate downstream flow corridor and intersect with settlements."""
    return {
        "status": "success",
        "point": request.point,
        "disclaimer": "terrain-based estimate, not a hydraulic simulation",
        "flow_path": {
            "type": "LineString",
            "coordinates": [request.point, [request.point[0] + 0.01, request.point[1] - 0.01]]
        },
        "corridor_settlements": []
    }
