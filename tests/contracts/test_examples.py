"""Every example payload in /contracts/examples must validate against its schema.

Guarantees the frontend can rely on the mocks matching the published contract.
"""

import pytest

from contracts.schemas import (
    ErrorResponse,
    FactsJSON,
    HealthResponse,
    JobRequest,
    JobStatus,
    PreflightRequest,
    PreflightResponse,
    Provenance,
    ResultManifest,
)


EXAMPLE_MODELS = {
    "health_response.json": HealthResponse,
    "error_response.json": ErrorResponse,
    "preflight_request.json": PreflightRequest,
    "preflight_response.json": PreflightResponse,
    "job_request.json": JobRequest,
    "job_status_queued.json": JobStatus,
    "job_status_running.json": JobStatus,
    "job_status_succeeded.json": JobStatus,
    "job_status_failed.json": JobStatus,
    "provenance.json": Provenance,
    "result_manifest.json": ResultManifest,
    "facts.json": FactsJSON,
}


@pytest.mark.parametrize("filename,model", list(EXAMPLE_MODELS.items()))
def test_example_validates_against_schema(example, filename, model):
    payload = example(filename)
    instance = model.model_validate(payload)
    # Round-trip must be lossless for the contract.
    assert model.model_validate(instance.model_dump(mode="json"))


def test_all_example_files_are_covered(examples_dir):
    """Fail if an example JSON is added without a schema mapping."""
    present = {p.name for p in examples_dir.glob("*.json")}
    assert present == set(EXAMPLE_MODELS), (
        f"unmapped examples: {present - set(EXAMPLE_MODELS)}; "
        f"missing examples: {set(EXAMPLE_MODELS) - present}"
    )


def test_facts_contain_required_fact_ids(example):
    facts = FactsJSON.model_validate(example("facts.json"))
    ids = {fact.id for fact in facts.facts}
    required = {
        "flooded_area_km2",
        "buildings_affected",
        "buildings_possibly_affected",
        "road_km_affected",
        "bridges_possibly_impacted",
        "settlements_newly_cut_off",
        "settlements_no_pre_event_access",
        "settlements_still_connected",
    }
    assert required.issubset(ids)


def test_provenance_osm_snapshot_precedes_event(example):
    facts = FactsJSON.model_validate(example("facts.json"))
    assert facts.meta.provenance.osm_snapshot_date < facts.meta.event_date


def test_failed_job_carries_typed_error(example):
    status = JobStatus.model_validate(example("job_status_failed.json"))
    assert status.status.value == "failed"
    assert status.error is not None
