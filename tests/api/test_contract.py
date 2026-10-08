"""Contract tests: /contracts/openapi.json, schemas and request validation.

These tests are the interface guarantee published to the frontend owner
(docs/ARCHITECTURE.md Section 7 and 15).
"""

import json

import pytest
from pydantic import ValidationError

from app.main import app
from contracts.schemas import (
    ErrorCode,
    JobRequest,
    PipelineStage,
    PreflightRequest,
)


# Endpoints that must exist per ARCHITECTURE.md Section 7.
REQUIRED_ENDPOINTS = [
    ("get", "/api/presets"),
    ("post", "/api/preflight"),
    ("post", "/api/jobs"),
    ("get", "/api/jobs/{job_id}"),
    ("post", "/api/jobs/{job_id}/cancel"),
    ("get", "/api/jobs/{job_id}/result"),
    ("get", "/api/jobs/{job_id}/layers/{layer_name}"),
    ("post", "/api/upstream"),
    ("post", "/api/copilot/ask"),
    ("get", "/api/jobs/{job_id}/report"),
    ("get", "/api/tiles/terrain/{z}/{x}/{y}.png"),
    ("get", "/api/eval/summary"),
    ("get", "/api/eval/emsr927"),
    ("get", "/api/health"),
    ("get", "/api/selftest"),
]

REQUIRED_ERROR_CODES = {
    "AOI_TOO_LARGE",
    "NO_VALID_ORBIT_PAIR",
    "NO_POST_EVENT_SCENE",
    "CDSE_UNAVAILABLE",
    "OHSOME_UNAVAILABLE",
    "MODEL_LOW_CONFIDENCE",
    "INTERNAL",
}

REQUIRED_STAGES = [
    "pairing",
    "fetch",
    "flood_model",
    "terrain_filter",
    "osm",
    "damage",
    "isolation",
    "facts",
    "report",
]


def _load_openapi(repo_root):
    with open(repo_root / "contracts" / "openapi.json", "r", encoding="utf-8") as fh:
        return json.load(fh)


def test_openapi_document_is_valid(repo_root):
    spec = _load_openapi(repo_root)
    assert spec["openapi"].startswith("3.")
    assert spec["info"]["title"] == "DYOTAK"
    assert isinstance(spec["paths"], dict) and spec["paths"]
    assert "schemas" in spec["components"]


def test_openapi_matches_running_app(repo_root):
    """The published contract must not drift from the FastAPI app."""
    assert _load_openapi(repo_root) == app.openapi()


@pytest.mark.parametrize("method,path", REQUIRED_ENDPOINTS)
def test_required_endpoint_present(repo_root, method, path):
    spec = _load_openapi(repo_root)
    assert path in spec["paths"], f"missing path {path}"
    assert method in spec["paths"][path], f"missing {method.upper()} {path}"


def test_error_code_enum_contains_required_codes():
    codes = {code.value for code in ErrorCode}
    assert REQUIRED_ERROR_CODES.issubset(codes)


def test_openapi_error_code_enum_matches_schema(repo_root):
    spec = _load_openapi(repo_root)
    api_enum = set(spec["components"]["schemas"]["ErrorCode"]["enum"])
    schema_enum = {code.value for code in ErrorCode}
    assert api_enum == schema_enum


def test_pipeline_stage_enum_matches_architecture():
    assert [stage.value for stage in PipelineStage] == REQUIRED_STAGES


def test_preflight_request_accepts_valid_bbox(example):
    req = PreflightRequest.model_validate(example("preflight_request.json"))
    assert req.bbox == [85.15, 27.85, 85.35, 28.05]


@pytest.mark.parametrize(
    "bbox",
    [
        [85.35, 27.85, 85.15, 28.05],  # min_lon >= max_lon
        [85.15, 28.05, 85.35, 27.85],  # min_lat >= max_lat
        [181.0, 27.85, 185.0, 28.05],  # longitude out of range
        [85.15, -95.0, 85.35, 28.05],  # latitude out of range
    ],
)
def test_preflight_request_rejects_bad_bbox(bbox):
    with pytest.raises(ValidationError):
        PreflightRequest(bbox=bbox, event_date="2024-09-28")


def test_preflight_request_rejects_wrong_bbox_length():
    with pytest.raises(ValidationError):
        PreflightRequest(bbox=[85.15, 27.85, 85.35], event_date="2024-09-28")


@pytest.mark.parametrize(
    "model",
    [PreflightRequest, JobRequest],
)
def test_osm_snapshot_must_precede_event_date(model):
    with pytest.raises((ValidationError, AssertionError)):
        model(
            bbox=[85.15, 27.85, 85.35, 28.05],
            event_date="2024-09-28",
            osm_snapshot_date="2024-09-28",
        )
    with pytest.raises((ValidationError, AssertionError)):
        model(
            bbox=[85.15, 27.85, 85.35, 28.05],
            event_date="2024-09-28",
            osm_snapshot_date="2024-10-01",
        )
    # A strictly earlier snapshot is accepted.
    req = model(
        bbox=[85.15, 27.85, 85.35, 28.05],
        event_date="2024-09-28",
        osm_snapshot_date="2024-09-25",
    )
    assert req.osm_snapshot_date == "2024-09-25"
