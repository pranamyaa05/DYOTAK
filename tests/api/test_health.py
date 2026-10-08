"""Health endpoint tests (ARCHITECTURE.md Section 7)."""

from fastapi.testclient import TestClient

from app.main import app
from contracts.schemas import HealthResponse

client = TestClient(app)


def test_health_returns_ok():
    resp = client.get("/api/health")
    assert resp.status_code == 200
    health = HealthResponse.model_validate(resp.json())
    assert health.status == "ok"
    assert health.version
    # Compliance: no torch in the production process.
    assert health.torch_detected is False


def test_selftest_returns_checks():
    resp = client.get("/api/selftest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "passed"
    assert isinstance(body["checks"], dict) and body["checks"]
