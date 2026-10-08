"""Mock server returning contracts/examples payloads.

Enables the frontend to build in parallel according to ARCHITECTURE.md Section 11:
"Contract development uses /contracts/mock (a mock server serving examples) until live endpoints pass the same schema tests."
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, Header, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from contracts.schemas import (
    ErrorCode,
    ErrorDetail,
    ErrorResponse,
    FactsJSON,
    HealthResponse,
    JobRequest,
    JobStatus,
    PreflightRequest,
    PreflightResponse,
    ResultManifest,
)

EXAMPLES_DIR = Path(__file__).parent / "examples"


def load_example(filename: str) -> Dict[str, Any]:
    target = EXAMPLES_DIR / filename
    if not target.is_file():
        raise FileNotFoundError(f"Missing example JSON: {filename}")
    with open(target, "r", encoding="utf-8") as f:
        return json.load(f)


def create_mock_app() -> FastAPI:
    mock_app = FastAPI(
        title="DYOTAK Mock Server",
        description="Parallel frontend mock backend serving /contracts/examples payloads.",
        version="0.1.0-mock"
    )

    mock_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @mock_app.get("/api/health", response_model=HealthResponse)
    def mock_health() -> HealthResponse:
        data = load_example("health_response.json")
        data["mock_mode"] = True
        return HealthResponse.model_validate(data)

    @mock_app.get("/api/selftest")
    def mock_selftest():
        return {
            "status": "passed",
            "mock_mode": True,
            "checks": {"pipeline": "mocked", "contracts": "valid"}
        }

    @mock_app.get("/api/presets")
    def mock_presets() -> List[Dict[str, Any]]:
        return [
            {
                "id": "trishuli_emsr927",
                "name": "Trishuli River Basin, Nepal",
                "description": "September 2024 flood event (Copernicus EMS EMSR927 case study)",
                "bbox": [85.15, 27.85, 85.35, 28.05],
                "event_date": "2024-09-28",
                "osm_snapshot_date": "2024-09-25"
            }
        ]

    @mock_app.post("/api/preflight", response_model=PreflightResponse)
    def mock_preflight(req: PreflightRequest, x_mock_error: Optional[str] = Header(None)) -> PreflightResponse:
        if x_mock_error:
            # Test hook: return the error code requested via the X-Mock-Error header.
            try:
                code = ErrorCode(x_mock_error)
            except ValueError:
                code = ErrorCode.INTERNAL
            return PreflightResponse(
                ok=False,
                bbox=req.bbox,
                aoi_area_km2=2800.0,
                event_date=req.event_date,
                osm_snapshot_date=req.osm_snapshot_date or "2024-09-25",
                pair_found=False,
                error=ErrorDetail(
                    code=code,
                    message_key=f"errors.{code.value.lower()}",
                    params={},
                )
            )
        data = load_example("preflight_response.json")
        data["bbox"] = req.bbox
        data["event_date"] = req.event_date
        return PreflightResponse.model_validate(data)

    @mock_app.post("/api/jobs", response_model=JobStatus, status_code=status.HTTP_202_ACCEPTED)
    def mock_submit_job(req: JobRequest) -> JobStatus:
        data = load_example("job_status_queued.json")
        return JobStatus.model_validate(data)

    @mock_app.get("/api/jobs/{job_id}", response_model=JobStatus)
    def mock_get_job_status(job_id: str, state: Optional[str] = Query("succeeded")) -> JobStatus:
        if state == "running":
            data = load_example("job_status_running.json")
        elif state == "failed":
            data = load_example("job_status_failed.json")
        elif state == "queued":
            data = load_example("job_status_queued.json")
        else:
            data = load_example("job_status_succeeded.json")

        data["job_id"] = job_id
        return JobStatus.model_validate(data)

    @mock_app.post("/api/jobs/{job_id}/cancel", response_model=JobStatus)
    def mock_cancel_job(job_id: str) -> JobStatus:
        data = load_example("job_status_queued.json")
        data["job_id"] = job_id
        data["status"] = "cancelled"
        return JobStatus.model_validate(data)

    @mock_app.get("/api/jobs/{job_id}/result", response_model=ResultManifest)
    def mock_get_result(job_id: str) -> ResultManifest:
        data = load_example("result_manifest.json")
        data["job_id"] = job_id
        return ResultManifest.model_validate(data)

    @mock_app.get("/api/jobs/{job_id}/facts", response_model=FactsJSON)
    def mock_get_facts(job_id: str) -> FactsJSON:
        data = load_example("facts.json")
        data["meta"]["job_id"] = job_id
        return FactsJSON.model_validate(data)

    @mock_app.get("/api/jobs/{job_id}/layers/{layer_name}")
    def mock_get_layer(job_id: str, layer_name: str):
        if layer_name.endswith(".png") or layer_name in ("s1_pre", "s1_post", "flood_probability", "uncertainty"):
            png_1x1 = (
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15"
                b"\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            return Response(content=png_1x1, media_type="image/png")
        return {
            "type": "FeatureCollection",
            "name": layer_name,
            "job_id": job_id,
            "features": []
        }

    @mock_app.post("/api/upstream")
    def mock_upstream(payload: Dict[str, Any]):
        pt = payload.get("point", [85.25, 27.95])
        return {
            "status": "success",
            "point": pt,
            "disclaimer": "terrain-based estimate, not a hydraulic simulation",
            "flow_path": {
                "type": "LineString",
                "coordinates": [pt, [pt[0] + 0.02, pt[1] - 0.02]]
            },
            "corridor_settlements": [
                {"name": "Trishuli Bazaar", "distance_downstream_km": 4.2}
            ]
        }

    @mock_app.post("/api/copilot/ask")
    def mock_copilot_ask(payload: Dict[str, Any]):
        lang = payload.get("lang", "en")
        if lang == "ne":
            ans = "बाढी प्रभावित क्षेत्र १४.२ वर्ग कि.मी. छ, ३१२ भवनहरू प्रभावित छन्।"
        else:
            ans = "Flooded area is 14.2 km², with 312 buildings affected."
        return {
            "answer": ans,
            "facts_used": ["flooded_area_km2", "buildings_affected"],
            "template_fallback": True,
            "disclaimer": "Numbers are rendered by code from the maps, not generated by the language model."
        }

    @mock_app.get("/api/jobs/{job_id}/report")
    def mock_report(job_id: str, lang: str = "en"):
        html = f"""<!DOCTYPE html><html><body><h1>DYOTAK Mock Report for {job_id} ({lang})</h1></body></html>"""
        return Response(content=html, media_type="text/html")

    @mock_app.get("/api/tiles/terrain/{z}/{x}/{y}.png")
    def mock_tiles(z: int, x: int, y: int):
        png_1x1 = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15"
            b"\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        return Response(content=png_1x1, media_type="image/png")

    @mock_app.get("/api/eval/summary")
    def mock_eval_summary():
        return {
            "status": "precomputed",
            "splits": {
                "held_out_himalayan": {
                    "onnx_model": {"iou": 0.74, "f1": 0.85},
                    "log_ratio_baseline": {"iou": 0.61, "f1": 0.75}
                }
            }
        }

    @mock_app.get("/api/eval/emsr927")
    def mock_eval_emsr927():
        return {
            "disclaimer": "precomputed validation",
            "case_study": "EMSR927 Trishuli Basin",
            "event_date": "2024-09-28",
            "metrics": {"iou": 0.72, "f1": 0.83}
        }

    return mock_app


app = create_mock_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("contracts.mock_server:app", host="0.0.0.0", port=8000, reload=True)
