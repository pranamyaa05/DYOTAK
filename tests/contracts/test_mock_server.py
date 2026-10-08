"""Mock server tests: /contracts/mock_server.py serves schema-valid examples.

The mock backend exists so the frontend can build in parallel against the
contract before live endpoints pass the same schema tests.
"""

import pytest
from fastapi.testclient import TestClient

from contracts.mock_server import create_mock_app
from contracts.schemas import (
    FactsJSON,
    HealthResponse,
    JobStatus,
    PreflightResponse,
    ResultManifest,
)


@pytest.fixture(scope="module")
def client():
    return TestClient(create_mock_app())


def test_mock_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    health = HealthResponse.model_validate(resp.json())
    assert health.status == "ok"
    assert health.mock_mode is True


def test_mock_presets(client):
    resp = client.get("/api/presets")
    assert resp.status_code == 200
    presets = resp.json()
    assert isinstance(presets, list) and presets
    assert {"id", "bbox", "event_date"}.issubset(presets[0])


def test_mock_preflight_success(client, example):
    resp = client.post("/api/preflight", json=example("preflight_request.json"))
    assert resp.status_code == 200
    body = PreflightResponse.model_validate(resp.json())
    assert body.ok is True
    assert body.pair_found is True


def test_mock_preflight_error_switch(client, example):
    resp = client.post(
        "/api/preflight",
        json=example("preflight_request.json"),
        headers={"x-mock-error": "AOI_TOO_LARGE"},
    )
    assert resp.status_code == 200
    body = PreflightResponse.model_validate(resp.json())
    assert body.ok is False
    assert body.error is not None
    assert body.error.code.value == "AOI_TOO_LARGE"


def test_mock_submit_job(client, example):
    resp = client.post("/api/jobs", json=example("job_request.json"))
    assert resp.status_code == 202
    JobStatus.model_validate(resp.json())


@pytest.mark.parametrize("state", ["queued", "running", "succeeded", "failed"])
def test_mock_job_status_states(client, state):
    resp = client.get(f"/api/jobs/job-xyz", params={"state": state})
    assert resp.status_code == 200
    status = JobStatus.model_validate(resp.json())
    assert status.job_id == "job-xyz"


def test_mock_cancel_job(client):
    resp = client.post("/api/jobs/job-xyz/cancel")
    assert resp.status_code == 200
    assert JobStatus.model_validate(resp.json()).status.value == "cancelled"


def test_mock_result_manifest(client):
    resp = client.get("/api/jobs/job-xyz/result")
    assert resp.status_code == 200
    manifest = ResultManifest.model_validate(resp.json())
    assert manifest.job_id == "job-xyz"
    assert manifest.layers


def test_mock_facts(client):
    resp = client.get("/api/jobs/job-xyz/facts")
    assert resp.status_code == 200
    facts = FactsJSON.model_validate(resp.json())
    assert facts.meta.job_id == "job-xyz"


def test_mock_layer_raster_and_vector(client):
    png = client.get("/api/jobs/job-xyz/layers/flood_probability")
    assert png.status_code == 200
    assert png.headers["content-type"] == "image/png"

    geojson = client.get("/api/jobs/job-xyz/layers/flood_polygons")
    assert geojson.status_code == 200
    assert geojson.json()["type"] == "FeatureCollection"


def test_mock_upstream(client):
    resp = client.post("/api/upstream", json={"point": [85.25, 27.95]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["disclaimer"] == "terrain-based estimate, not a hydraulic simulation"
    assert body["flow_path"]["type"] == "LineString"


def test_mock_copilot_ask_en_and_ne(client):
    en = client.post("/api/copilot/ask", json={"job_id": "j", "question": "flood?", "lang": "en"})
    assert en.status_code == 200
    assert en.json()["template_fallback"] is True

    ne = client.post("/api/copilot/ask", json={"job_id": "j", "question": "flood?", "lang": "ne"})
    assert ne.status_code == 200
    assert ne.json()["answer"]


def test_mock_report_and_tiles(client):
    assert client.get("/api/jobs/job-xyz/report", params={"lang": "ne"}).status_code == 200
    assert client.get("/api/tiles/terrain/10/1/1.png").status_code == 200


def test_mock_eval_endpoints(client):
    summary = client.get("/api/eval/summary")
    assert summary.status_code == 200
    assert summary.json()["status"] == "precomputed"

    emsr = client.get("/api/eval/emsr927")
    assert emsr.status_code == 200
    assert emsr.json()["disclaimer"] == "precomputed validation"
