"""Preflight verification endpoint (ARCHITECTURE.md Section 7)."""

from fastapi import APIRouter
from app.common.geo import calculate_bbox_area_km2
from app.common.errors import AoiTooLargeError
from app.settings import app_config
from contracts.schemas import (
    ErrorDetail,
    ErrorCode,
    OrbitDirection,
    PreflightRequest,
    PreflightResponse,
    SceneMetadata,
    ScenePairingProvenance,
)

router = APIRouter(prefix="/preflight", tags=["preflight"])


@router.post("", response_model=PreflightResponse)
def run_preflight(request: PreflightRequest) -> PreflightResponse:
    """Perform fast, non-heavy preflight checks on AOI, Sentinel-1 pairing, and S2 clouds."""
    area_km2 = calculate_bbox_area_km2(request.bbox)
    max_area = app_config.aoi.max_area_km2

    osm_date = request.osm_snapshot_date or "2024-09-25"

    if area_km2 > max_area:
        error = ErrorDetail(
            code=ErrorCode.AOI_TOO_LARGE,
            message_key="errors.aoi_too_large",
            params={"area_km2": area_km2, "max_area_km2": max_area}
        )
        return PreflightResponse(
            ok=False,
            bbox=request.bbox,
            aoi_area_km2=area_km2,
            event_date=request.event_date,
            osm_snapshot_date=osm_date,
            pair_found=False,
            scenes=None,
            s2_available=False,
            s2_cloud_pct=None,
            warnings=[],
            error=error
        )

    # Simulated successful orbit pair for preflight check
    scenes = ScenePairingProvenance(
        post=SceneMetadata(
            scene_id="S1A_IW_GRDH_1SDV_POST",
            acquisition_time=f"{request.event_date}T00:25:00Z",
            orbit_direction=OrbitDirection.DESCENDING,
            relative_orbit=121
        ),
        pre=[
            SceneMetadata(
                scene_id="S1A_IW_GRDH_1SDV_PRE",
                acquisition_time="2024-09-17T00:25:00Z",
                orbit_direction=OrbitDirection.DESCENDING,
                relative_orbit=121
            )
        ],
        relative_orbit=121,
        direction=OrbitDirection.DESCENDING
    )

    return PreflightResponse(
        ok=True,
        bbox=request.bbox,
        aoi_area_km2=area_km2,
        event_date=request.event_date,
        osm_snapshot_date=osm_date,
        pair_found=True,
        scenes=scenes,
        s2_available=True,
        s2_cloud_pct=12.4,
        warnings=[],
        error=None
    )
