"""DYOTAK Data Contracts and Pydantic v2 Models.

Adheres strictly to /docs/ARCHITECTURE.md and .cursorrules.
"""

from enum import Enum
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ==============================================================================
# Error Codes & Details
# ==============================================================================

class ErrorCode(str, Enum):
    """Canonical error codes defined in ARCHITECTURE.md."""
    AOI_TOO_LARGE = "AOI_TOO_LARGE"
    NO_VALID_ORBIT_PAIR = "NO_VALID_ORBIT_PAIR"
    NO_POST_EVENT_SCENE = "NO_POST_EVENT_SCENE"
    CDSE_UNAVAILABLE = "CDSE_UNAVAILABLE"
    OHSOME_UNAVAILABLE = "OHSOME_UNAVAILABLE"
    MODEL_LOW_CONFIDENCE = "MODEL_LOW_CONFIDENCE"
    S2_TOO_CLOUDY = "S2_TOO_CLOUDY"
    BUSY = "BUSY"
    INTERNAL = "INTERNAL"


class ErrorDetail(BaseModel):
    """Structured error payload carrying locale message_key and parameter dict."""
    model_config = ConfigDict(extra="forbid")

    code: ErrorCode = Field(..., description="Machine-readable error code")
    message_key: str = Field(..., description="Key for i18n message catalogue")
    params: Dict[str, Any] = Field(default_factory=dict, description="Template parameters for translation")


class ErrorResponse(BaseModel):
    """Standard HTTP error envelope."""
    error: ErrorDetail


# ==============================================================================
# Pipeline Stages and Job State
# ==============================================================================

class PipelineStage(str, Enum):
    """Explicit pipeline stages defined in ARCHITECTURE.md Section 4."""
    PAIRING = "pairing"
    FETCH = "fetch"
    FLOOD_MODEL = "flood_model"
    TERRAIN_FILTER = "terrain_filter"
    OSM = "osm"
    DAMAGE = "damage"
    ISOLATION = "isolation"
    FACTS = "facts"
    REPORT = "report"


class StageState(str, Enum):
    """Execution status of an individual stage."""
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class StageStatus(BaseModel):
    """Progress record for a single pipeline stage."""
    model_config = ConfigDict(extra="forbid")

    name: PipelineStage = Field(..., description="Stage identifier")
    state: StageState = Field(default=StageState.PENDING, description="Execution status")
    started_at: Optional[str] = Field(None, description="ISO 8601 timestamp")
    ended_at: Optional[str] = Field(None, description="ISO 8601 timestamp")
    duration_seconds: Optional[float] = Field(None, description="Duration in seconds")
    warnings: List[str] = Field(default_factory=list, description="Non-fatal warnings recorded during stage")


class JobState(str, Enum):
    """High-level state of a pipeline job."""
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ==============================================================================
# Provenance
# ==============================================================================

class ProvenanceMode(str, Enum):
    """Provenance execution mode."""
    LIVE = "live"
    CACHED = "cached"
    DEGRADED = "degraded"


class OrbitDirection(str, Enum):
    """Sentinel-1 orbit pass direction."""
    ASCENDING = "ASCENDING"
    DESCENDING = "DESCENDING"


class SceneMetadata(BaseModel):
    """Metadata for an individual satellite acquisition."""
    model_config = ConfigDict(extra="forbid")

    scene_id: str = Field(..., description="Product identifier / scene name")
    acquisition_time: str = Field(..., description="ISO 8601 acquisition timestamp")
    orbit_direction: Optional[OrbitDirection] = Field(None, description="Pass direction")
    relative_orbit: Optional[int] = Field(None, description="Relative orbit number")


class ScenePairingProvenance(BaseModel):
    """Verified same-orbit Sentinel-1 pair metadata."""
    model_config = ConfigDict(extra="forbid")

    post: SceneMetadata = Field(..., description="Post-event acquisition")
    pre: List[SceneMetadata] = Field(..., description="Pre-event baseline acquisition(s)")
    relative_orbit: int = Field(..., description="Shared relative orbit number")
    direction: OrbitDirection = Field(..., description="Shared pass direction")


class S2Provenance(BaseModel):
    """Sentinel-2 optical layer usage and cloud coverage."""
    model_config = ConfigDict(extra="forbid")

    used: bool = Field(..., description="Whether optical data was incorporated")
    cloud_pct: Optional[float] = Field(None, description="Estimated cloud percentage in AOI clip")
    scene_id: Optional[str] = Field(None, description="S2 L2A scene identifier if used")


class DegradationItem(BaseModel):
    """Disclosed degradation entry."""
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., description="Machine-readable degradation code")
    reason_key: str = Field(..., description="i18n catalogue key explaining degradation")
    details: Optional[Dict[str, Any]] = Field(default=None, description="Additional context")


class Provenance(BaseModel):
    """Complete provenance object attached to all output artifacts."""
    model_config = ConfigDict(extra="forbid")

    mode: ProvenanceMode = Field(..., description="Execution mode: live, cached, or degraded")
    computed_at: str = Field(..., description="ISO 8601 timestamp of computation")
    code_version: str = Field(..., description="Git commit hash or release version")
    config_hash: str = Field(..., description="Hash of active configuration values")
    model_version: str = Field(..., description="Model weights identifier or baseline version")
    scenes: ScenePairingProvenance = Field(..., description="S1 pair details")
    s2: S2Provenance = Field(..., description="S2 optical details")
    osm_snapshot_date: str = Field(..., description="Pre-event snapshot date used for OSM query")
    degradations: List[DegradationItem] = Field(default_factory=list, description="List of degradations if any")


# ==============================================================================
# Preflight Request & Response
# ==============================================================================

class PreflightRequest(BaseModel):
    """Lightweight availability and orbit compatibility check before starting a job."""
    model_config = ConfigDict(extra="forbid")

    bbox: List[float] = Field(
        ...,
        min_length=4,
        max_length=4,
        description="Bounding box [min_lon, min_lat, max_lon, max_lat] in WGS84"
    )
    event_date: str = Field(..., description="Event date in YYYY-MM-DD format")
    osm_snapshot_date: Optional[str] = Field(
        None,
        description="Optional OSM snapshot date; must strictly precede event_date"
    )

    @field_validator("bbox")
    @classmethod
    def validate_bbox(cls, v: List[float]) -> List[float]:
        min_lon, min_lat, max_lon, max_lat = v
        if min_lon >= max_lon or min_lat >= max_lat:
            raise ValueError(f"Invalid bbox coordinates: min values must be less than max values: {v}")
        if not (-180 <= min_lon <= 180 and -180 <= max_lon <= 180):
            raise ValueError("Longitude must be between -180 and 180")
        if not (-90 <= min_lat <= 90 and -90 <= max_lat <= 90):
            raise ValueError("Latitude must be between -90 and 90")
        return v

    @model_validator(mode="after")
    def validate_osm_before_event(self) -> "PreflightRequest":
        if self.osm_snapshot_date is not None:
            assert self.osm_snapshot_date < self.event_date, (
                f"osm_snapshot_date ({self.osm_snapshot_date}) must strictly precede event_date ({self.event_date})"
            )
        return self


class PreflightResponse(BaseModel):
    """Result of preflight check."""
    model_config = ConfigDict(extra="forbid")

    ok: bool = Field(..., description="Whether a job can be launched with these parameters")
    bbox: List[float] = Field(..., min_length=4, max_length=4)
    aoi_area_km2: float = Field(..., description="Computed AOI area in square kilometers")
    event_date: str = Field(...)
    osm_snapshot_date: str = Field(...)
    pair_found: bool = Field(..., description="Whether a valid same-orbit S1 pair is available")
    scenes: Optional[ScenePairingProvenance] = Field(None, description="Matched S1 pair if found")
    s2_available: bool = Field(default=False, description="Whether clear S2 optical imagery exists")
    s2_cloud_pct: Optional[float] = Field(None, description="Optical cloud estimate if inspected")
    warnings: List[str] = Field(default_factory=list, description="Warnings (e.g. S2 cloudy, high slope)")
    error: Optional[ErrorDetail] = Field(None, description="Blocking error if ok=False")


# ==============================================================================
# Job Request & Status
# ==============================================================================

class JobOptions(BaseModel):
    """Optional execution tunables for a job."""
    model_config = ConfigDict(extra="forbid")

    use_baseline: bool = Field(default=False, description="Force log-ratio baseline instead of ML model")
    optical_enabled: bool = Field(default=True, description="Attempt Sentinel-2 optical cue extraction")
    max_pre_scenes: Optional[int] = Field(None, description="Max pre-event acquisitions to median")


class JobRequest(BaseModel):
    """Payload to trigger a full flood and damage mapping run."""
    model_config = ConfigDict(extra="forbid")

    bbox: List[float] = Field(
        ...,
        min_length=4,
        max_length=4,
        description="Bounding box [min_lon, min_lat, max_lon, max_lat] in WGS84"
    )
    event_date: str = Field(..., description="Event date in YYYY-MM-DD format")
    osm_snapshot_date: Optional[str] = Field(
        None,
        description="Optional OSM snapshot date; must strictly precede event_date"
    )
    options: JobOptions = Field(default_factory=JobOptions)

    @field_validator("bbox")
    @classmethod
    def validate_bbox(cls, v: List[float]) -> List[float]:
        min_lon, min_lat, max_lon, max_lat = v
        if min_lon >= max_lon or min_lat >= max_lat:
            raise ValueError(f"Invalid bbox coordinates: min values must be less than max values: {v}")
        if not (-180 <= min_lon <= 180 and -180 <= max_lon <= 180):
            raise ValueError("Longitude must be between -180 and 180")
        if not (-90 <= min_lat <= 90 and -90 <= max_lat <= 90):
            raise ValueError("Latitude must be between -90 and 90")
        return v

    @model_validator(mode="after")
    def validate_osm_before_event(self) -> "JobRequest":
        if self.osm_snapshot_date is not None:
            assert self.osm_snapshot_date < self.event_date, (
                f"osm_snapshot_date ({self.osm_snapshot_date}) must strictly precede event_date ({self.event_date})"
            )
        return self


class JobStatus(BaseModel):
    """Current lifecycle status and stage progression of a job."""
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(..., description="Unique job identifier")
    status: JobState = Field(..., description="Overall job state")
    current_stage: Optional[PipelineStage] = Field(None, description="Currently active stage if running")
    progress: float = Field(0.0, ge=0.0, le=1.0, description="Overall progress fraction [0.0, 1.0]")
    stages: List[StageStatus] = Field(default_factory=list, description="Per-stage progress records")
    created_at: str = Field(..., description="ISO 8601 job creation timestamp")
    updated_at: str = Field(..., description="ISO 8601 last update timestamp")
    warnings: List[str] = Field(default_factory=list, description="Accumulated job warnings")
    error: Optional[ErrorDetail] = Field(None, description="Failure details if status=failed")


# ==============================================================================
# Facts JSON Model
# ==============================================================================

class FactItem(BaseModel):
    """An individual atomic fact item."""
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Machine-readable fact key e.g. flooded_area_km2")
    value: Union[int, float, str, bool] = Field(..., description="Fact value computed by pipeline")
    unit: str = Field(..., description="Unit of measurement e.g. km2, count, km")
    source_stage: PipelineStage = Field(..., description="Pipeline stage producing this fact")


class SettlementClass(str, Enum):
    """Settlement isolation status classes defined in ARCHITECTURE.md Section 4.7."""
    NEWLY_CUT_OFF = "newly_cut_off"
    STILL_CONNECTED = "still_connected"
    NO_PRE_EVENT_ACCESS = "no_pre_event_access"


class CutoffSettlementRecord(BaseModel):
    """Entry in cutoff_settlements table."""
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str = Field(..., description="Settlement name from OSM place/town tag")
    settlement_class: SettlementClass = Field(..., alias="class", description="Cut-off classification")
    buildings_affected: int = Field(..., description="Number of flooded/affected buildings in cluster")
    extra_distance_km: Optional[float] = Field(None, description="Detour km if still connected, null if cut off")
    priority_rank: int = Field(..., description="Rescue priority rank (1 = highest priority)")


class FactsMeta(BaseModel):
    """Metadata enclosing facts.json."""
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(...)
    bbox: List[float] = Field(..., min_length=4, max_length=4)
    event_date: str = Field(...)
    provenance: Provenance = Field(...)
    units: Dict[str, str] = Field(
        default_factory=lambda: {
            "area": "km2",
            "distance": "km",
            "count": "integer"
        }
    )


class FactsTables(BaseModel):
    """Structured tabular data in facts.json."""
    model_config = ConfigDict(extra="forbid")

    cutoff_settlements: List[CutoffSettlementRecord] = Field(default_factory=list)


class FactsJSON(BaseModel):
    """Single source of truth numbers generated by Stage 8 (Facts)."""
    model_config = ConfigDict(extra="forbid")

    meta: FactsMeta
    facts: List[FactItem]
    tables: FactsTables


# ==============================================================================
# Result Manifest & Layer Metadata
# ==============================================================================

class LayerType(str, Enum):
    """Type of map layer."""
    RASTER = "raster"
    VECTOR = "vector"


class LayerFormat(str, Enum):
    """File/wire format for layer."""
    PNG = "png"
    GEOJSON = "geojson"
    GEOPARQUET = "geoparquet"
    COG = "cog"


class LayerMetadata(BaseModel):
    """Description and download/render URL of a generated layer."""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Layer identifier e.g. flood_polygons, s1_post")
    layer_type: LayerType = Field(..., description="Raster or vector")
    format: LayerFormat = Field(..., description="Format encoding")
    url: str = Field(..., description="Endpoint URL to fetch layer data")
    bounds: Optional[List[float]] = Field(None, description="Layer bounding box [min_lon, min_lat, max_lon, max_lat]")
    feature_count: Optional[int] = Field(None, description="Feature count for vectors")
    legend: Optional[Dict[str, Any]] = Field(None, description="Legend and styling tokens")


class ResultManifest(BaseModel):
    """Complete manifest returned upon job completion."""
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(...)
    bbox: List[float] = Field(..., min_length=4, max_length=4)
    event_date: str = Field(...)
    provenance: Provenance = Field(...)
    layers: Dict[str, LayerMetadata] = Field(..., description="Map of available raster and vector layers")
    facts_url: str = Field(..., description="URL to fetch full facts.json")
    report_url: str = Field(..., description="URL to generate/fetch situation report PDF")
    summary: Dict[str, Any] = Field(default_factory=dict, description="Headline metric summaries")


# ==============================================================================
# Health Check
# ==============================================================================

class HealthResponse(BaseModel):
    """System liveness and component status."""
    model_config = ConfigDict(extra="forbid")

    status: str = Field("ok", description="Liveness state")
    version: str = Field("0.1.0", description="App version")
    mock_mode: bool = Field(False, description="Whether server is operating in mock mode")
    onnx_available: bool = Field(True, description="Whether ONNX runtime is available for inference")
    torch_detected: bool = Field(False, description="Compliance check: MUST be False in production")
